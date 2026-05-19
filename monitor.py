#!/usr/bin/env python3
"""
排行榜和外包市场监控
用法：
  python monitor.py --leaderboard        查看排行榜
  python monitor.py --bounty             查看外包市场
  python monitor.py --snapshot           保存快照
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.arena_client import ArenaClient
from src.config_loader import ConfigLoader


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def show_leaderboard():
    arena = ArenaClient()
    lb = arena.get_leaderboard()

    print(f"\n{'Rank':>4} {'Name':<20} {'Score':>8} {'Overall':>8} {'Wallet':>8}")
    print("-" * 55)
    for p in lb:
        print(
            f"#{p['rank']:>3} {p['display_name']:<20} "
            f"{p['total_score']:>8.1f} {p['overall_score']:>8.1f} {p['wallet_balance']:>8}"
        )
    print(f"\nTotal participants: {len(lb)}")
    return lb


def show_bounty():
    arena = ArenaClient()
    bounties = arena.get_bounty_list()

    open_bounties = [b for b in bounties if b["status"] == "open"]
    print(f"\nOpen bounties: {len(open_bounties)} / {len(bounties)} total")
    print(f"\n{'ID':<10} {'Amount':>6} {'Answers':>7} {'Title'}")
    print("-" * 80)
    for b in open_bounties:
        short_id = b["bounty_task_id"][:8]
        title = b["title"][:50]
        print(f"{short_id:<10} {b['bounty_amount']:>6} {b['answer_count']:>7} {title}")

    return bounties


def save_snapshot(output_dir: str = "logs"):
    arena = ArenaClient()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    base = Path(output_dir)
    lb_dir = base / "leaderboard_snapshots"
    lb_dir.mkdir(parents=True, exist_ok=True)
    lb = arena.get_leaderboard()
    with open(lb_dir / f"{now}.json", "w", encoding="utf-8") as f:
        json.dump({"timestamp": now, "data": lb}, f, ensure_ascii=False, indent=2)

    bounty_dir = base / "bounty_market"
    bounty_dir.mkdir(parents=True, exist_ok=True)
    bounties = arena.get_bounty_list()
    with open(bounty_dir / f"{now}.json", "w", encoding="utf-8") as f:
        json.dump({"timestamp": now, "data": bounties}, f, ensure_ascii=False, indent=2)

    me = arena.get_me()
    print(f"Snapshot saved at {now}")
    print(f"  Leaderboard: {len(lb)} entries")
    print(f"  Bounties: {len(bounties)} entries")
    print(f"  My score: {me['total_score']}, wallet: {me['wallet_balance']}")


def main():
    parser = argparse.ArgumentParser(description="Arena 监控工具")
    parser.add_argument("--leaderboard", action="store_true", help="查看排行榜")
    parser.add_argument("--bounty", action="store_true", help="查看外包市场")
    parser.add_argument("--snapshot", action="store_true", help="保存快照")
    parser.add_argument("--config-dir", type=str, default="config", help="配置目录路径")
    parser.add_argument("--output-dir", type=str, default=None, help="统一输出目录")
    args = parser.parse_args()

    setup_logging()

    config = ConfigLoader(args.config_dir)
    output_dir = args.output_dir or config.settings.get("logging", {}).get("log_dir", "logs")

    if args.leaderboard:
        show_leaderboard()
    elif args.bounty:
        show_bounty()
    elif args.snapshot:
        save_snapshot(output_dir)
    else:
        show_leaderboard()
        print()
        show_bounty()


if __name__ == "__main__":
    main()
