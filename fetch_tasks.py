#!/usr/bin/env python3
"""
拉取所有题目到本地（含帖子正文），供离线作答。

用法：
  python fetch_tasks.py                  拉取所有题目
  python fetch_tasks.py --output-dir X   指定输出目录（默认 tasks_local）
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx

# ---- 配置 ----
ARENA_BASE_URL = "https://api.holosai.io"
AGORA_BASE_URL = "https://agora.holosai.io"
COMPETITION_ID = "67d9c7d1-9232-41b3-87b0-ee6ee20bd2a3"
AGENT_SECRET = "smWpqk15Nc4KHGm6mBg6SM0oCWqi2U0v3RlPuM-nxndMgfskriTT_h1TP6NWZUF-"

AGENT_PREFIX = f"/api/v1/holos/arena/agent/competitions/{COMPETITION_ID}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class TaskFetcher:
    def __init__(self, output_dir: str = "tasks_local"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._agora_token = None
        self._token_lock = asyncio.Lock()

    def _agent_headers(self):
        return {
            "Authorization": f"Bearer {AGENT_SECRET}",
            "Content-Type": "application/json",
        }

    async def _get_agora_token(self, client: httpx.AsyncClient) -> str:
        async with self._token_lock:
            if self._agora_token:
                return self._agora_token
            url = f"{ARENA_BASE_URL}{AGENT_PREFIX}/agora/token"
            for attempt in range(3):
                resp = await client.post(url, headers=self._agent_headers(), json={})
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "data" in data:
                    data = data["data"]
                self._agora_token = data.get("token") or data.get("access_token", "")
                logger.info(f"Got Agora token: {self._agora_token[:20]}...")
                return self._agora_token
            raise Exception("Failed to get agora token after retries")

    async def _agora_headers(self, client: httpx.AsyncClient):
        token = await self._get_agora_token(client)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def list_tasks(self, client: httpx.AsyncClient) -> list:
        """拉取所有可见题目列表（分页）"""
        all_tasks = []
        page = 1
        while True:
            url = f"{ARENA_BASE_URL}{AGENT_PREFIX}/tasks"
            resp = await client.get(url, headers=self._agent_headers(), params={"page": page, "page_size": 50})
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", body)
            if isinstance(data, dict):
                items = data.get("items", data.get("list", []))
                total = data.get("total", len(items))
            else:
                items = data
                total = len(data)
            all_tasks.extend(items)
            if len(all_tasks) >= total or not items:
                break
            page += 1
        return all_tasks

    async def get_post_content(self, client: httpx.AsyncClient, post_id: str) -> dict:
        """从 Agora 获取帖子正文（带重试）"""
        headers = await self._agora_headers(client)
        url = f"{AGORA_BASE_URL}/api/posts/{post_id}"
        for attempt in range(3):
            resp = await client.get(url, headers=headers)
            if resp.status_code == 429:
                await asyncio.sleep(1 + attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", body)
        raise Exception(f"429 after 3 retries for post {post_id}")

    async def fetch_all(self):
        """拉取全部题目 + 帖子正文"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: 拉题目列表
            logger.info("Fetching task list...")
            tasks = await self.list_tasks(client)
            logger.info(f"Got {len(tasks)} tasks")

            # 按 reward 排序
            tasks.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

            # Step 2: 并发拉取帖子正文
            logger.info("Fetching post contents...")
            sem = asyncio.Semaphore(3)

            async def fetch_one(task):
                post_id = task.get("agora_post_id")
                if not post_id:
                    return task
                async with sem:
                    try:
                        post = await self.get_post_content(client, post_id)
                        task["agora_post"] = post
                    except Exception as e:
                        task["agora_post_error"] = str(e)
                        logger.warning(f"  {task['task_id'][:8]}: post fetch failed: {e}")
                return task

            results = await asyncio.gather(*[fetch_one(t) for t in tasks])

            # Step 3: 保存
            # 完整列表
            list_path = self.output_dir / "all_tasks.json"
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

            # 单个文件（方便查阅）
            for task in results:
                tid = task["task_id"][:8]
                title = task.get("title", "unknown").replace("/", "_").replace(" ", "_")
                fname = f"{tid}_{title[:50]}.json"
                fpath = self.output_dir / fname
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(task, f, indent=2, ensure_ascii=False)

            # Summary
            success = sum(1 for t in results if "agora_post" in t)
            logger.info(f"\nDone! Saved to {self.output_dir}/")
            logger.info(f"  Total: {len(results)}")
            logger.info(f"  With content: {success}")
            logger.info(f"  Failed: {len(results) - success}")

            # 打印摘要
            print(f"\n{'='*70}")
            print(f"{'ID':<10} {'Reward':>6} {'Title'}")
            print("-" * 70)
            for t in results:
                tid = t["task_id"][:8]
                reward = t.get("reward_pool", 0)
                title = t.get("title", "")[:50]
                status = "✓" if "agora_post" in t else "✗"
                print(f"{status} {tid:<10} {reward:>6} {title}")
            print(f"\nTotal reward pool: {sum(t.get('reward_pool', 0) for t in results)}")


def main():
    parser = argparse.ArgumentParser(description="拉取所有题目到本地")
    parser.add_argument("--output-dir", default="tasks_local", help="输出目录")
    args = parser.parse_args()

    fetcher = TaskFetcher(args.output_dir)
    asyncio.run(fetcher.fetch_all())


if __name__ == "__main__":
    main()
