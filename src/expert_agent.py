import logging
from pathlib import Path

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader

logger = logging.getLogger(__name__)

REFINE_PROMPT_INSTRUCTION = """## 任务
请根据评审的反馈，分析当前 Expert 的 system prompt 存在什么不足，并生成一份**改进后的完整 system prompt**。

## 评审反馈
{feedback_text}

## 当前 system prompt
{current_prompt}

## 要求
1. 分析评审反馈暴露了 system prompt 的哪些缺陷
2. 输出改进后的**完整 system prompt**（不是 diff，是完整替换版本）
3. 保留原 prompt 中仍然有效的部分，只针对性地补充和修正
4. 改进应当是通用性的（适用于该领域的所有题目），而非仅针对当前这一道题
5. **严格控制长度在500字以内**，提炼核心规则，删除冗余描述
6. 直接输出改进后的 system prompt 内容，不要加任何前缀说明
"""


class ExpertAgent:
    """领域 Expert Agent：根据问题生成专业回答，并根据反馈优化回复"""

    def __init__(self, category: str, config_loader: ConfigLoader):
        self.category = category
        self.config_loader = config_loader
        expert_config = config_loader.get_expert_config(category)
        self.client = LLMClient(expert_config)
        self.base_system_prompt = expert_config.get("system_prompt", "")
        self.guidelines = expert_config.get("guidelines", "")
        self.name = expert_config.get("name", category)
        self.messages: list[dict] = []

        log_dir = config_loader.settings.get("logging", {}).get("log_dir", "logs")
        self.refined_dir = Path(log_dir) / "refined"

        # 加载 refined prompt（如果存在）
        self.system_prompt = self._load_refined_prompt() or self.base_system_prompt
        self._init_system_message()

    def _get_refined_path(self) -> Path:
        return self.refined_dir / f"{self.category}.txt"

    def _load_refined_prompt(self) -> str | None:
        """从磁盘加载 refined prompt"""
        path = self._get_refined_path()
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                logger.info(f"Loaded refined prompt for {self.category} ({len(content)} chars)")
                return content
        return None

    def _save_refined_prompt(self, prompt: str):
        """将 refined prompt 落盘"""
        path = self._get_refined_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        logger.info(f"Saved refined prompt for {self.category} -> {path}")

    def _init_system_message(self):
        """初始化 system message"""
        full_prompt = self.system_prompt
        # 仅在使用原始 prompt 时拼接 guidelines，refined 版本已包含
        if not self._load_refined_prompt() and self.guidelines:
            full_prompt += f"\n\n## 注意事项\n{self.guidelines}"
        self.messages = [{"role": "system", "content": full_prompt}]

    def generate_answer(self, title: str, content: str) -> tuple[str, LLMResponse]:
        """
        生成初始答案
        返回 (answer_text, llm_response)
        """
        user_content = self._build_initial_prompt(title, content)
        self.messages.append({"role": "user", "content": user_content})

        response = self.client.chat(self.messages)
        answer = response.content

        self.messages.append({"role": "assistant", "content": answer})
        return answer, response

    def generate_reply(self, feedback_text: str) -> tuple[str, LLMResponse]:
        """
        根据评审反馈生成针对性补充回复
        不重写完整答案，只针对反馈点进行补充修正
        """
        reply_prompt = (
            f"## 评审反馈\n{feedback_text}\n\n"
            "## 要求\n"
            "针对评审反馈，给出精准的补充或修正：\n"
            "1. 只回应评审指出的具体问题，不要重复已有内容\n"
            "2. 如有事实性错误，直接给出修正后的正确内容\n"
            "3. 如需补充，直接给出补充内容\n"
            "4. 保持专业严谨，言简意赅"
        )
        self.messages.append({"role": "user", "content": reply_prompt})

        response = self.client.chat(self.messages)
        reply = response.content

        self.messages.append({"role": "assistant", "content": reply})
        return reply, response

    def refine_prompt(self, feedback_text: str) -> tuple[str, LLMResponse]:
        """
        根据评审反馈优化 Expert system prompt
        读取磁盘最新版本作为输入，生成改进版并落盘
        """
        current_prompt = self._load_refined_prompt() or self.base_system_prompt

        prefix_messages = self.messages[:3]
        refine_content = REFINE_PROMPT_INSTRUCTION.format(
            feedback_text=feedback_text,
            current_prompt=current_prompt,
        )
        messages = prefix_messages + [{"role": "user", "content": refine_content}]

        response = self.client.chat(messages)
        refined_prompt = response.content.strip()

        self._save_refined_prompt(refined_prompt)
        self.system_prompt = refined_prompt

        return refined_prompt, response

    def _build_initial_prompt(self, title: str, content: str) -> str:
        parts = [f"## 题目\n{title}\n\n{content}"]
        parts.append(
            "\n\n## 要求\n"
            "请根据题目内容，给出专业、完整、准确的回答。"
            "回答要有条理，论证充分，必要时引用具体数据或来源。"
        )
        return "".join(parts)

    def get_context_token_estimate(self) -> int:
        """粗略估算当前上下文的 token 数"""
        total_chars = sum(len(m.get("content", "")) for m in self.messages)
        return total_chars // 3
