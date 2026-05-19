import logging
import time

from .config_loader import ConfigLoader
from .llm_client import LLMClient
from .arena_client import ArenaClient
from .plan_agent import PlanAgent
from .expert_agent import ExpertAgent
from .feedback_loop import FeedbackLoop
from .knowledge_retrieval import KnowledgeRetrieval
from .paper_cache import PaperCache
from .logger import TaskLogger, SummaryLogger
from .exceptions import TokenBudgetExceeded, TaskProcessingError

logger = logging.getLogger(__name__)


class TaskWorker:
    """单任务处理进程：完整的题目处理流水线"""

    def __init__(self, config_loader: ConfigLoader, paper_cache: PaperCache | None = None):
        self.config = config_loader
        self.arena = ArenaClient()
        self.plan_agent = PlanAgent(config_loader)
        self.limits = config_loader.get_limits()
        self.summary_logger = SummaryLogger(
            config_loader.settings.get("logging", {}).get("log_dir", "logs")
        )
        # 知识检索（共享 paper_cache 实例）
        log_dir = config_loader.settings.get("logging", {}).get("log_dir", "logs")
        self.paper_cache = paper_cache or PaperCache(f"{log_dir}/paper_cache")
        self.knowledge_retrieval = KnowledgeRetrieval(config_loader, self.paper_cache)

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

            # Step 3: 知识检索 — 从学术数据库获取相关文献
            logger.info(f"[Task {task_id[:8]}] Running knowledge retrieval...")
            retrieval_result = self.knowledge_retrieval.retrieve(
                title, content, category, task_id=task_id
            )
            reference_context = retrieval_result.context_text
            if reference_context:
                logger.info(
                    f"[Task {task_id[:8]}] Retrieved {len(retrieval_result.papers_used)} papers "
                    f"(cache_hits={retrieval_result.cache_hits}, tokens={retrieval_result.tokens_used})"
                )
                task_logger.log_retrieval(retrieval_result)
            else:
                logger.info(f"[Task {task_id[:8]}] No relevant papers found")

            # Step 4: Expert Agent 生成答案（注入检索上下文）
            logger.info(f"[Task {task_id[:8]}] Running Expert Agent ({category})...")
            expert = ExpertAgent(category, self.config)
            answer, expert_response = expert.generate_answer(title, content, reference_context)

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
                "feedback_text": feedback_result.get("feedback_text", ""),
                "tokens_used": task_logger.data["token_summary"]["grand_total_tokens"],
                "papers_used": len(retrieval_result.papers_used),
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

            # Plan Agent 分类
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
            }

        except Exception as e:
            logger.error(f"[Task {task_id[:8]}] Resume failed: {type(e).__name__}: {e}")
            if task_logger:
                task_logger.log_exception(type(e).__name__, str(e), "abort", stage="resume")
                task_logger.finish("failed")
                self.summary_logger.update_from_task_log(task_logger)
            return {"task_id": task_id, "status": "failed", "error": str(e)}
