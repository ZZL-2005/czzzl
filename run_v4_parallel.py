#!/usr/bin/env python3
"""
V4 并行处理脚本
将任务分成多组，并行执行检索+答题。共享同一个 paper_cache。

用法:
  python run_v4_parallel.py --workers 4         4路并行处理所有未答题
  python run_v4_parallel.py --workers 4 --limit 20
"""

import argparse
import json
import logging
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from src.config_loader import ConfigLoader
from src.arena_client import ArenaClient
from src.plan_agent import PlanAgent
from src.expert_agent import ExpertAgent
from src.retriever import AcademicRetriever, Paper
from src.paper_reader import PaperReader
from src.paper_cache import PaperCache
from src.llm_client import LLMClient

from run_v4 import process_single_task, save_result, TaskResult, print_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logv4/runtime.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Thread-safe counter
class ProgressCounter:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.submitted = 0
        self.failed = 0
        self.lock = threading.Lock()

    def increment(self, submitted=False, failed=False):
        with self.lock:
            self.done += 1
            if submitted:
                self.submitted += 1
            if failed:
                self.failed += 1
            return self.done

    def status(self):
        with self.lock:
            return f"{self.done}/{self.total} done, {self.submitted} submitted, {self.failed} failed"


def worker_process_task(task, config, cache, output_dir, counter):
    """单个worker处理一个task"""
    task_id = task["task_id"]
    title = task["title"][:40]

    retriever = AcademicRetriever(config.settings.get("retrieval", {}))
    reader = PaperReader(config)
    llm = LLMClient(config.get_plan_agent_config())
    arena = ArenaClient()

    try:
        result = process_single_task(
            task_id, config, cache, retriever, reader, llm, arena,
            retrieval_only=False,
        )
        save_result(result, output_dir)

        n = counter.increment(submitted=result.answer_submitted)
        logger.info(f"[Progress {counter.status()}] Done: {title}")
        return result

    except Exception as e:
        n = counter.increment(failed=True)
        logger.error(f"[Progress {counter.status()}] Failed: {title} - {e}")
        return TaskResult(
            task_id=task_id, title=task["title"],
            category="unknown", status=f"error: {str(e)[:100]}",
        )
    finally:
        retriever.close()


def main():
    parser = argparse.ArgumentParser(description="V4 并行处理")
    parser.add_argument("--workers", type=int, default=4, help="并行worker数")
    parser.add_argument("--limit", type=int, default=None, help="最多处理N个")
    parser.add_argument("--output-dir", type=str, default="logv4", help="输出目录")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    config = ConfigLoader("config")
    arena = ArenaClient()
    cache = PaperCache(f"{args.output_dir}/paper_cache")

    # Get unanswered tasks
    tasks = arena.get_tasks()
    tasks.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

    to_process = []
    for t in tasks:
        my_answer = arena.get_my_answer(t["task_id"])
        if not my_answer or my_answer.get("answer", {}).get("score") is None:
            to_process.append(t)

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"Tasks to process: {len(to_process)} with {args.workers} workers")
    print(f"Cache status: {cache.stats()}")
    print()

    counter = ProgressCounter(len(to_process))
    results = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(worker_process_task, task, config, cache, args.output_dir, counter): task
            for task in to_process
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    # Summary
    print_summary(results)

    # Save summary
    summary_path = Path(args.output_dir) / "run_summary_parallel.json"
    summary = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workers": args.workers,
        "tasks_processed": len(results),
        "tasks_submitted": sum(1 for r in results if r.answer_submitted),
        "tasks_failed": sum(1 for r in results if "error" in r.status),
        "cache_stats": cache.stats(),
        "total_tokens": sum(r.tokens_total for r in results),
        "results": [asdict(r) for r in results],
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nSummary saved: {summary_path}")


if __name__ == "__main__":
    main()
