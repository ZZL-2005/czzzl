import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

from .expert_agent import ExpertAgent
from .config_loader import ConfigLoader
from .llm_client import LLMResponse

logger = logging.getLogger(__name__)


class DAGExecutor:
    """DAG 执行引擎：按拓扑序并行/串行执行子任务，最终汇总"""

    def __init__(self, config_loader: ConfigLoader, title: str, content: str):
        self.config = config_loader
        self.title = title
        self.content = content
        self.results: dict[str, str] = {}
        self.token_usage: list[dict] = []

    def execute(self, plan: dict) -> tuple[str, list[dict]]:
        """
        执行 DAG 分解方案
        返回 (final_answer, token_usage_records)
        """
        sub_tasks = plan.get("sub_tasks", [])
        final_synthesis = plan.get("final_synthesis")

        if not sub_tasks:
            return "", []

        # 单任务退化：直接用原始问题调 expert
        if len(sub_tasks) == 1 and not final_synthesis:
            return self._execute_single(sub_tasks[0])

        # 多任务：拓扑排序 + 分层执行
        levels = self._topological_sort(sub_tasks)
        for level in levels:
            self._execute_level(level, sub_tasks)

        # 最终汇总
        if final_synthesis:
            return self._execute_synthesis(final_synthesis)

        # 无 synthesis 时直接拼接
        ordered_results = [self.results[t["id"]] for t in sub_tasks if t["id"] in self.results]
        final_answer = "\n\n---\n\n".join(ordered_results)
        return final_answer, self.token_usage

    def _execute_single(self, task: dict) -> tuple[str, list[dict]]:
        """单任务退化模式：等价于 v2 直接调 expert"""
        expert = ExpertAgent(task["expert"], self.config)
        answer, response = expert.generate_answer(self.title, self.content)
        self.token_usage.append(self._make_usage_record(task["id"], task["expert"], response))
        self.results[task["id"]] = answer
        return answer, self.token_usage

    def _topological_sort(self, sub_tasks: list[dict]) -> list[list[str]]:
        """拓扑排序，返回分层的 task id 列表（同层可并行）"""
        id_to_task = {t["id"]: t for t in sub_tasks}
        in_degree = {t["id"]: len(t.get("depends_on", [])) for t in sub_tasks}
        adj = {t["id"]: [] for t in sub_tasks}
        for t in sub_tasks:
            for dep in t.get("depends_on", []):
                if dep in adj:
                    adj[dep].append(t["id"])

        levels = []
        queue = deque([tid for tid, deg in in_degree.items() if deg == 0])

        while queue:
            level = list(queue)
            levels.append(level)
            next_queue = deque()
            for tid in level:
                for neighbor in adj[tid]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_queue.append(neighbor)
            queue = next_queue

        return levels

    def _execute_level(self, level_ids: list[str], sub_tasks: list[dict]):
        """执行同一层的子任务（并行）"""
        id_to_task = {t["id"]: t for t in sub_tasks}

        if len(level_ids) == 1:
            tid = level_ids[0]
            self._execute_subtask(id_to_task[tid])
            return

        with ThreadPoolExecutor(max_workers=len(level_ids)) as executor:
            futures = {
                executor.submit(self._execute_subtask, id_to_task[tid]): tid
                for tid in level_ids
            }
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Sub-task {tid} failed: {e}")
                    self.results[tid] = f"[Error: {e}]"

    def _execute_subtask(self, task: dict):
        """执行单个子任务"""
        tid = task["id"]
        question = task["question"]
        expert_category = task["expert"]
        depends_on = task.get("depends_on", [])

        # 构建 context（前驱结果）
        context_parts = []
        for dep_id in depends_on:
            if dep_id in self.results:
                context_parts.append(f"[子问题 {dep_id} 的结果]\n{self.results[dep_id]}")
        context = "\n\n".join(context_parts) if context_parts else ""

        expert = ExpertAgent(expert_category, self.config)
        answer, response = expert.answer_subtask(
            title=self.title,
            question=question,
            context=context,
        )

        self.results[tid] = answer
        self.token_usage.append(self._make_usage_record(tid, expert_category, response))
        logger.info(f"Sub-task {tid} ({expert_category}): {len(answer)} chars, {response.total_tokens} tokens")

    def _execute_synthesis(self, synthesis: dict) -> tuple[str, list[dict]]:
        """执行最终汇总"""
        expert_category = synthesis["expert"]
        instruction = synthesis.get("instruction", "请整合以上所有子问题的分析，形成完整、连贯的最终答案。")

        # 构建所有结果作为 context
        context_parts = []
        for tid, result in self.results.items():
            context_parts.append(f"## 子问题 {tid} 的分析结果\n{result}")
        all_context = "\n\n---\n\n".join(context_parts)

        synthesis_prompt = f"{instruction}\n\n{all_context}"

        expert = ExpertAgent(expert_category, self.config)
        answer, response = expert.answer_subtask(
            title=self.title,
            question=synthesis_prompt,
            context="",
        )

        self.token_usage.append(self._make_usage_record("synthesis", expert_category, response))
        return answer, self.token_usage

    def _make_usage_record(self, task_id: str, expert: str, response: LLMResponse) -> dict:
        return {
            "sub_task_id": task_id,
            "expert": expert,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "reasoning_tokens": response.reasoning_tokens,
            "total_tokens": response.total_tokens,
            "latency_ms": response.latency_ms,
        }
