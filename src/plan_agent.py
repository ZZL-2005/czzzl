import json
import logging
from pathlib import Path

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务分解专家。你的职责是将复杂问题拆解为若干子问题，形成有向无环图（DAG）结构。

## 可用的 Expert 领域
- natural_science（自然科学：化学、生物、物理、数学、通信）
- law（法律事务：刑事、民事、商事、知识产权、国际法）
- finance（金融分析：股票、VC/PE、基金、消费金融、并购）
- industrial_engineering（工业工程：建筑、土木、软件开发、嵌入式、3D）
- medical_health（医疗健康：外科、内科、妇产科、病理）

## 分解规则
1. 分析题目，判断是否需要分解。简单单一问题不需要分解。
2. 每个子问题必须明确指定由哪个 expert 处理。
3. 子问题之间可以有依赖关系（前驱结果作为后继的输入）。
4. 最终需要一个 synthesis 步骤整合所有子问题的答案。
5. 子问题数量控制在 2-5 个，避免过度分解。
6. 跨领域题目可以分配给不同 expert。

## 输出格式（严格 JSON）
{
  "decomposition_mode": "模式标签（如：single_direct, multi_step_reasoning, data_extraction_then_analysis, multi_perspective_comparison, cross_domain_synthesis）",
  "sub_tasks": [
    {
      "id": "A",
      "question": "具体的子问题描述，要足够清晰让 expert 独立回答",
      "expert": "expert领域标识",
      "depends_on": []
    }
  ],
  "final_synthesis": {
    "expert": "负责汇总的expert领域标识",
    "instruction": "汇总指令，说明如何整合各子问题答案"
  }
}

## 简单题目（不需分解）
如果题目是单一明确的问题，输出 single_direct 模式：
{
  "decomposition_mode": "single_direct",
  "sub_tasks": [
    {"id": "A", "question": "原始完整问题", "expert": "对应领域", "depends_on": []}
  ],
  "final_synthesis": null
}

{strategy_section}"""

STRATEGY_SECTION_TEMPLATE = """
## 历史经验（从过往执行中学到的分解策略）
{strategies}
"""


class PlanAgent:
    """Plan Agent：分析题目并生成 DAG 分解方案"""

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        plan_config = config_loader.get_plan_agent_config()
        self.client = LLMClient(plan_config)
        self.strategy_index = self._load_strategy_index()

    def _load_strategy_index(self) -> dict:
        log_dir = self.config_loader.settings.get("logging", {}).get("log_dir", "logs")
        index_path = Path(log_dir) / "strategies" / "index.json"
        if index_path.exists():
            try:
                content = index_path.read_text(encoding="utf-8")
                data = json.loads(content)
                logger.info(f"Loaded strategy index with {len(data.get('modes', {}))} modes")
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load strategy index: {e}")
        return {}

    def _build_strategy_section(self) -> str:
        modes = self.strategy_index.get("modes", {})
        if not modes:
            return ""
        lines = []
        for mode_name, mode_data in modes.items():
            desc = mode_data.get("description", "")
            applicable = mode_data.get("applicable_to", "")
            lessons = mode_data.get("lessons", [])
            avg_score = mode_data.get("avg_score", 0)
            lines.append(f"- **{mode_name}** (avg score: {avg_score:.1f}): {desc}")
            if applicable:
                lines.append(f"  适用: {applicable}")
            if lessons:
                lines.append(f"  经验: {'; '.join(lessons[:3])}")
        return STRATEGY_SECTION_TEMPLATE.format(strategies="\n".join(lines))

    def analyze(self, title: str, content: str) -> tuple[dict, LLMResponse]:
        """
        分析题目，生成 DAG 分解方案
        返回 (plan_result, llm_response)
        plan_result 结构: {decomposition_mode, sub_tasks, final_synthesis}
        """
        strategy_section = self._build_strategy_section()
        system_prompt = PLAN_SYSTEM_PROMPT.replace("{strategy_section}", strategy_section)

        user_message = f"## 题目标题\n{title}\n\n## 题目正文\n{content}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        plan_result, response = self.client.chat_json(messages)

        # 校验输出结构
        plan_result = self._validate_plan(plan_result)

        return plan_result, response

    def _validate_plan(self, plan: dict) -> dict:
        valid_categories = self.config_loader.get_valid_categories()
        default_category = self.config_loader.get_default_category()

        if "decomposition_mode" not in plan:
            plan["decomposition_mode"] = "single_direct"

        if "sub_tasks" not in plan or not plan["sub_tasks"]:
            plan["sub_tasks"] = [{"id": "A", "question": "", "expert": default_category, "depends_on": []}]
            plan["decomposition_mode"] = "single_direct"

        # 校验每个子任务
        seen_ids = set()
        for task in plan["sub_tasks"]:
            if "id" not in task:
                task["id"] = chr(65 + len(seen_ids))
            seen_ids.add(task["id"])

            if task.get("expert") not in valid_categories:
                task["expert"] = default_category
                logger.warning(f"Invalid expert '{task.get('expert')}' in sub_task, using default")

            if "depends_on" not in task:
                task["depends_on"] = []

            # 验证依赖的 id 存在
            task["depends_on"] = [dep for dep in task["depends_on"] if dep in seen_ids or dep in [t["id"] for t in plan["sub_tasks"]]]

        # 校验 final_synthesis
        if plan.get("final_synthesis"):
            synth = plan["final_synthesis"]
            if synth.get("expert") not in valid_categories:
                synth["expert"] = plan["sub_tasks"][-1]["expert"]

        # 单子任务时清除 synthesis
        if len(plan["sub_tasks"]) == 1:
            plan["final_synthesis"] = None
            plan["decomposition_mode"] = "single_direct"

        return plan

    def get_category(self, plan: dict) -> str:
        """从分解方案中提取主要 category（用于日志记录）"""
        experts_used = [t["expert"] for t in plan.get("sub_tasks", [])]
        if plan.get("final_synthesis"):
            experts_used.append(plan["final_synthesis"]["expert"])
        if experts_used:
            from collections import Counter
            return Counter(experts_used).most_common(1)[0][0]
        return self.config_loader.get_default_category()
