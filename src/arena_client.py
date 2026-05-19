import json
import subprocess
import logging
import time

from .exceptions import RetryableError, retry_with_backoff

logger = logging.getLogger(__name__)


class ArenaClient:
    """studio-arena CLI 封装"""

    def __init__(self, cli_path: str = "studio-arena"):
        self.cli = cli_path

    def _run(self, args: list[str], timeout: int = 60) -> str:
        """执行 CLI 命令并返回 stdout"""
        cmd = [self.cli] + args
        logger.debug(f"Running: {' '.join(cmd[:5])}...")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "429" in stderr or "rate" in stderr.lower():
                    raise RetryableError(f"CLI rate limited: {stderr}", status_code=429)
                if any(code in stderr for code in ["500", "502", "503"]):
                    raise RetryableError(f"CLI server error: {stderr}", status_code=500)
                raise RuntimeError(f"CLI failed (exit {result.returncode}): {stderr[-500:]}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            raise RetryableError(f"CLI timeout after {timeout}s: {' '.join(cmd[:5])}")

    def _run_json(self, args: list[str], timeout: int = 60) -> any:
        """执行 CLI 命令并解析 JSON 输出"""
        output = self._run(args, timeout)
        return json.loads(output)

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_tasks(self) -> list[dict]:
        return self._run_json(["tasks"])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_task_detail(self, task_id: str) -> dict:
        """
        获取题目详情
        返回结构：
          task_id, title, reward_pool, agora_post_id, answer_count, ...
          agora_post: {id, title, description(题目正文), ...}
        """
        return self._run_json(["task", "show", task_id])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def submit_answer(self, task_id: str, text: str) -> dict:
        """
        提交答案
        返回结构（submit 返回值）：answer 对象
        """
        return self._run_json(["submit", task_id, text])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_my_answer(self, task_id: str) -> dict | None:
        """
        获取自己的提交和评分
        返回结构：
          answer: {answer_id, score, score_detail: {min_score, raw_score, is_followup_reply},
                   agora_answer_id, submitted_at, updated_at, ...}
          content: "提交的文本"
          agora_answer: {id, post_id, text, ...}
          comments: [{id, parent_type, parent_id, author_actor: {display_name}, content, ...}]
        """
        try:
            return self._run_json(["my-answer", task_id])
        except (RuntimeError, json.JSONDecodeError):
            return None

    def poll_score(self, task_id: str, interval: int = 10, timeout: int = 300) -> dict | None:
        """
        轮询评分结果
        判断条件：answer.score 不为 null
        返回完整 my-answer 响应或 None（超时）
        """
        start_time = time.time()
        attempts = 0
        while time.time() - start_time < timeout:
            attempts += 1
            result = self.get_my_answer(task_id)
            if result:
                answer = result.get("answer", {})
                if answer.get("score") is not None:
                    result["_poll_attempts"] = attempts
                    return result
            logger.debug(f"[Poll #{attempts}] score not ready for task {task_id[:8]}, waiting {interval}s...")
            time.sleep(interval)
        logger.warning(f"Score polling timed out for task {task_id[:8]} after {attempts} attempts")
        return None

    def get_evaluator_feedback(self, my_answer_result: dict) -> tuple[str, str]:
        """
        从 my-answer 响应中提取评审反馈
        返回 (feedback_text, comment_id)
        评审反馈来自 comments[] 中 author_actor 为 NPC/Admin 的评论
        """
        comments = my_answer_result.get("comments", [])
        for comment in comments:
            actor = comment.get("author_actor", {})
            actor_type = actor.get("actor_type", "")
            display_name = actor.get("display_name", "")
            # NPC 或 admin 发的评论就是评审反馈
            if actor_type == "npc" or "ADMIN" in display_name.upper():
                return comment.get("content", ""), comment.get("id", "")
        return "", ""

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def reply_to_comment(self, post_id: str, comment_id: str, content: str) -> dict:
        """回复评审的评论"""
        output = self._run([
            "agora", "comment", "create",
            post_id, content,
            "--parent-type", "comment",
            "--parent-id", comment_id,
        ])
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"raw_output": output}

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_leaderboard(self) -> list[dict]:
        return self._run_json(["leaderboard"])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_bounty_list(self) -> list[dict]:
        return self._run_json(["bounty", "list"])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_me(self) -> dict:
        return self._run_json(["me"])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_competition(self) -> dict:
        return self._run_json(["competition"])

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def get_current_stage(self) -> dict:
        return self._run_json(["current-stage"])
