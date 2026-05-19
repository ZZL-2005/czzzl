import json
import logging
from pathlib import Path

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader

logger = logging.getLogger(__name__)

LESSON_EXTRACT_PROMPT = """你是一个策略分析专家。根据以下信息，提取一条关于"如何分解问题"的通用经验教训。

## 题目
{title}

## 使用的分解模式
{decomposition_mode}

## 子任务结构
{dag_structure}

## 得分
{score}

## 评审反馈
{feedback}

## 要求
1. 总结一条通用的分解策略教训（不针对具体题目内容，而是针对"如何分解此类问题"）
2. 如果反馈暴露了分解策略的不足，指出应如何改进
3. 如果分解策略本身合理（高分），总结为什么这个分解方式有效
4. 控制在50字以内，一句话概括

直接输出教训内容，不要任何前缀。"""


class StrategyLearner:
    """策略学习器：从执行反馈中学习分解方法论"""

    def __init__(self, config_loader: ConfigLoader):
        self.config = config_loader
        plan_config = config_loader.get_plan_agent_config()
        self.client = LLMClient(plan_config)
        log_dir = config_loader.settings.get("logging", {}).get("log_dir", "logs")
        self.strategies_dir = Path(log_dir) / "strategies"
        self.strategies_dir.mkdir(parents=True, exist_ok=True)

    def learn_from_result(self, result: dict) -> dict | None:
        """
        从单个 task 执行结果生成策略文件
        result 需要包含: task_id, title, decomposition_mode, plan_result, score, feedback_text
        """
        task_id = result.get("task_id", "")
        if not task_id or not result.get("feedback_text"):
            return None

        plan_result = result.get("plan_result", {})
        sub_tasks = plan_result.get("sub_tasks", [])
        dag_structure = self._describe_dag(sub_tasks)
        experts_used = list(set(t.get("expert", "") for t in sub_tasks))

        # 用 LLM 提取教训
        lesson = self._extract_lesson(
            title=result.get("title", ""),
            decomposition_mode=result.get("decomposition_mode", "single_direct"),
            dag_structure=dag_structure,
            score=result.get("score", 0),
            feedback=result.get("feedback_text", ""),
        )

        strategy = {
            "task_id": task_id,
            "title": result.get("title", ""),
            "decomposition_mode": result.get("decomposition_mode", "single_direct"),
            "sub_task_count": len(sub_tasks),
            "dag_structure": dag_structure,
            "experts_used": experts_used,
            "score": result.get("score", 0),
            "feedback_summary": result.get("feedback_text", "")[:200],
            "lesson": lesson,
        }

        # 落盘
        path = self.strategies_dir / f"{task_id}.json"
        path.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Strategy saved: {path.name} (mode={strategy['decomposition_mode']})")
        return strategy

    def update_index(self):
        """读取所有策略文件，更新 index.json（浓缩摘要）"""
        strategies = []
        for f in self.strategies_dir.glob("*.json"):
            if f.name == "index.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                strategies.append(data)
            except (json.JSONDecodeError, OSError):
                continue

        if not strategies:
            return

        # 按 decomposition_mode 聚合
        modes: dict[str, dict] = {}
        for s in strategies:
            mode = s.get("decomposition_mode", "unknown")
            if mode not in modes:
                modes[mode] = {
                    "description": "",
                    "applicable_to": "",
                    "scores": [],
                    "lessons": [],
                    "examples": [],
                }
            modes[mode]["scores"].append(s.get("score", 0))
            if s.get("lesson"):
                modes[mode]["lessons"].append(s["lesson"])
            modes[mode]["examples"].append(s.get("title", "")[:50])

        # 构建索引
        index = {"modes": {}}
        for mode_name, mode_data in modes.items():
            scores = mode_data["scores"]
            lessons = mode_data["lessons"]
            avg_score = sum(scores) / len(scores) if scores else 0

            # 只保留最有价值的 lessons（去重，限制数量）
            unique_lessons = list(dict.fromkeys(lessons))[:5]

            index["modes"][mode_name] = {
                "description": self._infer_mode_description(mode_name),
                "applicable_to": ", ".join(mode_data["examples"][:3]),
                "avg_score": round(avg_score, 1),
                "count": len(scores),
                "lessons": unique_lessons,
            }

        # 限制模式数量（保留最常用的 20 个）
        if len(index["modes"]) > 20:
            sorted_modes = sorted(index["modes"].items(), key=lambda x: x[1]["count"], reverse=True)
            index["modes"] = dict(sorted_modes[:20])

        # 落盘
        index_path = self.strategies_dir / "index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Strategy index updated: {len(index['modes'])} modes, {len(strategies)} strategies")

    def _extract_lesson(self, title: str, decomposition_mode: str, dag_structure: str, score: float, feedback: str) -> str:
        """用 LLM 从反馈中提取一条策略教训"""
        try:
            prompt = LESSON_EXTRACT_PROMPT.format(
                title=title,
                decomposition_mode=decomposition_mode,
                dag_structure=dag_structure,
                score=score,
                feedback=feedback[:300],
            )
            messages = [{"role": "user", "content": prompt}]
            response = self.client.chat(messages, max_tokens=200)
            return response.content.strip()[:100]
        except Exception as e:
            logger.warning(f"Lesson extraction failed: {e}")
            return ""

    def _describe_dag(self, sub_tasks: list[dict]) -> str:
        """描述 DAG 结构为简短字符串"""
        if not sub_tasks:
            return "empty"
        parts = []
        for t in sub_tasks:
            deps = t.get("depends_on", [])
            if deps:
                parts.append(f"{','.join(deps)}->{t['id']}")
            else:
                parts.append(t["id"])
        return "; ".join(parts)

    def _infer_mode_description(self, mode_name: str) -> str:
        """根据模式名推断描述"""
        descriptions = {
            "single_direct": "单一问题直接回答，无需分解",
            "multi_step_reasoning": "多步推理，后续步骤依赖前置结论",
            "data_extraction_then_analysis": "先提取数据/事实，再做分析推理",
            "multi_perspective_comparison": "从多个视角/法域/理论对比分析",
            "cross_domain_synthesis": "跨领域协作，不同expert负责不同维度",
        }
        return descriptions.get(mode_name, f"自动识别的分解模式: {mode_name}")
