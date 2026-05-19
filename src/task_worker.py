import logging
import time

from .config_loader import ConfigLoader
from .llm_client import LLMClient
from .arena_client import ArenaClient
from .plan_agent import PlanAgent
from .expert_agent import ExpertAgent
from .feedback_loop import FeedbackLoop
from .logger import TaskLogger, SummaryLogger
from .exceptions import TokenBudgetExceeded, TaskProcessingError

logger = logging.getLogger(__name__)


class TaskWorker:
    """单任务处理进程：完整的题目处理流水线"""

    def __init__(self, config_loader: ConfigLoader):
        self.config = config_loader
        self.arena = ArenaClient()
        self.plan_agent = PlanAgent(config_loader)
        self.limits = config_loader.get_limits()
        self.summary_logger = SummaryLogger(
            config_loader.settings.get("logging", {}).get("log_dir", "logs")
        )

    def process_task(self, task_id: str) -> dict:
        """
        处理单个任务的完整流程
        返回处理结果摘要
        """
        task_logger = None
        try:
            # Step 1: 获取题目
            logger.info(f"[Task {task_id[:8]}] Fetching task details...")
            task_detail = self.arena.get_task_detail(task_id)
            title = task_detail.get("title", "")
            content = ""
            agora_post = task_detail.get("agora_post", {})
            if agora_post:
                content = agora_post.get("description", "") or agora_post.get("content", "")
            post_id = task_detail.get("agora_post_id", "")
            reward_pool = task_detail.get("reward_pool", 0)

            task_logger = TaskLogger(
                task_id=task_id,
                title=title,
                log_dir=self.config.settings.get("logging", {}).get("log_dir", "logs"),
            )
            task_logger.set_task_detail(task_detail)
            task_logger.set_reward_pool(reward_pool)
            logger.info(f"[Task {task_id[:8]}] Title: {title}, Reward: {reward_pool}")

            # Step 2: Plan Agent 分类
            logger.info(f"[Task {task_id[:8]}] Running Plan Agent...")
            plan_result, plan_response = self.plan_agent.analyze(title, content)
            category = plan_result["category"]

            task_logger.set_category(category)
            task_logger.log_plan_agent(
                input_text=f"{title}\n{content[:500]}",
                output_json=plan_result,
                reasoning_text=plan_response.reasoning,
                input_tokens=plan_response.input_tokens,
                output_tokens=plan_response.output_tokens,
                reasoning_tokens=plan_response.reasoning_tokens,
                latency_ms=plan_response.latency_ms,
            )
            logger.info(f"[Task {task_id[:8]}] Category: {category}")

            # Step 3: Expert Agent 生成答案
            logger.info(f"[Task {task_id[:8]}] Running Expert Agent ({category})...")
            expert = ExpertAgent(category, self.config)
            answer, expert_response = expert.generate_answer(title, content)

            task_logger.log_expert_agent(
                round_num=1,
                input_tokens=expert_response.input_tokens,
                output_tokens=expert_response.output_tokens,
                reasoning_tokens=expert_response.reasoning_tokens,
                latency_ms=expert_response.latency_ms,
                answer_preview=answer,
                reasoning_text=expert_response.reasoning,
            )

            # Step 4: 提交答案
            logger.info(f"[Task {task_id[:8]}] Submitting answer (length={len(answer)})...")
            submit_result = self.arena.submit_answer(task_id, answer)
            task_logger.log_submission(len(answer))

            # Step 5: 反馈循环
            logger.info(f"[Task {task_id[:8]}] Starting feedback loop...")
            feedback_loop = FeedbackLoop(
                config_loader=self.config,
                arena_client=self.arena,
                expert_agent=expert,
                task_logger=task_logger,
            )
            feedback_result = feedback_loop.run(task_id, post_id, category)

            # 完成
            task_logger.finish("completed")
            self.summary_logger.update_from_task_log(task_logger)

            return {
                "task_id": task_id,
                "title": title,
                "category": category,
                "status": "completed",
                "score": feedback_result.get("score"),
                "tokens_used": task_logger.data["token_summary"]["grand_total_tokens"],
            }

        except TokenBudgetExceeded as e:
            logger.error(f"[Task {task_id[:8]}] Token budget exceeded: {e}")
            if task_logger:
                task_logger.log_exception("TokenBudgetExceeded", str(e), "abort", stage="budget_check")
                task_logger.finish("failed_budget")
                self.summary_logger.update_from_task_log(task_logger)
            return {"task_id": task_id, "status": "failed_budget", "error": str(e)}

        except Exception as e:
            logger.error(f"[Task {task_id[:8]}] Processing failed: {type(e).__name__}: {e}")
            if task_logger:
                task_logger.log_exception(type(e).__name__, str(e), "abort", stage="unknown")
                task_logger.finish("failed")
                self.summary_logger.update_from_task_log(task_logger)
            return {"task_id": task_id, "status": "failed", "error": str(e)}
