#!/usr/bin/env python3
"""
Cowork 工作流：Plan路由 → 专家分析 → NPC求助 → 整合作答

单任务处理流程：
1. Plan Agent 路由到专家领域
2. Expert 分析问题，判断是否需要事实性依据
3. 如需要：发 bounty 悬赏帖向 NPC 求助 → 等待回答 → 选取一个回答
4. Expert 整合 NPC 回答 + 原始问题，生成最终答案
5. 提交答案

用法：
  python -m cowork.pipeline --task <task_id>       处理单个任务
  python -m cowork.pipeline --all                  处理所有未答题目
  python -m cowork.pipeline --local                从本地 tasks_local/ 加载题目（离线作答）
  python -m cowork.pipeline --local --submit       离线作答后提交
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx
from openai import OpenAI

# ============================================================================
# 配置
# ============================================================================

LLM_BASE_URL = "http://10.246.241.177:8000/v1"
LLM_API_KEY = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImhvbG9zLWtleSIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJob2xvcyIsImF1ZCI6ImxsbS1nYXRld2F5IiwiZXhwIjoxNzc5MjkyODAwLCJpYXQiOjE3NzkxMTcyOTksImp0aSI6ImM3YWQ3MzUyLWIwMWMtNDc5OC1iZTg1LTY4ZGE1NmVkYzM0NiIsInNjb3BlIjoibGxtX2dhdGV3YXkiLCJhZ2VudCI6eyJhZ2VudF9pZCI6ImIwNTVkZDkwLWM2YWEtNGNiZS04NTlmLTVhZTlhYzAxOTA4NSJ9LCJvd25lciI6eyJvd25lcl91c2VyX2lkIjoxNTJ9LCJhcmVuYSI6eyJjb21wZXRpdGlvbl9pZCI6IjY3ZDljN2QxLTkyMzItNDFiMy04N2IwLWVlNmVlMjBiZDJhMyIsInBhcnRpY2lwYW50X2lkIjoiY2IwYjJlODUtNWU2Ni00YmM2LWE0ZjktZTMxMGQzOTE4NTg5Iiwic3RhZ2VfaWQiOm51bGwsInJlbGF0ZWRfdHlwZSI6Im1hbnVhbF90ZXN0IiwicmVsYXRlZF9pZCI6Im1hbnVhbCJ9fQ.l7KJ59YYb-VfftaXHkLf2VnQuDjYRPhVupRNTXXnJ-Vr9_SWByoLCbIODgIbYGdXZ5MQupVFO9J-clQy8FhMWFV4hNv7my2haAeqe1Dsi361qefzm-2MIJacK-KXfOjbEFaE_sXxZNyVjB1iarwzmdD1I9CCE14Gc-pYHt_AEo4CL6ZSFE00mI3tlXMOPMJCuguqaslY6HbUMxGHMm6lQd3wWSlHA4sGO3dqHTeoD9yeZUzpS8w36qRzWZXn56xPIuINhospI2Ox5vL_vLg9uvmxKhdDgHsqEYF1jeoYWkqStDXM1BW_B6pxmnl-8_5bBJDAYhyn2ofiPNHqwu_g7Q"
LLM_MODEL = "nex/nex-n1.1"

ARENA_BASE_URL = "https://api.holosai.io"
AGORA_BASE_URL = "https://agora.holosai.io"
COMPETITION_ID = "67d9c7d1-9232-41b3-87b0-ee6ee20bd2a3"
AGENT_SECRET = "smWpqk15Nc4KHGm6mBg6SM0oCWqi2U0v3RlPuM-nxndMgfskriTT_h1TP6NWZUF-"
AGENT_PREFIX = f"/api/v1/holos/arena/agent/competitions/{COMPETITION_ID}"

BOUNTY_POLL_INTERVAL = 15  # 秒
BOUNTY_POLL_MAX = 8  # 最多等 2 分钟

LOG_DIR = Path("cowork/logs")
REFINED_DIR = Path("logv2/refined")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================================
# Refined Prompts
# ============================================================================

REFINED_PROMPTS = {}
for f in REFINED_DIR.glob("*.txt"):
    REFINED_PROMPTS[f.stem] = f.read_text(encoding="utf-8").strip()

# ============================================================================
# Plan Agent Prompt
# ============================================================================

PLAN_SYSTEM_PROMPT = """你是一个任务分类专家。你的唯一职责是判断题目属于哪个领域。

必须从以下五个领域中选一个：
- natural_science（自然科学：化学、生物、物理、数学、通信）
- law（法律事务：刑事、民事、商事、知识产权、国际法）
- finance（金融分析：股票、VC/PE、基金、消费金融、并购）
- industrial_engineering（工业工程：建筑、土木、软件开发、嵌入式、3D）
- medical_health（医疗健康：外科、内科、妇产科、病理）

你必须以 JSON 格式输出：
{
  "category": "五大领域之一的英文标识",
  "reasoning": "简短的分类理由"
}
"""

# ============================================================================
# Expert 分析 Prompt（第一步：判断是否需要外部事实依据）
# ============================================================================

ANALYSIS_SYSTEM_PROMPT = """你是一位资深专家。现在需要你分析以下问题，判断回答此问题是否**必须**参考外部事实性依据。

事实性依据包括：
- 具体的法律条文、司法解释、判例
- 具体的财务数据、财报数字、市场数据
- 具体的临床指南、药物剂量、诊断标准
- 具体的学术论文结论、实验数据、公式常数
- 具体的工程规范、标准编号、技术参数

**不需要**外部依据的情况：
- 纯逻辑推理/分析框架题
- 基于题目已给信息即可完整回答的
- 通用知识即可覆盖的

请以 JSON 格式输出：
{
  "needs_reference": true/false,
  "reason": "简要说明为什么需要/不需要外部依据",
  "query": "如果 needs_reference=true，这里写你想向外部求助的具体问题（精准、可直接作为帖子内容发出）。如果不需要，留空字符串。"
}
"""

# ============================================================================
# LLM Client
# ============================================================================

def llm_chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 20000,
             response_format: dict = None) -> dict:
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=180)
    params = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        params["response_format"] = response_format
    resp = client.chat.completions.create(**params)
    choice = resp.choices[0]
    content = choice.message.content or ""
    usage = resp.usage
    return {
        "content": content,
        "input_tokens": usage.prompt_tokens if usage else 0,
        "output_tokens": usage.completion_tokens if usage else 0,
    }


def llm_chat_json(messages: list[dict], **kwargs) -> dict:
    result = llm_chat(messages, response_format={"type": "json_object"}, **kwargs)
    try:
        parsed = json.loads(result["content"])
    except json.JSONDecodeError:
        parsed = {}
    result["parsed"] = parsed
    return result

# ============================================================================
# Arena / Agora Client
# ============================================================================

class ArenaClient:
    def __init__(self):
        self._agora_token = None

    def _agent_headers(self):
        return {"Authorization": f"Bearer {AGENT_SECRET}", "Content-Type": "application/json"}

    def _get_agora_token_sync(self) -> str:
        if self._agora_token:
            return self._agora_token
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{ARENA_BASE_URL}{AGENT_PREFIX}/agora/token",
                headers=self._agent_headers(), json={}
            )
            resp.raise_for_status()
            data = resp.json().get("data", resp.json())
            self._agora_token = data.get("token") or data.get("access_token", "")
        return self._agora_token

    def create_bounty(self, title: str, description: str, amount: int = 1) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{ARENA_BASE_URL}{AGENT_PREFIX}/bounty-tasks",
                headers=self._agent_headers(),
                json={"title": title, "description": description, "bounty_amount": amount}
            )
            resp.raise_for_status()
            return resp.json().get("data", resp.json())

    def poll_bounty_answers(self, post_id: str, max_attempts: int = BOUNTY_POLL_MAX) -> list:
        token = self._get_agora_token_sync()
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, max_attempts + 1):
            time.sleep(BOUNTY_POLL_INTERVAL)
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{AGORA_BASE_URL}/api/posts/{post_id}/answers",
                    headers=headers
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", resp.json())
                    items = data.get("items", [])
                    if items:
                        logger.info(f"  Got {len(items)} answers after {attempt * BOUNTY_POLL_INTERVAL}s")
                        # 获取第一个回答的完整内容
                        ans_id = items[0]["id"]
                        full_resp = client.get(
                            f"{AGORA_BASE_URL}/api/answers/{ans_id}",
                            headers=headers
                        )
                        if full_resp.status_code == 200:
                            full_data = full_resp.json().get("data", full_resp.json())
                            items[0]["text"] = full_data.get("text", items[0].get("text_preview", ""))
                        return items
            logger.debug(f"  Poll #{attempt}/{max_attempts}: no answers yet")
        return []

    def submit_answer(self, task_id: str, text: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{ARENA_BASE_URL}{AGENT_PREFIX}/tasks/{task_id}/answers",
                headers=self._agent_headers(),
                json={"text": text}
            )
            resp.raise_for_status()
            return resp.json().get("data", resp.json())

    def get_my_answer(self, task_id: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{ARENA_BASE_URL}{AGENT_PREFIX}/tasks/{task_id}/answers/me",
                headers=self._agent_headers()
            )
            if resp.status_code == 200:
                return resp.json().get("data", resp.json())
            return {}

# ============================================================================
# Pipeline
# ============================================================================

@dataclass
class TaskResult:
    task_id: str
    title: str = ""
    category: str = ""
    needs_reference: bool = False
    bounty_query: str = ""
    npc_answer: str = ""
    final_answer: str = ""
    submitted: bool = False
    tokens_used: int = 0
    error: str = ""


def process_task(task_id: str, title: str, content: str, arena: ArenaClient,
                 submit: bool = True) -> TaskResult:
    """处理单个任务的完整流程"""
    result = TaskResult(task_id=task_id, title=title)
    total_tokens = 0

    try:
        # === Step 1: Plan Agent 路由 ===
        logger.info(f"[{task_id[:8]}] Plan Agent...")
        plan_result = llm_chat_json([
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": f"## 题目标题\n{title}\n\n## 题目正文\n{content}"},
        ])
        total_tokens += plan_result["input_tokens"] + plan_result["output_tokens"]
        category = plan_result["parsed"].get("category", "natural_science")
        valid = ["natural_science", "law", "finance", "industrial_engineering", "medical_health"]
        if category not in valid:
            category = "natural_science"
        result.category = category
        logger.info(f"[{task_id[:8]}] Category: {category}")

        # === Step 2: Expert 分析，判断是否需要外部依据 ===
        logger.info(f"[{task_id[:8]}] Expert 分析问题...")
        analysis_result = llm_chat_json([
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": f"## 题目标题\n{title}\n\n## 题目正文\n{content}"},
        ])
        total_tokens += analysis_result["input_tokens"] + analysis_result["output_tokens"]
        needs_ref = analysis_result["parsed"].get("needs_reference", False)
        query = analysis_result["parsed"].get("query", "")
        result.needs_reference = needs_ref

        # === Step 3: 如需要，发 bounty 求助 NPC ===
        npc_context = ""
        if needs_ref and query:
            result.bounty_query = query
            logger.info(f"[{task_id[:8]}] 发 bounty 求助: {query[:60]}...")
            bounty_title = f"{title} | {query}"[:200]
            bounty = arena.create_bounty(
                title=bounty_title,
                description=f"{content}\n\n---\n需要的信息：{query}",
                amount=1
            )
            post_id = bounty.get("agora_post_id", "")
            if post_id:
                answers = arena.poll_bounty_answers(post_id)
                if answers:
                    # 选第一个有内容的回答（已获取完整text）
                    chosen = answers[0]
                    npc_answer = chosen.get("text", "") or chosen.get("text_preview", "")
                    result.npc_answer = npc_answer
                    npc_context = f"\n\n## 参考信息（来自外部专家）\n{npc_answer}"
                    logger.info(f"[{task_id[:8]}] NPC 回答({len(npc_answer)}字): {npc_answer[:80]}...")
                else:
                    logger.warning(f"[{task_id[:8]}] NPC 未回答，继续独立作答")
        else:
            logger.info(f"[{task_id[:8]}] 无需外部依据，直接作答")

        # === Step 4: Expert 最终作答 ===
        logger.info(f"[{task_id[:8]}] Expert 最终作答...")
        expert_prompt = REFINED_PROMPTS.get(category, "你是一位资深专家，请严谨专业地回答问题。")
        answer_result = llm_chat([
            {"role": "system", "content": expert_prompt},
            {"role": "user", "content": f"## 题目标题\n{title}\n\n## 题目正文\n{content}{npc_context}\n\n请根据以上信息，给出完整、专业、严谨的回答。"},
        ], temperature=0.1)
        total_tokens += answer_result["input_tokens"] + answer_result["output_tokens"]
        result.final_answer = answer_result["content"]

        # === Step 5: 提交 ===
        if submit:
            logger.info(f"[{task_id[:8]}] 提交答案...")
            arena.submit_answer(task_id, result.final_answer)
            result.submitted = True
            logger.info(f"[{task_id[:8]}] ✓ 提交成功")

        result.tokens_used = total_tokens

    except Exception as e:
        result.error = str(e)
        logger.error(f"[{task_id[:8]}] Error: {e}")

    # 保存结果
    _save_result(result)
    return result


def _save_result(result: TaskResult):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{result.task_id[:8]}_{result.category}.json"
    data = {
        "task_id": result.task_id,
        "title": result.title,
        "category": result.category,
        "needs_reference": result.needs_reference,
        "bounty_query": result.bounty_query,
        "npc_answer": result.npc_answer[:500],
        "final_answer": result.final_answer[:500],
        "submitted": result.submitted,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
    with open(LOG_DIR / fname, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================

def load_local_tasks() -> list[dict]:
    path = Path("tasks_local/all_tasks.json")
    if not path.exists():
        logger.error("tasks_local/all_tasks.json not found. Run fetch_tasks.py first.")
        return []
    with open(path) as f:
        tasks = json.load(f)
    # 只处理 published 状态的新题
    tasks = [t for t in tasks if t.get("status") == "published"]
    logger.info(f"Filtered to {len(tasks)} published tasks")
    return tasks


def run_all(tasks: list[dict], submit: bool = True, concurrency: int = 5):
    arena = ArenaClient()

    # 过滤已完成的
    done_ids = set()
    for f in LOG_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                d = json.load(fh)
                if d.get("submitted") or d.get("final_answer"):
                    done_ids.add(d["task_id"])
        except:
            pass

    pending = [t for t in tasks if t["task_id"] not in done_ids]
    pending.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)
    logger.info(f"Total: {len(tasks)}, Done: {len(done_ids)}, Pending: {len(pending)}")

    results = []

    def _process(task):
        tid = task["task_id"]
        title = task.get("title", "")
        post = task.get("agora_post", {})
        content = post.get("description", "") or post.get("content", "") if post else ""
        return process_task(tid, title, content, arena, submit=submit)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_process, t): t for t in pending}
        for future in as_completed(futures):
            task = futures[future]
            try:
                r = future.result()
                results.append(r)
                status = "✓" if not r.error else f"✗ {r.error[:30]}"
                logger.info(f"[{r.task_id[:8]}] {status} | cat={r.category} | ref={r.needs_reference} | tokens={r.tokens_used}")
            except Exception as e:
                logger.error(f"[{task['task_id'][:8]}] Unhandled: {e}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Processed: {len(results)}")
    print(f"Submitted: {sum(1 for r in results if r.submitted)}")
    print(f"With NPC:  {sum(1 for r in results if r.npc_answer)}")
    print(f"Errors:    {sum(1 for r in results if r.error)}")
    print(f"Tokens:    {sum(r.tokens_used for r in results)}")


def main():
    parser = argparse.ArgumentParser(description="Cowork Pipeline: Plan→Expert→NPC→Answer")
    parser.add_argument("--task", type=str, help="处理单个 task_id")
    parser.add_argument("--all", action="store_true", help="处理所有题目")
    parser.add_argument("--local", action="store_true", help="从本地 tasks_local/ 加载")
    parser.add_argument("--submit", action="store_true", help="提交答案（默认不提交）")
    parser.add_argument("--concurrency", type=int, default=5, help="并发数")
    args = parser.parse_args()

    if args.local or args.all:
        tasks = load_local_tasks()
        if not tasks:
            return
        run_all(tasks, submit=args.submit, concurrency=args.concurrency)
    elif args.task:
        tasks = load_local_tasks()
        task = next((t for t in tasks if t["task_id"] == args.task), None)
        if not task:
            logger.error(f"Task {args.task} not found")
            return
        arena = ArenaClient()
        post = task.get("agora_post", {})
        content = post.get("description", "") or post.get("content", "") if post else ""
        process_task(args.task, task.get("title", ""), content, arena, submit=args.submit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
