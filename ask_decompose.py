#!/usr/bin/env python3
"""
直接向 LLM 询问如何分解任务，查看分解方案
用法：
  python ask_decompose.py <task_id>        指定任务
  python ask_decompose.py --all            所有未完成任务
  python ask_decompose.py --sample 5       随机抽5个
"""

import argparse
import json
import sys

from src.config_loader import ConfigLoader
from src.arena_client import ArenaClient
from src.llm_client import LLMClient

DECOMPOSE_PROMPT = """你是一个任务分解专家。请分析以下题目，告诉我应该如何分解这个问题来获得最佳回答。

## 可用的 Expert 领域
- natural_science（自然科学：化学、生物、物理、数学、通信）
- law（法律事务：刑事、民事、商事、知识产权、国际法）
- finance（金融分析：股票、VC/PE、基金、消费金融、并购）
- industrial_engineering（工业工程：建筑、土木、软件开发、嵌入式、3D）
- medical_health（医疗健康：外科、内科、妇产科、病理）

## 题目标题
{title}

## 题目正文
{content}

## 请回答以下问题
1. 这道题的核心难点是什么？
2. 建议的分解模式是什么？（single_direct / multi_step_reasoning / data_extraction_then_analysis / multi_perspective_comparison / cross_domain_synthesis）
3. 如果需要分解，应该拆成哪些子问题？每个子问题分配给哪个 expert？依赖关系是什么？
4. 最终如何汇总（synthesis instruction）？

请用 JSON 格式输出分解方案，同时用中文简要解释你的分解理由。"""


def ask_one(client: LLMClient, arena: ArenaClient, task_id: str):
    """对单个任务询问分解方案"""
    task_detail = arena.get_task_detail(task_id)
    title = task_detail.get("title", "")
    content = ""
    agora_post = task_detail.get("agora_post", {})
    if agora_post:
        content = agora_post.get("description", "") or agora_post.get("content", "")

    prompt = DECOMPOSE_PROMPT.format(title=title, content=content[:3000])
    messages = [{"role": "user", "content": prompt}]

    print(f"\n{'='*70}")
    print(f"Task: {title}")
    print(f"ID:   {task_id}")
    print(f"{'='*70}")

    response = client.chat(messages)
    print(response.content)
    print(f"\n[tokens: in={response.input_tokens}, out={response.output_tokens}, latency={response.latency_ms}ms]")
    return response.content


def main():
    parser = argparse.ArgumentParser(description="向LLM询问任务分解方案")
    parser.add_argument("task_id", nargs="?", help="指定 task_id")
    parser.add_argument("--all", action="store_true", help="所有未完成任务")
    parser.add_argument("--sample", type=int, help="随机抽N个任务")
    parser.add_argument("--config-dir", default="config", help="配置目录")
    args = parser.parse_args()

    config = ConfigLoader(args.config_dir)
    plan_config = config.get_plan_agent_config()
    client = LLMClient(plan_config)
    arena = ArenaClient()

    if args.task_id:
        ask_one(client, arena, args.task_id)
    elif args.all or args.sample:
        tasks = arena.get_tasks()
        if args.sample:
            import random
            tasks = random.sample(tasks, min(args.sample, len(tasks)))
        for task in tasks:
            try:
                ask_one(client, arena, task["task_id"])
            except Exception as e:
                print(f"\n[ERROR] {task['task_id'][:8]}: {e}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
