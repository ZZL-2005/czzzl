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
1. 分析评审反馈暴露了 system prompt 的哪些缺陷（比如：缺少某类要求、某个领域覆盖不足、回答风格不符合评审期望等）
2. 输出改进后的**完整 system prompt**（不是 diff，是完整替换版本）
3. 保留原 prompt 中仍然有效的部分，只针对性地补充和修正
4. 改进应当是通用性的（适用于该领域的所有题目），而非仅针对当前这一道题
5. 直接输出改进后的 system prompt 内容，不要加任何前缀说明
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
        if self.guidelines:
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
        根据评审反馈生成改进回复（请求1）
        共享前缀：system + 原题 + 原答案
        """
        reply_prompt = (
            f"## 评审反馈\n{feedback_text}\n\n"
            "## 要求\n"
            "评审对你之前的回答提出了补充要求或修正意见。请重新生成一份**针对原题目的完整作答**：\n"
            "1. 保留初次回答中正确的部分\n"
            "2. 针对评审指出的不足进行补充和完善\n"
            "3. 如果评审指出了事实性错误，在新的完整回答中修正\n"
            "4. 最终输出应该是一份独立可读的、完整的题目解答（不是简短的补充说明）\n"
            "5. 保持专业水准和详细程度，涵盖评审要求的所有要点"
        )
        self.messages.append({"role": "user", "content": reply_prompt})

        response = self.client.chat(self.messages)
        reply = response.content

        self.messages.append({"role": "assistant", "content": reply})
        return reply, response

    def refine_prompt(self, feedback_text: str) -> tuple[str, LLMResponse]:
        """
        根据评审反馈优化 Expert system prompt（请求2）
        共享前缀：system + 原题 + 原答案
        结果落盘，下次同类任务使用改进后的 prompt
        """
        # 使用共享前缀（不包含 generate_reply 追加的内容）
        # 从 messages 中取前缀：system + user(题目) + assistant(答案)
        prefix_messages = self.messages[:3]

        refine_content = REFINE_PROMPT_INSTRUCTION.format(
            feedback_text=feedback_text,
            current_prompt=self.system_prompt,
        )
        messages = prefix_messages + [{"role": "user", "content": refine_content}]

        response = self.client.chat(messages)
        refined_prompt = response.content.strip()

        # 落盘
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
