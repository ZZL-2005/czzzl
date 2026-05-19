import json
import logging
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


class TaskLogger:
    """单任务结构化日志记录器"""

    def __init__(self, task_id: str, title: str, log_dir: str = "logs"):
        self.task_id = task_id
        self.title = title
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        safe_title = title.replace("/", "_").replace(" ", "_")[:50]
        short_id = task_id[:8]
        self.log_file = self.log_dir / f"task_{short_id}_{safe_title}.json"

        self.data: dict[str, Any] = {
            "task_id": task_id,
            "title": title,
            "task_detail": None,
            "category": None,
            "reward_pool": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "status": "in_progress",
            "plan_agent": None,
            "expert_agent": {"rounds": []},
            "submission": None,
            "scoring": None,
            "reply": [],
            "refine_prompt": None,
            "token_summary": {
                "plan_agent_total": 0,
                "expert_agent_total": 0,
                "grand_total_tokens": 0,
            },
            "cost_summary": {
                "reward_pool": 0,
                "score_earned": None,
                "max_score": None,
                "min_score": None,
            },
            "exceptions": [],
        }

        self._console_logger = logging.getLogger(f"task.{short_id}")

    def set_task_detail(self, task_detail: dict):
        self.data["task_detail"] = task_detail

    def set_category(self, category: str):
        self.data["category"] = category

    def set_reward_pool(self, reward_pool: int):
        self.data["reward_pool"] = reward_pool
        self.data["cost_summary"]["reward_pool"] = reward_pool

    def log_plan_agent(
        self,
        input_text: str,
        output_json: dict,
        reasoning_text: str,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        latency_ms: int,
    ):
        total = input_tokens + output_tokens + reasoning_tokens
        self.data["plan_agent"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total,
            "latency_ms": latency_ms,
            "input_text": input_text,
            "output_json": output_json,
            "reasoning_text": reasoning_text,
        }
        self.data["token_summary"]["plan_agent_total"] = total
        self._update_grand_total()
        self._console_logger.info(
            f"[Plan Agent] category={output_json.get('category', '?')} "
            f"tokens={total} latency={latency_ms}ms"
        )

    def log_expert_agent(
        self,
        round_num: int,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        latency_ms: int,
        answer_preview: str,
        reasoning_text: str = "",
        is_feedback_round: bool = False,
        feedback_received: str = "",
    ):
        total = input_tokens + output_tokens + reasoning_tokens
        entry = {
            "round": round_num,
            "is_feedback_round": is_feedback_round,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total,
            "latency_ms": latency_ms,
            "answer_preview": answer_preview[:300],
            "reasoning_text": reasoning_text[:500] if reasoning_text else "",
        }
        if is_feedback_round:
            entry["feedback_received"] = feedback_received[:500]

        self.data["expert_agent"]["rounds"].append(entry)
        self.data["token_summary"]["expert_agent_total"] += total
        self._update_grand_total()
        round_type = "Feedback" if is_feedback_round else "Initial"
        self._console_logger.info(
            f"[Expert R{round_num}] ({round_type}) tokens={total} latency={latency_ms}ms"
        )

    def log_submission(self, answer_length: int):
        self.data["submission"] = {
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "answer_length": answer_length,
        }
        self._console_logger.info(f"[Submit] answer_length={answer_length}")

    def log_scoring(
        self,
        score: float,
        max_score: int,
        min_score: int,
        final_score: int,
        scoring_mode: str,
        review_text: str,
        scored_at: str,
        poll_attempts: int,
    ):
        self.data["scoring"] = {
            "score": score,
            "max_score": max_score,
            "min_score": min_score,
            "final_score": final_score,
            "scoring_mode": scoring_mode,
            "review_text": review_text,
            "scored_at": scored_at,
            "poll_attempts": poll_attempts,
        }
        self.data["cost_summary"]["score_earned"] = score
        self.data["cost_summary"]["max_score"] = max_score
        self.data["cost_summary"]["min_score"] = min_score
        self._console_logger.info(
            f"[Score] {score}/{max_score} (min={min_score}) mode={scoring_mode} polls={poll_attempts}"
        )

    def log_reply(self, reply_text: str, round_num: int):
        entry = {
            "round": round_num,
            "replied_at": datetime.now(timezone.utc).isoformat(),
            "reply_length": len(reply_text),
            "reply_preview": reply_text[:300],
        }
        self.data["reply"].append(entry)
        self._console_logger.info(f"[Reply R{round_num}] length={len(reply_text)}")

    def log_refine_prompt(
        self,
        category: str,
        feedback_text: str,
        refined_prompt: str,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        latency_ms: int,
    ):
        self.data["refine_prompt"] = {
            "refined_at": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "feedback_trigger": feedback_text[:500],
            "refined_prompt_preview": refined_prompt[:1000],
            "refined_prompt_length": len(refined_prompt),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "latency_ms": latency_ms,
        }
        self._console_logger.info(
            f"[Refine] category={category} prompt_length={len(refined_prompt)} "
            f"tokens={input_tokens + output_tokens + reasoning_tokens} latency={latency_ms}ms"
        )

    def log_exception(self, exc_type: str, message: str, action: str, retry_count: int = 0, resolved: bool = False, stage: str = ""):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": exc_type,
            "message": message[:500],
            "action": action,
            "retry_count": retry_count,
            "resolved": resolved,
            "stage": stage,
        }
        self.data["exceptions"].append(entry)
        self._console_logger.warning(f"[Exception] {exc_type}: {message[:100]} action={action}")

    def finish(self, status: str = "completed"):
        self.data["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.data["status"] = status
        self.save()
        self._console_logger.info(
            f"[Done] status={status} total_tokens={self.data['token_summary']['grand_total_tokens']} "
            f"score={self.data['cost_summary'].get('score_earned', '?')}"
        )

    def save(self):
        with open(self.log_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _update_grand_total(self):
        self.data["token_summary"]["grand_total_tokens"] = (
            self.data["token_summary"]["plan_agent_total"]
            + self.data["token_summary"]["expert_agent_total"]
        )


class SummaryLogger:
    """全局汇总日志（线程安全，单例）"""

    _instances: dict[str, "SummaryLogger"] = {}
    _class_lock = threading.Lock()

    def __new__(cls, log_dir: str = "logs"):
        with cls._class_lock:
            key = str(Path(log_dir).resolve())
            if key not in cls._instances:
                instance = super().__new__(cls)
                cls._instances[key] = instance
            return cls._instances[key]

    def __init__(self, log_dir: str = "logs"):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.summary_file = self.log_dir / "summary.json"
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.summary_file.exists():
            with open(self.summary_file, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {
                "last_updated": None,
                "total_tasks_processed": 0,
                "total_tasks_succeeded": 0,
                "total_tasks_failed": 0,
                "total_score": 0.0,
                "total_tokens_used": 0,
                "by_category": {},
                "exception_stats": {
                    "rate_limit_hits": 0,
                    "timeouts": 0,
                    "total_retries": 0,
                },
            }

    def update_from_task_log(self, task_log: TaskLogger):
        with self._lock:
            self.data["last_updated"] = datetime.now(timezone.utc).isoformat()
            self.data["total_tasks_processed"] += 1

            if task_log.data["status"] == "completed":
                self.data["total_tasks_succeeded"] += 1
            else:
                self.data["total_tasks_failed"] += 1

            score = task_log.data["cost_summary"].get("score_earned") or 0
            self.data["total_score"] += score
            self.data["total_tokens_used"] += task_log.data["token_summary"]["grand_total_tokens"]

            category = task_log.data.get("category", "unknown")
            if category not in self.data["by_category"]:
                self.data["by_category"][category] = {"count": 0, "score": 0, "tokens": 0}
            self.data["by_category"][category]["count"] += 1
            self.data["by_category"][category]["score"] += score
            self.data["by_category"][category]["tokens"] += task_log.data["token_summary"]["grand_total_tokens"]

            for exc in task_log.data.get("exceptions", []):
                self.data["exception_stats"]["total_retries"] += exc.get("retry_count", 0)
                if "RateLimit" in exc.get("type", "") or "429" in exc.get("message", ""):
                    self.data["exception_stats"]["rate_limit_hits"] += 1
                if "Timeout" in exc.get("type", ""):
                    self.data["exception_stats"]["timeouts"] += 1

            self.save()

    def save(self):
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
