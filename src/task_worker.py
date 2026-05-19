import logging
import time

from .config_loader import ConfigLoader
from .llm_client import LLMClient
from .arena_client import ArenaClient
from .plan_agent import PlanAgent
from .expert_agent import ExpertAgent
from .dag_executor import DAGExecutor
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

            # Step 2: Plan Agent 分解任务
            logger.info(f"[Task {task_id[:8]}] Running Plan Agent...")
            plan_result, plan_response = self.plan_agent.analyze(title, content)
            category = self.plan_agent.get_category(plan_result)

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
            mode = plan_result.get("decomposition_mode", "single_direct")
            sub_count = len(plan_result.get("sub_tasks", []))
            logger.info(f"[Task {task_id[:8]}] Plan: mode={mode}, sub_tasks={sub_count}, category={category}")

            # Step 3: DAG Executor 执行子任务
            logger.info(f"[Task {task_id[:8]}] Executing DAG ({sub_count} sub-tasks)...")
            dag = DAGExecutor(self.config, title, content)
            answer, token_records = dag.execute(plan_result)

            # 记录 expert agent token 使用
            for i, record in enumerate(token_records):
                task_logger.log_expert_agent(
                    round_num=i + 1,
                    input_tokens=record["input_tokens"],
                    output_tokens=record["output_tokens"],
                    reasoning_tokens=record["reasoning_tokens"],
                    latency_ms=record["latency_ms"],
                    answer_preview=dag.results.get(record["sub_task_id"], "")[:200],
                    reasoning_text="",
                )

            # Step 4: 提交答案
            logger.info(f"[Task {task_id[:8]}] Submitting answer (length={len(answer)})...")
            submit_result = self.arena.submit_answer(task_id, answer)
            task_logger.log_submission(len(answer))

            # Step 5: 反馈循环（用主 category 的 expert 回复评审）
            logger.info(f"[Task {task_id[:8]}] Starting feedback loop...")
            reply_expert = ExpertAgent(category, self.config)
            # 重建对话上下文：user question + our answer
            user_content = reply_expert._build_initial_prompt(title, content)
            reply_expert.messages.append({"role": "user", "content": user_content})
            reply_expert.messages.append({"role": "assistant", "content": answer})

            feedback_loop = FeedbackLoop(
                config_loader=self.config,
                arena_client=self.arena,
                expert_agent=reply_expert,
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
                "feedback_text": feedback_result.get("feedback_text", ""),
                "tokens_used": task_logger.data["token_summary"]["grand_total_tokens"],
                "decomposition_mode": plan_result.get("decomposition_mode", "single_direct"),
                "plan_result": plan_result,
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

    def resume_task(self, task_id: str) -> dict:
        """
        恢复已提交但未完成反馈流程的任务
        跳过答案生成和提交，直接进入 feedback loop
        """
        task_logger = None
        try:
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

            # Plan Agent 分解
            plan_result, plan_response = self.plan_agent.analyze(title, content)
            category = self.plan_agent.get_category(plan_result)
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

            # 获取已提交的答案，重建 Expert Agent 上下文
            my_answer_result = self.arena.get_my_answer(task_id)
            existing_answer = my_answer_result.get("content", "")

            expert = ExpertAgent(category, self.config)
            # 重建对话历史：system + user question + assistant answer
            user_content = expert._build_initial_prompt(title, content)
            expert.messages.append({"role": "user", "content": user_content})
            expert.messages.append({"role": "assistant", "content": existing_answer})

            task_logger.log_expert_agent(
                round_num=1,
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                latency_ms=0,
                answer_preview=existing_answer[:200],
                reasoning_text="",
            )
            task_logger.log_submission(len(existing_answer))

            logger.info(f"[Task {task_id[:8]}] Resuming feedback loop...")
            feedback_loop = FeedbackLoop(
                config_loader=self.config,
                arena_client=self.arena,
                expert_agent=expert,
                task_logger=task_logger,
            )
            feedback_result = feedback_loop.run(task_id, post_id, category)

            task_logger.finish("completed")
            self.summary_logger.update_from_task_log(task_logger)

            return {
                "task_id": task_id,
                "title": title,
                "category": category,
                "status": "completed",
                "score": feedback_result.get("score"),
                "feedback_text": feedback_result.get("feedback_text", ""),
                "tokens_used": task_logger.data["token_summary"]["grand_total_tokens"],
                "decomposition_mode": plan_result.get("decomposition_mode", "single_direct"),
                "plan_result": plan_result,
            }

        except Exception as e:
            logger.error(f"[Task {task_id[:8]}] Resume failed: {type(e).__name__}: {e}")
            if task_logger:
                task_logger.log_exception(type(e).__name__, str(e), "abort", stage="resume")
                task_logger.finish("failed")
                self.summary_logger.update_from_task_log(task_logger)
            return {"task_id": task_id, "status": "failed", "error": str(e)}
