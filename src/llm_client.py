import time
import logging
from dataclasses import dataclass, field
from openai import OpenAI

from .exceptions import RetryableError, retry_with_backoff

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 调用返回结构"""
    content: str
    reasoning: str = ""
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    raw_response: dict = field(default_factory=dict)

    @property
    def input_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0) or self.usage.get("input_tokens", 0)

    @property
    def output_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0) or self.usage.get("output_tokens", 0)

    @property
    def reasoning_tokens(self) -> int:
        detail = self.usage.get("completion_tokens_details", {}) or {}
        return detail.get("reasoning_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.usage.get("total_tokens", 0) or (self.input_tokens + self.output_tokens)


class LLMClient:
    """OpenAI SDK 格式的 LLM 客户端封装"""

    def __init__(self, config: dict):
        self.config = config
        self.client = OpenAI(
            base_url=config.get("base_url"),
            api_key=config.get("api_key"),
            timeout=config.get("timeout_seconds", 120),
        )
        self.model = config.get("model", "")
        self.temperature = config.get("temperature", 0.3)
        self.max_tokens = config.get("max_tokens", 4096)
        self.reasoning_enabled = config.get("reasoning", {}).get("enabled", False)
        self.reasoning_params = config.get("reasoning", {}).get("extra_params", {})

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        """调用 chat/completions，返回结构化响应"""
        start_time = time.time()

        request_params = {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if self.reasoning_enabled and self.reasoning_params:
            request_params.update(self.reasoning_params)

        for key in ["response_format", "tools", "tool_choice"]:
            if key in kwargs:
                request_params[key] = kwargs[key]

        try:
            response = self.client.chat.completions.create(**request_params)
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "rate" in error_msg.lower():
                raise RetryableError(error_msg, status_code=429)
            if any(code in error_msg for code in ["500", "502", "503", "504"]):
                raise RetryableError(error_msg, status_code=500)
            raise

        latency_ms = int((time.time() - start_time) * 1000)

        choice = response.choices[0]
        content = choice.message.content or ""
        reasoning = ""

        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
            reasoning = choice.message.reasoning_content
        elif hasattr(choice.message, "reasoning") and choice.message.reasoning:
            reasoning = choice.message.reasoning

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            if hasattr(response.usage, "completion_tokens_details") and response.usage.completion_tokens_details:
                usage["completion_tokens_details"] = {
                    "reasoning_tokens": getattr(response.usage.completion_tokens_details, "reasoning_tokens", 0) or 0,
                }

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            usage=usage,
            latency_ms=latency_ms,
            raw_response={"id": response.id, "model": response.model},
        )

    def chat_json(self, messages: list[dict], **kwargs) -> tuple[dict, LLMResponse]:
        """强制 JSON 格式输出，返回 (parsed_json, raw_response)"""
        import json

        kwargs["response_format"] = {"type": "json_object"}
        response = self.chat(messages, **kwargs)

        try:
            parsed = json.loads(response.content)
        except json.JSONDecodeError:
            content = response.content
            start = content.find("{")
            end = content.rfind("}") + 1
            if start != -1 and end > start:
                parsed = json.loads(content[start:end])
            else:
                logger.error(f"Failed to parse JSON from LLM response: {content[:200]}")
                raise

        return parsed, response
