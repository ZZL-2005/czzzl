import logging

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务分类专家。你的唯一职责是判断题目属于哪个领域。

必须从以下五个领域中选一个：
- natural_science（自然科学：化学、生物、物理、数学、通信）
- law（法律事务：刑事、民事、商事、知识产权、国际法）
- finance（金融分析：股票、VC/PE、基金、消费金融、并购）
- industrial_engineering（工业工程：建筑、土木、软件开发、嵌入式、3D）
- medical_health（医疗健康：外科、内科、妇产科、病理）

你必须以 JSON 格式输出：
{
  "category": "五大领域之一的英文标识",
  "reasoning": "简短的分类理由"
}

注意：category 必须是五个选项之一，不可自创。
"""


class PlanAgent:
    """Plan Agent：分析题目并分类到对应领域"""

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        plan_config = config_loader.get_plan_agent_config()
        self.client = LLMClient(plan_config)

    def analyze(self, title: str, content: str) -> tuple[dict, LLMResponse]:
        """
        分析题目，返回 (plan_result, llm_response)
        plan_result 结构: {category, reasoning}
        """
        user_message = f"## 题目标题\n{title}\n\n## 题目正文\n{content}"

        messages = [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        plan_result, response = self.client.chat_json(messages)

        valid_categories = self.config_loader.get_valid_categories()
        default_category = self.config_loader.get_default_category()

        if "category" not in plan_result or plan_result["category"] not in valid_categories:
            old_val = plan_result.get("category", "<missing>")
            plan_result["category"] = default_category
            logger.warning(f"Plan Agent returned invalid category '{old_val}', falling back to: {default_category}")

        if "reasoning" not in plan_result:
            plan_result["reasoning"] = response.reasoning or ""

        return plan_result, response
