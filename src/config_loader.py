import os
import yaml
from pathlib import Path
from typing import Any


class ConfigLoader:
    """加载和管理所有 YAML 配置"""

    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = Path(__file__).parent.parent / "config"
        self.config_dir = Path(config_dir)
        self._cache: dict[str, Any] = {}

    def _load_yaml(self, filename: str) -> dict:
        if filename in self._cache:
            return self._cache[filename]
        filepath = self.config_dir / filename
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self._cache[filename] = data
        return data

    @property
    def settings(self) -> dict:
        return self._load_yaml("settings.yaml")

    @property
    def llm(self) -> dict:
        return self._load_yaml("llm.yaml")

    @property
    def experts(self) -> dict:
        return self._load_yaml("experts.yaml")

    @property
    def categories(self) -> dict:
        return self._load_yaml("categories.yaml")

    def get_expert_config(self, category: str) -> dict:
        """获取指定领域的 Expert 完整配置（合并默认配置）"""
        default = self.llm.get("expert_agent_default", {}).copy()
        expert = self.experts.get("experts", {}).get(category, {})
        for key in ["base_url", "api_key", "model", "temperature", "max_tokens", "timeout_seconds", "reasoning"]:
            if key in expert:
                default[key] = expert[key]
        default["system_prompt"] = expert.get("system_prompt", "")
        default["guidelines"] = expert.get("guidelines", "")
        default["name"] = expert.get("name", category)
        return default

    def get_plan_agent_config(self) -> dict:
        return self.llm.get("plan_agent", {})

    def get_valid_categories(self) -> list[str]:
        return self.categories.get("valid_categories", [
            "natural_science", "law", "finance", "industrial_engineering", "medical_health"
        ])

    def get_default_category(self) -> str:
        return self.categories.get("default_category", "natural_science")

    def get_retry_config(self) -> dict:
        return self.settings.get("retry", {})

    def get_polling_config(self) -> dict:
        return self.settings.get("polling", {})

    def get_limits(self) -> dict:
        return self.settings.get("limits", {})

    def reload(self):
        """清除缓存，重新加载配置"""
        self._cache.clear()
