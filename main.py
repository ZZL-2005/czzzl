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
from src.strategy_learner import StrategyLearner


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


def _do_strategy_learning(config: ConfigLoader, results: list[dict]):
    """从已完成任务的结果中学习分解策略"""
    learnable = [r for r in results if r.get("status") == "completed" and r.get("feedback_text") and r.get("plan_result")]
    if not learnable:
        return

    learner = StrategyLearner(config)
    learned = 0
    for r in learnable:
        try:
            strategy = learner.learn_from_result(r)
            if strategy:
                learned += 1
        except Exception as e:
            print(f"  Strategy learning failed for {r['task_id'][:8]}: {e}")

    if learned:
        learner.update_index()
        print(f"[Strategy] Learned {learned} strategies, index updated.")


def _find_interrupted_tasks(arena: ArenaClient, tasks: list[dict], log_dir: str, limit: int | None = None) -> list[dict]:
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
            if limit and len(interrupted) >= limit:
                break

    return interrupted


def cmd_process_all(config: ConfigLoader, concurrency: int | None = None, limit: int | None = None):
    """并发处理所有未答题目 + 恢复中断任务"""
    arena = ArenaClient()
    tasks = arena.get_tasks()
    log_dir = config.settings.get("logging", {}).get("log_dir", "logs")

    # 检测中断任务（已提交答案但未完成反馈）
    interrupted = _find_interrupted_tasks(arena, tasks, log_dir, limit=limit)

    # 如果 limit 模式且中断任务已经够数，跳过遍历未答题目
    work_items = []
    for t in interrupted:
        work_items.append(("resume", t))

    remaining = (limit - len(work_items)) if limit else None

    if remaining is None or remaining > 0:
        # 找未答题目
        completed_ids = set()
        for f in glob.glob(f"{log_dir}/task_*.json"):
            with open(f) as fh:
                d = json.load(fh)
                if d.get("status") == "completed":
                    completed_ids.add(d["task_id"])

        answered_ids = completed_ids | {t["task_id"] for t in interrupted}
        unanswered = []
        for task in tasks:
            tid = task["task_id"]
            if tid in answered_ids:
                continue
            my_answer = arena.get_my_answer(tid)
            if my_answer and my_answer.get("answer"):
                answered_ids.add(tid)
                continue
            unanswered.append(task)
            if remaining and len(unanswered) >= remaining:
                break

        unanswered.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)
        for t in unanswered:
            work_items.append(("new", t))
    else:
        unanswered = []

    total = len(work_items)
    workers = concurrency or total
    print(f"Total: {len(tasks)}, Interrupted: {len(interrupted)}, To process: {total}, Concurrency: {workers}")

    if not work_items:
        print("Nothing to process.")
        return

    results = []

    def process_one(mode: str, task: dict) -> dict:
        worker = TaskWorker(config)
        if mode == "resume":
            return worker.resume_task(task["task_id"])
        return worker.process_task(task["task_id"])

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one, mode, t): (mode, t) for mode, t in work_items}
        with tqdm(total=total, desc="Tasks", unit="task") as pbar:
            for future in as_completed(futures):
                mode, task = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"task_id": task["task_id"], "status": "failed", "error": str(e)}
                results.append(result)
                score = result.get("score", "N/A")
                tag = "[R]" if mode == "resume" else ""
                pbar.set_postfix_str(f"{tag}{task['title'][:28]} → score={score}")
                pbar.update(1)

    # 全部完成后统一做 refine + strategy learning
    _do_batch_refine(config, results, " (final)")
    _do_strategy_learning(config, results)

    # 记录分解详情
    _save_decomposition_report(config, results)

    print(f"\n{'='*60}")
    print(f"Processed {len(results)} tasks")
    succeeded = sum(1 for r in results if r["status"] == "completed")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed: {len(results) - succeeded}")
    total_score = sum(r.get("score", 0) or 0 for r in results)
    print(f"  Total score: {total_score}")


def _save_decomposition_report(config: ConfigLoader, results: list[dict]):
    """保存每个任务的分解详情报告"""
    log_dir = config.settings.get("logging", {}).get("log_dir", "logs")
    report_path = Path(log_dir) / "decomposition_report.json"

    report = []
    for r in results:
        if r.get("status") != "completed":
            continue
        plan_result = r.get("plan_result", {})
        sub_tasks = plan_result.get("sub_tasks", [])
        entry = {
            "task_id": r["task_id"],
            "title": r.get("title", ""),
            "category": r.get("category", ""),
            "score": r.get("score"),
            "decomposition_mode": r.get("decomposition_mode", ""),
            "sub_tasks": [
                {
                    "id": t["id"],
                    "question": t["question"][:80],
                    "expert": t["expert"],
                    "depends_on": t.get("depends_on", []),
                }
                for t in sub_tasks
            ],
            "dag_graph": _describe_dag_short(sub_tasks),
            "final_synthesis": plan_result.get("final_synthesis"),
        }
        report.append(entry)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Report] Decomposition report saved: {report_path} ({len(report)} tasks)")


def _describe_dag_short(sub_tasks: list[dict]) -> str:
    """简短描述 DAG 结构"""
    if not sub_tasks:
        return "empty"
    parts = []
    for t in sub_tasks:
        deps = t.get("depends_on", [])
        expert_tag = t["expert"][:3]
        if deps:
            parts.append(f"{','.join(deps)}->{t['id']}({expert_tag})")
        else:
            parts.append(f"{t['id']}({expert_tag})")
    return " ; ".join(parts)


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
    parser.add_argument("--test", type=int, nargs="?", const=2, help="测试模式：只处理N个任务（默认2）")
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
    elif args.test is not None:
        cmd_process_all(config, concurrency=args.test, limit=args.test)
    elif args.all:
        cmd_process_all(config)
    elif args.list:
        cmd_list_tasks(config)
    elif args.status:
        cmd_status(config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
