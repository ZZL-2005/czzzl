#!/usr/bin/env python3
"""
V4 逐题处理脚本
逐条读取当前榜单题目，执行：检索→缓存查询→精读→构建上下文→生成答案→提交

详细记录：
- 每题的 token 消耗明细
- 缓存命中情况（命中了哪些、是否真的有帮助）
- LLM 判定命中有效性
- 最终答案效果

用法:
  python run_v4.py --task <task_id>     处理单个任务
  python run_v4.py --all                处理所有未答题目
  python run_v4.py --list               列出可处理题目
  python run_v4.py --limit 5            只处理前5个
  python run_v4.py --retrieval-only     只做检索不提交答案
"""

import argparse
import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict

from src.config_loader import ConfigLoader
from src.arena_client import ArenaClient
from src.plan_agent import PlanAgent
from src.expert_agent import ExpertAgent
from src.retriever import AcademicRetriever, Paper
from src.paper_reader import PaperReader
from src.paper_cache import PaperCache
from src.llm_client import LLMClient
from src.feedback_loop import FeedbackLoop
from src.logger import TaskLogger, SummaryLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logv4/runtime.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 缓存命中有效性判断 prompt
CACHE_HIT_VALIDATION_PROMPT = """判断以下缓存命中的论文信息是否对回答研究问题真正有帮助。

## 研究问题
{question}

## 缓存命中的论文
{cache_hit_info}

## 要求
以 JSON 格式回答：
{{
  "helpful": true/false,
  "reason": "为什么有帮助/没帮助（一句话）",
  "usefulness_score": 1-5
}}
评分标准：5=直接回答问题核心，4=提供重要背景，3=部分相关，2=边缘相关，1=不相关
"""


@dataclass
class TaskResult:
    """单题处理结果"""
    task_id: str
    title: str
    category: str
    reward_pool: int = 0

    # 检索统计
    keywords_extracted: list = field(default_factory=list)
    papers_searched: int = 0
    papers_relevant: int = 0
    papers_with_full_text: int = 0

    # 缓存命中
    cache_hits: int = 0
    cache_hits_validated: int = 0
    cache_hit_details: list = field(default_factory=list)

    # 精读
    papers_read: int = 0
    reading_details: list = field(default_factory=list)

    # Token 消耗
    tokens_keyword: int = 0
    tokens_cache_validation: int = 0
    tokens_relevance: int = 0
    tokens_reading: int = 0
    tokens_expert: int = 0
    tokens_total: int = 0

    # 最终结果
    context_injected: str = ""
    context_length: int = 0
    answer_submitted: bool = False
    score: float = None
    status: str = "pending"


def process_single_task(
    task_id: str,
    config: ConfigLoader,
    cache: PaperCache,
    retriever: AcademicRetriever,
    reader: PaperReader,
    llm: LLMClient,
    arena: ArenaClient,
    retrieval_only: bool = False,
) -> TaskResult:
    """处理单个任务的完整 v4 流程"""

    # 获取题目
    task_detail = arena.get_task_detail(task_id)
    title = task_detail.get("title", "")
    agora_post = task_detail.get("agora_post") or {}
    content = agora_post.get("description", "") or agora_post.get("content", "")
    reward_pool = task_detail.get("reward_pool", 0)
    post_id = task_detail.get("agora_post_id", "")

    # Plan Agent 分类
    plan_agent = PlanAgent(config)
    plan_result, plan_response = plan_agent.analyze(title, content)
    category = plan_result["category"]

    result = TaskResult(
        task_id=task_id, title=title, category=category, reward_pool=reward_pool,
    )

    question = f"{title}\n{content[:600]}"
    logger.info(f"[{task_id[:8]}] {title} (category={category}, reward={reward_pool})")

    # ═══════════════════════════════════════════
    # Phase 1: 关键词提取
    # ═══════════════════════════════════════════
    kw_prompt = (
        f"分析以下学术题目，提取用于论文检索的英文关键词。\n\n"
        f"## 题目\n{title}\n\n## 内容\n{content[:800]}\n\n"
        f"## 要求\n输出 JSON：{{\"keywords\": [[\"kw1\", \"kw2\"], [\"kw3\", \"kw4\"]], "
        f"\"search_focus\": \"一句话说明需要什么类型的论文支撑\"}}"
    )
    try:
        kw_result, kw_resp = llm.chat_json([{"role": "user", "content": kw_prompt}])
        keywords_groups = kw_result.get("keywords", [])
        result.keywords_extracted = keywords_groups
        result.tokens_keyword = kw_resp.total_tokens
        logger.info(f"  Keywords: {keywords_groups}")
    except Exception as e:
        logger.error(f"  Keyword extraction failed: {e}")
        result.status = "failed_keywords"
        return result

    all_keywords = [kw for group in keywords_groups for kw in group]

    # ═══════════════════════════════════════════
    # Phase 2: 缓存查询 + 有效性验证
    # ═══════════════════════════════════════════
    cached_results = cache.search_by_keywords(all_keywords, max_results=10)
    result.cache_hits = len(cached_results)

    validated_cache_content = []
    if cached_results:
        logger.info(f"  Cache hits: {len(cached_results)}")
        for cached_paper in cached_results[:5]:
            pid = cached_paper.get("paper_id", "")
            cached_title = cached_paper.get("title", "")
            extracted = cached_paper.get("full_text_extracted", "")
            abstract = cached_paper.get("abstract", "")

            # 用 LLM 验证缓存命中是否真的有帮助
            cache_info = f"标题: {cached_title}\n"
            if extracted:
                cache_info += f"已提取信息: {extracted[:300]}"
            else:
                cache_info += f"摘要: {abstract[:300]}"

            validation_prompt = CACHE_HIT_VALIDATION_PROMPT.format(
                question=question,
                cache_hit_info=cache_info,
            )
            try:
                val_result, val_resp = llm.chat_json([{"role": "user", "content": validation_prompt}])
                result.tokens_cache_validation += val_resp.total_tokens
                helpful = val_result.get("helpful", False)
                score = val_result.get("usefulness_score", 1)

                hit_detail = {
                    "paper_id": pid,
                    "title": cached_title,
                    "helpful": helpful,
                    "usefulness_score": score,
                    "reason": val_result.get("reason", ""),
                }
                result.cache_hit_details.append(hit_detail)

                if helpful and score >= 3:
                    result.cache_hits_validated += 1
                    content_to_use = extracted if extracted else abstract[:400]
                    validated_cache_content.append(f"[缓存] {cached_title}\n{content_to_use}")
                    cache.mark_used_by_task(pid, task_id)
                    logger.info(f"    ✅ {cached_title[:40]} (score={score})")
                else:
                    logger.info(f"    ❌ {cached_title[:40]} (score={score}, {val_result.get('reason', '')})")
            except Exception as e:
                logger.warning(f"    Cache validation failed: {e}")

    # ═══════════════════════════════════════════
    # Phase 3: API 检索新论文
    # ═══════════════════════════════════════════
    all_papers = []
    for group in keywords_groups[:3]:
        papers = retriever.search(group, category, max_total=6)
        all_papers.extend(papers)
        time.sleep(0.5)

    # 去重
    seen = set()
    unique_papers = []
    for p in all_papers:
        k = p.title.lower().strip()
        if k not in seen:
            seen.add(k)
            unique_papers.append(p)

    result.papers_searched = len(unique_papers)
    logger.info(f"  API search: {len(unique_papers)} papers")

    # ═══════════════════════════════════════════
    # Phase 4: 相关性判断 + 精读
    # ═══════════════════════════════════════════
    relevant_papers = []
    for p in unique_papers[:10]:
        rel, expected, tokens = reader.check_relevance(question, p.title, p.abstract)
        result.tokens_relevance += tokens
        if rel:
            relevant_papers.append((p, expected))
            cache.add_paper(p, all_keywords)
        time.sleep(0.3)

    result.papers_relevant = len(relevant_papers)
    logger.info(f"  Relevant: {len(relevant_papers)}/{min(len(unique_papers), 10)}")

    # 精读有全文的论文
    fresh_extracted = []
    for p, expected in relevant_papers[:3]:
        full_text = None
        if p.pmcid:
            full_text = retriever.fetch_full_text_pmc(p.pmcid)
        elif p.arxiv_id:
            full_text = retriever.fetch_full_text_arxiv(p.arxiv_id)

        paper_id = cache._paper_id(p)

        if full_text:
            result.papers_with_full_text += 1
            reading = reader.read_paper(question, p.title, full_text, expected)
            result.tokens_reading += reading.tokens_used
            result.papers_read += 1
            result.reading_details.append({
                "title": p.title[:60],
                "chunks_read": reading.chunks_read,
                "total_chunks": reading.total_chunks,
                "early_exit": reading.early_exit,
                "exit_reason": reading.exit_reason[:60],
                "tokens": reading.tokens_used,
                "extracted_length": len(reading.extracted_info),
            })
            if reading.extracted_info:
                cache.set_extracted_content(paper_id, reading.extracted_info)
                fresh_extracted.append(f"[{p.title[:40]}]\n{reading.extracted_info}")
                logger.info(f"    ✓ Read {p.title[:40]} ({reading.chunks_read}/{reading.total_chunks} chunks)")
        else:
            # 仅摘要
            if p.abstract and len(p.abstract) > 100:
                fresh_extracted.append(f"[{p.title[:40]}]\n摘要: {p.abstract[:400]}")
                cache.set_extracted_content(paper_id, f"[摘要] {p.abstract[:400]}")

        cache.mark_used_by_task(paper_id, task_id)
        time.sleep(0.5)

    # ═══════════════════════════════════════════
    # Phase 5: 组装参考文献上下文
    # ═══════════════════════════════════════════
    all_context_parts = validated_cache_content + fresh_extracted
    reference_context = "\n\n".join(all_context_parts)

    # 限制长度
    max_chars = config.settings.get("retrieval", {}).get("max_context_chars", 4000)
    if len(reference_context) > max_chars:
        reference_context = reference_context[:max_chars] + "\n...[truncated]"

    result.context_injected = reference_context
    result.context_length = len(reference_context)
    logger.info(f"  Context: {len(reference_context)} chars ({len(all_context_parts)} sources)")

    if retrieval_only:
        result.tokens_total = (
            result.tokens_keyword + result.tokens_cache_validation
            + result.tokens_relevance + result.tokens_reading
        )
        result.status = "retrieval_done"
        return result

    # ═══════════════════════════════════════════
    # Phase 6: Expert Agent 生成答案 + 提交
    # ═══════════════════════════════════════════
    expert = ExpertAgent(category, config)
    answer, expert_resp = expert.generate_answer(title, content, reference_context)
    result.tokens_expert = expert_resp.total_tokens
    logger.info(f"  Expert answer: {len(answer)} chars, tokens={expert_resp.total_tokens}")

    # 提交
    try:
        arena.submit_answer(task_id, answer)
        result.answer_submitted = True
        logger.info(f"  ✓ Answer submitted")
    except Exception as e:
        logger.error(f"  Submit failed: {e}")
        result.status = "failed_submit"

    result.tokens_total = (
        result.tokens_keyword + result.tokens_cache_validation
        + result.tokens_relevance + result.tokens_reading + result.tokens_expert
    )
    result.status = "submitted"
    return result


def save_result(result: TaskResult, output_dir: str = "logv4"):
    """保存单题结果"""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    safe_title = result.title.replace("/", "_").replace(" ", "_")[:50]
    filename = f"task_{result.task_id[:8]}_{safe_title}.json"
    filepath = path / filename

    data = asdict(result)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"  Result saved: {filepath}")


def print_summary(results: list[TaskResult]):
    """打印汇总统计"""
    print(f"\n{'='*60}")
    print(f"V4 Processing Summary")
    print(f"{'='*60}")
    print(f"Tasks processed: {len(results)}")
    print()

    # Token 统计
    total_tokens = sum(r.tokens_total for r in results)
    print(f"Token Consumption:")
    print(f"  Keyword extraction: {sum(r.tokens_keyword for r in results):,}")
    print(f"  Cache validation:   {sum(r.tokens_cache_validation for r in results):,}")
    print(f"  Relevance check:    {sum(r.tokens_relevance for r in results):,}")
    print(f"  Paper reading:      {sum(r.tokens_reading for r in results):,}")
    print(f"  Expert answer:      {sum(r.tokens_expert for r in results):,}")
    print(f"  ──────────────────")
    print(f"  Total:              {total_tokens:,}")
    if results:
        print(f"  Avg per task:       {total_tokens // len(results):,}")
    print()

    # 缓存命中统计
    total_hits = sum(r.cache_hits for r in results)
    total_validated = sum(r.cache_hits_validated for r in results)
    print(f"Cache Hit Analysis:")
    print(f"  Total hits:       {total_hits}")
    print(f"  Validated useful: {total_validated}")
    print(f"  Hit precision:    {total_validated/total_hits*100:.1f}%" if total_hits > 0 else "  Hit precision:    N/A")
    print()

    # 检索统计
    print(f"Retrieval Stats:")
    print(f"  Papers searched:     {sum(r.papers_searched for r in results)}")
    print(f"  Papers relevant:     {sum(r.papers_relevant for r in results)}")
    print(f"  Papers with fulltext:{sum(r.papers_with_full_text for r in results)}")
    print(f"  Papers deep-read:    {sum(r.papers_read for r in results)}")
    print()

    # 逐题明细
    print(f"Per-Task Details:")
    print(f"{'ID':<10} {'Category':<20} {'Tokens':>7} {'Cache':>5} {'Valid':>5} {'Read':>4} {'Ctx':>5}")
    print("-" * 70)
    for r in results:
        print(
            f"{r.task_id[:8]:<10} {r.category:<20} {r.tokens_total:>7,} "
            f"{r.cache_hits:>5} {r.cache_hits_validated:>5} {r.papers_read:>4} {r.context_length:>5}"
        )


def main():
    parser = argparse.ArgumentParser(description="V4 逐题处理")
    parser.add_argument("--task", type=str, help="处理单个 task_id")
    parser.add_argument("--all", action="store_true", help="处理所有未答题目")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 个")
    parser.add_argument("--list", action="store_true", help="列出可处理题目")
    parser.add_argument("--retrieval-only", action="store_true", help="只做检索不提交答案")
    parser.add_argument("--output-dir", type=str, default="logv4", help="输出目录")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    config = ConfigLoader("config")
    arena = ArenaClient()
    cache = PaperCache(f"{args.output_dir}/paper_cache")
    retriever = AcademicRetriever(config.settings.get("retrieval", {}))
    reader = PaperReader(config)
    llm = LLMClient(config.get_plan_agent_config())

    if args.list:
        tasks = arena.get_tasks()
        tasks.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)
        print(f"{'ID':<38} {'Reward':>6} {'Ans':>3} {'Title'}")
        print("-" * 90)
        for t in tasks:
            print(f"{t['task_id']:<38} {t['reward_pool']:>6} {t['answer_count']:>3} {t['title'][:40]}")
        return

    if args.task:
        # 单题处理
        result = process_single_task(
            args.task, config, cache, retriever, reader, llm, arena,
            retrieval_only=args.retrieval_only,
        )
        save_result(result, args.output_dir)
        print_summary([result])
        return

    if args.all:
        # 处理所有未答题目
        tasks = arena.get_tasks()
        tasks.sort(key=lambda t: t.get("reward_pool", 0), reverse=True)

        # 过滤已答的
        to_process = []
        for t in tasks:
            my_answer = arena.get_my_answer(t["task_id"])
            if not my_answer or not my_answer.get("answer", {}).get("score"):
                to_process.append(t)

        if args.limit:
            to_process = to_process[:args.limit]

        print(f"Tasks to process: {len(to_process)}")
        results = []

        for i, task in enumerate(to_process):
            print(f"\n[{i+1}/{len(to_process)}] {task['title'][:50]} (reward={task['reward_pool']})")
            try:
                result = process_single_task(
                    task["task_id"], config, cache, retriever, reader, llm, arena,
                    retrieval_only=args.retrieval_only,
                )
                results.append(result)
                save_result(result, args.output_dir)
            except Exception as e:
                logger.error(f"  Failed: {e}")
                results.append(TaskResult(
                    task_id=task["task_id"], title=task["title"],
                    category="unknown", status=f"error: {str(e)[:100]}",
                ))

            # 每 5 题打印进度
            if (i + 1) % 5 == 0:
                print(f"\n--- Progress: {i+1}/{len(to_process)}, Cache: {cache.stats()} ---")

        print_summary(results)

        # 保存汇总报告
        summary_path = Path(args.output_dir) / "run_summary.json"
        summary = {
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tasks_processed": len(results),
            "cache_stats": cache.stats(),
            "total_tokens": sum(r.tokens_total for r in results),
            "cache_hit_precision": (
                sum(r.cache_hits_validated for r in results)
                / max(sum(r.cache_hits for r in results), 1)
            ),
            "results": [asdict(r) for r in results],
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\nSummary saved: {summary_path}")


if __name__ == "__main__":
    main()
