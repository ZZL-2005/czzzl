import logging

from .config_loader import ConfigLoader
from .expert_agent import ExpertAgent
from .arena_client import ArenaClient
from .logger import TaskLogger

logger = logging.getLogger(__name__)


class FeedbackLoop:
    """反馈循环：评分轮询 + 分析反馈 + 回复评审"""

    def __init__(
        self,
        config_loader: ConfigLoader,
        arena_client: ArenaClient,
        expert_agent: ExpertAgent,
        task_logger: TaskLogger,
    ):
        self.config = config_loader
        self.arena = arena_client
        self.expert = expert_agent
        self.task_logger = task_logger
        self.max_rounds = config_loader.get_limits().get("max_feedback_rounds", 2)

    def run(self, task_id: str, post_id: str, category: str) -> dict:
        """
        执行反馈循环
        返回最终结果 {status, score, feedback_text}
        """
        # Step 1: 轮询等待评分（最多10分钟）
        my_answer_result = self._poll_for_score(task_id)

        if my_answer_result is None:
            logger.warning(f"Score poll timed out for task {task_id[:8]}, skipping feedback")
            return {"status": "completed", "score": None, "feedback_text": ""}

        # Step 2: 记录评分
        answer = my_answer_result.get("answer", {})
        score = answer.get("score")
        score_detail = answer.get("score_detail", {})
        poll_attempts = my_answer_result.get("_poll_attempts", 0)

        self.task_logger.log_scoring(
            score=score or 0,
            max_score=score_detail.get("max_score", abs(score_detail.get("min_score", 0))),
            min_score=score_detail.get("min_score", 0),
            final_score=score_detail.get("final_score", score or 0),
            scoring_mode=score_detail.get("scoring_mode", "unknown"),
            review_text="",
            scored_at=answer.get("updated_at", ""),
            poll_attempts=poll_attempts,
        )

        # Step 3: 高分题跳过反馈回复（节省token，避免画蛇添足）
        skip_threshold = self.config.settings.get("limits", {}).get("skip_reply_score", 40)
        if score is not None and score >= skip_threshold:
            logger.info(f"Score {score} >= {skip_threshold}, skipping feedback reply")
            return {"status": "completed", "score": score, "feedback_text": ""}

        # Step 4: 轮询等待评审反馈（30s一次，最多10分钟）
        feedback_text, comment_id = self._poll_for_feedback(task_id)

        if not feedback_text:
            logger.warning(f"No feedback received for task {task_id[:8]}, skipping reply")
            return {"status": "completed", "score": score, "feedback_text": ""}

        logger.info(f"Got evaluator feedback (comment_id={comment_id[:8]}): {feedback_text[:100]}...")
        if self.task_logger.data.get("scoring"):
            self.task_logger.data["scoring"]["review_text"] = feedback_text

        # Step 5: 生成针对性补充，拼接原始答案后回复评审
        try:
            reply, reply_response = self.expert.generate_reply(feedback_text)

            self.task_logger.log_expert_agent(
                round_num=2,
                input_tokens=reply_response.input_tokens,
                output_tokens=reply_response.output_tokens,
                reasoning_tokens=reply_response.reasoning_tokens,
                latency_ms=reply_response.latency_ms,
                answer_preview=reply,
                reasoning_text=reply_response.reasoning,
                is_feedback_round=True,
                feedback_received=feedback_text,
            )

            # 拼接：原始答案 + 补充修正
            original_answer = self.expert.messages[2]["content"]
            full_reply = f"{original_answer}\n\n---\n\n## 补充与修正\n\n{reply}"

            self.arena.reply_to_comment(post_id, comment_id, full_reply)
            self.task_logger.log_reply(full_reply, 1)
        except Exception as e:
            self.task_logger.log_exception(
                type(e).__name__, str(e), "skip_reply",
                stage="feedback_reply"
            )
            logger.error(f"Feedback reply failed: {e}")

        return {"status": "completed", "score": score, "feedback_text": feedback_text}

    def _poll_for_score(self, task_id: str, interval: int = 10, max_attempts: int = 60) -> dict | None:
        """轮询等待评分结果（最多 max_attempts 次，默认10分钟）"""
        import time
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            result = self.arena.get_my_answer(task_id)
            if result:
                answer = result.get("answer", {})
                if answer.get("score") is not None:
                    result["_poll_attempts"] = attempts
                    logger.info(f"Score received after {attempts} polls ({attempts * interval}s): {answer['score']}")
                    return result
            logger.debug(f"[Score Poll #{attempts}/{max_attempts}] score not ready, waiting {interval}s...")
            time.sleep(interval)
        logger.warning(f"Score poll timed out after {max_attempts} attempts for task {task_id[:8]}")
        return None

    def _poll_for_feedback(self, task_id: str, interval: int = 30, max_attempts: int = 20) -> tuple[str, str]:
        """轮询等待评审反馈（30s一次，最多20次=10分钟）"""
        import time
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            result = self.arena.get_my_answer(task_id)
            if result:
                feedback_text, comment_id = self.arena.get_evaluator_feedback(result)
                if feedback_text:
                    logger.info(f"Got evaluator feedback after {attempts} polls ({attempts * interval}s)")
                    return feedback_text, comment_id
            logger.debug(f"[Feedback Poll #{attempts}/{max_attempts}] no comment yet, waiting {interval}s...")
            time.sleep(interval)
        logger.warning(f"Feedback poll timed out after {max_attempts} attempts for task {task_id[:8]}")
        return "", ""

    def _process_feedback_round(
        self, round_num: int, feedback_text: str,
        post_id: str, comment_id: str,
    ) -> str | None:
        """处理一轮反馈：生成回复 + refine prompt（两个独立请求）"""
        try:
            # 请求1: 生成改进回复
            reply, reply_response = self.expert.generate_reply(feedback_text)

            self.task_logger.log_expert_agent(
                round_num=round_num + 1,
                input_tokens=reply_response.input_tokens,
                output_tokens=reply_response.output_tokens,
                reasoning_tokens=reply_response.reasoning_tokens,
                latency_ms=reply_response.latency_ms,
                answer_preview=reply,
                reasoning_text=reply_response.reasoning,
                is_feedback_round=True,
                feedback_received=feedback_text,
            )

            # 回复评审的 comment
            self.arena.reply_to_comment(post_id, comment_id, reply)
            self.task_logger.log_reply(reply, round_num)

            # 请求2: refine prompt（落盘，优化下次同类任务的 prompt）
            try:
                refined, refine_response = self.expert.refine_prompt(feedback_text)
                self.task_logger.log_refine_prompt(
                    category=self.expert.category,
                    feedback_text=feedback_text,
                    refined_prompt=refined,
                    input_tokens=refine_response.input_tokens,
                    output_tokens=refine_response.output_tokens,
                    reasoning_tokens=refine_response.reasoning_tokens,
                    latency_ms=refine_response.latency_ms,
                )
                logger.info(f"Refined prompt saved for {self.expert.category} ({len(refined)} chars)")
            except Exception as e:
                logger.warning(f"Refine prompt failed (non-critical): {e}")

            return reply

        except Exception as e:
            self.task_logger.log_exception(
                type(e).__name__, str(e), "skip_round",
                stage=f"feedback_round_{round_num}"
            )
            logger.error(f"Feedback round {round_num} failed: {e}")
            return None
