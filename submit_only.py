#!/usr/bin/env python3
"""
纯提交模式：拿题 → 分类 → Expert作答 → 提交，不做反馈循环/refine/策略学习
用法：
  python submit_only.py --all                    处理所有未提交题目
  python submit_only.py --all --concurrency 5    控制并发
  python submit_only.py --task <task_id>         处理单个任务
  python submit_only.py --test 2                 测试模式（只跑2个）
"""

import argparse
import glob
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from src.config_loader import ConfigLoader
from src.arena_client import ArenaClient
from src.plan_agent import PlanAgent
from src.expert_agent import ExpertAgent


def setup_logging(level: str = "INFO", log_dir: str = "logs"):
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path / "runtime.log", encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)


def process_task(task_id: str, config: ConfigLoader) -> dict:
    """处理单个任务：分类 → 作答 → 提交"""
    arena = ArenaClient()

    # 获取题目
    task_detail = arena.get_task_detail(task_id)
    title = task_detail.get("title", "")
    content = ""
    agora_post = task_detail.get("agora_post", {})
    if agora_post:
        content = agora_post.get("description", "") or agora_post.get("content", "")

    logger.info(f"[{task_id[:8]}] Title: {title}")

    # Plan Agent 分类
    plan_agent = PlanAgent(config)
    plan_result, plan_response = plan_agent.analyze(title, content)
    category = plan_result["category"]
    logger.info(f"[{task_id[:8]}] Category: {category}")

    # Expert Agent 作答
    expert = ExpertAgent(category, config)
    answer, expert_response = expert.generate_answer(title, content)
    logger.info(f"[{task_id[:8]}] Answer length: {len(answer)}")

    # 提交
    try:
        submit_result = arena.submit_answer(task_id, answer)
        logger.info(f"[{task_id[:8]}] Submitted")
    except Exception as e:
        if "409" in str(e):
            logger.info(f"[{task_id[:8]}] Already submitted (409)")
        else:
            raise

    total_tokens = plan_response.total_tokens + expert_response.total_tokens
    return {
        "task_id": task_id,
        "title": title,
        "category": category,
        "status": "submitted",
        "tokens_used": total_tokens,
        "answer_length": len(answer),
    }


def main():
    parser = argparse.ArgumentParser(description="Arena Agent - 纯提交模式（无反馈循环）")
    parser.add_argument("--task", type=str, help="处理指定 task_id")
    parser.add_argument("--all", action="store_true", help="处理所有未提交题目")
    parser.add_argument("--test", type=int, nargs="?", const=2, help="测试模式（默认2个）")
    parser.add_argument("--concurrency", type=int, default=None, help="并发数（默认全并发）")
    parser.add_argument("--config-dir", type=str, default="config", help="配置目录")
    parser.add_argument("--output-dir", type=str, default="logs_submit", help="输出目录")
    args = parser.parse_args()

    config = ConfigLoader(args.config_dir)
    if "logging" not in config.settings:
        config.settings["logging"] = {}
    config.settings["logging"]["log_dir"] = args.output_dir

    setup_logging("DEBUG", args.output_dir)

    if args.task:
        result = process_task(args.task, config)
        print(f"Done: {result['title']} | category={result['category']} | tokens={result['tokens_used']}")
        return

    # 获取所有任务，排除已提交的
    arena = ArenaClient()
    print("Fetching task list...")
    tasks = arena.get_tasks()
    print(f"Found {len(tasks)} tasks. Checking submission status...")

    unanswered = []
    for task in tqdm(tasks, desc="Scanning", unit="task"):
        my_answer = arena.get_my_answer(task["task_id"])
        if not my_answer or not my_answer.get("answer"):
            unanswered.append(task)

    unanswered.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

    # limit
    if args.test is not None:
        unanswered = unanswered[:args.test]
    elif not args.all:
        parser.print_help()
        return

    total = len(unanswered)
    workers = args.concurrency or total
    print(f"Tasks to submit: {total}, Concurrency: {workers}")

    if not unanswered:
        print("Nothing to submit.")
        return

    results = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_task, t["task_id"], config): t for t in unanswered}
        with tqdm(total=total, desc="Submitting", unit="task") as pbar:
            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"task_id": task["task_id"], "title": task["title"], "status": "failed", "error": str(e)}
                results.append(result)
                status = result.get("status", "?")
                pbar.set_postfix_str(f"{task['title'][:30]} → {status}")
                pbar.update(1)

    # 保存结果
    report_path = Path(args.output_dir) / "submit_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    submitted = sum(1 for r in results if r["status"] == "submitted")
    failed = sum(1 for r in results if r["status"] == "failed")
    total_tokens = sum(r.get("tokens_used", 0) for r in results)
    print(f"Submitted: {submitted}, Failed: {failed}, Total tokens: {total_tokens}")
    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()
