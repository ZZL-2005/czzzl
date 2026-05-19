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
import glob
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

from tqdm import tqdm

from src.config_loader import ConfigLoader
from src.task_worker import TaskWorker
from src.arena_client import ArenaClient
from src.expert_agent import ExpertAgent
from src.paper_cache import PaperCache


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
    print(f"Papers: {result.get('papers_used', 0)}")
    print(f"{'='*60}")
    return result


def _do_batch_refine(config: ConfigLoader, results: list[dict], label: str = ""):
    """按 category 汇总反馈，做一次综合 refine"""
    feedback_by_category: dict[str, list[str]] = {}
    for r in results:
        cat = r.get("category")
        fb = r.get("feedback_text", "")
        if cat and fb:
            feedback_by_category.setdefault(cat, []).append(fb)

    if feedback_by_category:
        print(f"\n[Refine{label}] Refining prompts for {len(feedback_by_category)} categories...")
        for category, feedbacks in feedback_by_category.items():
            combined_feedback = "\n\n---\n\n".join(
                f"[Feedback {i+1}]\n{fb}" for i, fb in enumerate(feedbacks)
            )
            try:
                expert = ExpertAgent(category, config)
                expert.refine_prompt(combined_feedback)
                print(f"  {category}: refined with {len(feedbacks)} feedback(s)")
            except Exception as e:
                print(f"  {category}: refine failed - {e}")


def _find_interrupted_tasks(arena: ArenaClient, tasks: list[dict], log_dir: str) -> list[dict]:
    """找出已提交答案但未完成反馈流程的 task"""
    completed_ids = set()
    for f in glob.glob(f"{log_dir}/task_*.json"):
        with open(f) as fh:
            d = json.load(fh)
            if d.get("status") == "completed":
                completed_ids.add(d["task_id"])

    interrupted = []
    for task in tasks:
        tid = task["task_id"]
        if tid in completed_ids:
            continue
        my_answer = arena.get_my_answer(tid)
        if my_answer and my_answer.get("answer"):
            interrupted.append(task)

    return interrupted


def cmd_process_all(config: ConfigLoader, concurrency: int = 10):
    """并发处理所有未答题目 + 恢复中断任务，每 concurrency 个完成做一次 refine"""
    arena = ArenaClient()
    tasks = arena.get_tasks()
    log_dir = config.settings.get("logging", {}).get("log_dir", "logs")

    # 全局共享的论文缓存
    paper_cache = PaperCache(f"{log_dir}/paper_cache")

    # 检测中断任务（已提交答案但未完成反馈）
    interrupted = _find_interrupted_tasks(arena, tasks, log_dir)

    # 找未答题目（排除已完成和已中断的）
    completed_ids = set()
    for f in glob.glob(f"{log_dir}/task_*.json"):
        with open(f) as fh:
            d = json.load(fh)
            if d.get("status") == "completed":
                completed_ids.add(d["task_id"])

    answered_ids = completed_ids | {t["task_id"] for t in interrupted}
    for task in tasks:
        tid = task["task_id"]
        if tid not in answered_ids:
            my_answer = arena.get_my_answer(tid)
            if my_answer and my_answer.get("answer"):
                answered_ids.add(tid)

    unanswered = [t for t in tasks if t["task_id"] not in answered_ids]
    unanswered.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

    print(f"Total: {len(tasks)}, Completed: {len(completed_ids)}, Interrupted: {len(interrupted)}, To process: {len(unanswered)}, Concurrency: {concurrency}")

    # 构建工作队列：先恢复中断任务，再处理新任务
    work_items = []
    for t in interrupted:
        work_items.append(("resume", t))
    for t in unanswered:
        work_items.append(("new", t))

    results = []
    batch_results = []  # 当前 batch 的 results（用于 refine）
    refine_count = 0

    def process_one(mode: str, task: dict) -> dict:
        worker = TaskWorker(config, paper_cache=paper_cache)
        if mode == "resume":
            return worker.resume_task(task["task_id"])
        return worker.process_task(task["task_id"])

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(process_one, mode, t): (mode, t) for mode, t in work_items}
        with tqdm(total=len(work_items), desc="Tasks", unit="task") as pbar:
            for future in as_completed(futures):
                mode, task = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"task_id": task["task_id"], "status": "failed", "error": str(e)}
                results.append(result)
                batch_results.append(result)
                score = result.get("score", "N/A")
                tag = "[R]" if mode == "resume" else ""
                pbar.set_postfix_str(f"{tag}{task['title'][:28]} → score={score}")
                pbar.update(1)

                # 每 concurrency 个完成做一次 batch refine
                if len(batch_results) >= concurrency:
                    refine_count += 1
                    _do_batch_refine(config, batch_results, f" #{refine_count}")
                    batch_results = []

    # 最后剩余的也做一次 refine
    if batch_results:
        refine_count += 1
        _do_batch_refine(config, batch_results, f" #{refine_count} (final)")

    print(f"\n{'='*60}")
    print(f"Processed {len(results)} tasks")
    succeeded = sum(1 for r in results if r["status"] == "completed")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed: {len(results) - succeeded}")
    total_score = sum(r.get("score", 0) or 0 for r in results)
    print(f"  Total score: {total_score}")
    print(f"  Refine rounds: {refine_count}")


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
