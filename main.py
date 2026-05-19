#!/usr/bin/env python3
"""
Arena Agent 主入口
用法：
  python main.py --task <task_id>         处理单个任务
  python main.py --all                    处理所有未答题目
  python main.py --list                   列出所有题目
  python main.py --status                 查看自身状态
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

from tqdm import tqdm

from src.config_loader import ConfigLoader
from src.task_worker import TaskWorker
from src.arena_client import ArenaClient


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


def cmd_process_task(task_id: str, config: ConfigLoader):
    """处理单个任务"""
    worker = TaskWorker(config)
    result = worker.process_task(task_id)
    print(f"\n{'='*60}")
    print(f"Task: {result.get('title', task_id[:8])}")
    print(f"Status: {result.get('status')}")
    print(f"Score: {result.get('score', 'N/A')}")
    print(f"Tokens: {result.get('tokens_used', 0)}")
    print(f"{'='*60}")
    return result


def cmd_process_all(config: ConfigLoader, concurrency: int = 10):
    """并发处理所有未答题目"""
    arena = ArenaClient()
    tasks = arena.get_tasks()

    answered = set()
    for task in tasks:
        tid = task["task_id"]
        my_answer = arena.get_my_answer(tid)
        if my_answer and my_answer.get("answer"):
            answered.add(tid)

    unanswered = [t for t in tasks if t["task_id"] not in answered]
    unanswered.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

    print(f"Total: {len(tasks)}, Answered: {len(answered)}, To process: {len(unanswered)}, Concurrency: {concurrency}")

    results = []

    def process_one(task: dict) -> dict:
        worker = TaskWorker(config)
        return worker.process_task(task["task_id"])

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(process_one, t): t for t in unanswered}
        with tqdm(total=len(unanswered), desc="Tasks", unit="task") as pbar:
            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"task_id": task["task_id"], "status": "failed", "error": str(e)}
                results.append(result)
                score = result.get("score", "N/A")
                pbar.set_postfix_str(f"{task['title'][:30]} → score={score}")
                pbar.update(1)

    print(f"\n{'='*60}")
    print(f"Processed {len(results)} tasks")
    succeeded = sum(1 for r in results if r["status"] == "completed")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed: {len(results) - succeeded}")
    total_score = sum(r.get("score", 0) or 0 for r in results)
    print(f"  Total score: {total_score}")


def cmd_list_tasks(config: ConfigLoader):
    """列出所有题目及作答状态"""
    arena = ArenaClient()
    tasks = arena.get_tasks()
    tasks.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

    print(f"{'ID':<38} {'Reward':>6} {'Answers':>7} {'Title'}")
    print("-" * 90)
    for t in tasks:
        print(f"{t['task_id']:<38} {t['reward_pool']:>6} {t['answer_count']:>7} {t['title']}")

    print(f"\nTotal: {len(tasks)} tasks")


def cmd_status(config: ConfigLoader):
    """查看自身状态"""
    arena = ArenaClient()
    me = arena.get_me()
    lb = arena.get_leaderboard()

    print(f"Display Name: {me['display_name']}")
    print(f"Status: {me['status']}")
    print(f"Wallet Balance: {me['wallet_balance']}")
    print(f"Total Score: {me['total_score']}")
    print(f"Token Used: {me['token_used']}")

    my_rank = next((p["rank"] for p in lb if p["participant_id"] == me["participant_id"]), "?")
    print(f"Rank: #{my_rank} / {len(lb)}")


def main():
    parser = argparse.ArgumentParser(description="Arena Agent - 自动答题系统")
    parser.add_argument("--task", type=str, help="处理指定 task_id")
    parser.add_argument("--all", action="store_true", help="处理所有未答题目")
    parser.add_argument("--list", action="store_true", help="列出所有题目")
    parser.add_argument("--status", action="store_true", help="查看自身状态")
    parser.add_argument("--config-dir", type=str, default="config", help="配置目录路径")
    parser.add_argument("--output-dir", type=str, default=None, help="统一输出目录（日志、快照、refined prompts 等）")
    parser.add_argument("--log-level", type=str, default=None, help="日志级别覆盖")
    parser.add_argument("--concurrency", type=int, default=10, help="并发数（默认10）")
    args = parser.parse_args()

    config = ConfigLoader(args.config_dir)
    log_level = args.log_level or config.settings.get("logging", {}).get("level", "INFO")
    output_dir = args.output_dir or config.settings.get("logging", {}).get("log_dir", "logs")

    # 将 output_dir 注入配置，确保所有组件使用统一路径
    if "logging" not in config.settings:
        config.settings["logging"] = {}
    config.settings["logging"]["log_dir"] = output_dir

    setup_logging(log_level, output_dir)

    if args.task:
        cmd_process_task(args.task, config)
    elif args.all:
        cmd_process_all(config, concurrency=args.concurrency)
    elif args.list:
        cmd_list_tasks(config)
    elif args.status:
        cmd_status(config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
