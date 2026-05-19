#!/usr/bin/env python3
"""
缓存预建脚本 (v4)
读取历史任务 (logv1/logv2) 的题目，执行完整的检索+阅读流程，预建论文缓存池。

用法：
  python build_cache.py                     处理所有历史任务
  python build_cache.py --limit 5           只处理前 5 个
  python build_cache.py --category finance  只处理 finance 类
  python build_cache.py --dry-run           只提取关键词，不做实际检索
  python build_cache.py --stats             查看缓存统计
"""

import argparse
import glob
import json
import logging
import time
import sys
from pathlib import Path
from dataclasses import dataclass

from src.config_loader import ConfigLoader
from src.retriever import AcademicRetriever, Paper
from src.paper_cache import PaperCache
from src.paper_reader import PaperReader
from src.knowledge_retrieval import KnowledgeRetrieval
from src.llm_client import LLMClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/build_cache.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class TokenTracker:
    """Token 消耗追踪"""
    keyword_extraction: int = 0
    relevance_check: int = 0
    chunk_reading: int = 0
    summarization: int = 0
    total: int = 0

    def add(self, stage: str, tokens: int):
        if hasattr(self, stage):
            setattr(self, stage, getattr(self, stage) + tokens)
        self.total += tokens

    def report(self) -> str:
        return (
            f"Token consumption:\n"
            f"  Keyword extraction: {self.keyword_extraction:,}\n"
            f"  Relevance check:    {self.relevance_check:,}\n"
            f"  Chunk reading:      {self.chunk_reading:,}\n"
            f"  Summarization:      {self.summarization:,}\n"
            f"  ─────────────────────\n"
            f"  Total:              {self.total:,}"
        )


def load_historical_tasks(log_dirs: list[str] = None) -> list[dict]:
    """加载历史任务数据"""
    if log_dirs is None:
        log_dirs = ["logv1", "logv2", "logv3"]

    tasks = []
    for log_dir in log_dirs:
        for f in sorted(glob.glob(f"{log_dir}/task_*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
                task_detail = data.get("task_detail") or {}
                agora_post = task_detail.get("agora_post") or {}
                content = agora_post.get("description", "") or agora_post.get("content", "")

                if not content and not data.get("title"):
                    continue

                tasks.append({
                    "task_id": data.get("task_id", ""),
                    "title": data.get("title", ""),
                    "content": content,
                    "category": data.get("category", ""),
                    "score": (data.get("cost_summary") or {}).get("score_earned"),
                    "feedback": (data.get("scoring") or {}).get("review_text", ""),
                    "source_file": f,
                })
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load {f}: {e}")
    return tasks


def build_cache_for_task(
    task: dict,
    config: ConfigLoader,
    cache: PaperCache,
    retriever: AcademicRetriever,
    reader: PaperReader,
    llm: LLMClient,
    tracker: TokenTracker,
    dry_run: bool = False,
) -> dict:
    """
    为单个历史任务构建缓存

    流程：
    1. LLM 分析题目 → 提取搜索关键词
    2. 先查缓存是否已有匹配论文
    3. 根据领域搜索学术数据库
    4. LLM 判断摘要相关性
    5. 对相关论文执行分块精读
    6. 缓存提取的关键信息
    """
    title = task["title"]
    content = task["content"]
    category = task["category"]
    task_id = task["task_id"]
    question = f"{title}\n{content[:500]}"

    # Per-task token tracker
    task_tokens = TokenTracker()

    result = {
        "task_id": task_id,
        "title": title,
        "category": category,
        "papers_searched": 0,
        "papers_relevant": 0,
        "papers_read": 0,
        "papers_cached": 0,
        "cache_hits": 0,
        "cache_hits_with_extraction": 0,
        "tokens_used": 0,
        "token_breakdown": {},
        "reading_details": [],
    }

    # Step 1: 提取关键词
    logger.info(f"[{task_id[:8]}] Extracting keywords for: {title[:50]}")
    keywords_prompt = (
        f"分析以下学术题目，提取用于论文检索的英文关键词。\n\n"
        f"## 题目\n{title}\n\n## 内容\n{content[:800]}\n\n"
        f"## 要求\n输出 JSON：{{\"keywords\": [[\"kw1\", \"kw2\"], [\"kw3\", \"kw4\"]], "
        f"\"search_focus\": \"一句话说明需要什么类型的论文支撑\"}}"
    )
    try:
        kw_result, kw_response = llm.chat_json([{"role": "user", "content": keywords_prompt}])
        keywords_groups = kw_result.get("keywords", [])
        search_focus = kw_result.get("search_focus", "")
        task_tokens.add("keyword_extraction", kw_response.total_tokens)
        tracker.add("keyword_extraction", kw_response.total_tokens)
        logger.info(f"  Keywords: {keywords_groups}, Focus: {search_focus}")
    except Exception as e:
        logger.error(f"  Keyword extraction failed: {e}")
        return result

    if dry_run:
        result["keywords"] = keywords_groups
        result["search_focus"] = search_focus
        return result

    all_keywords = [kw for group in keywords_groups for kw in group]

    # Step 2: 先查缓存 — 看之前的 task 是否已经缓存了相关论文
    cached_results = cache.search_by_keywords(all_keywords, max_results=10)
    cache_hits = len(cached_results)
    cache_hits_with_extraction = sum(
        1 for r in cached_results if r.get("full_text_extracted")
    )
    result["cache_hits"] = cache_hits
    result["cache_hits_with_extraction"] = cache_hits_with_extraction

    if cache_hits > 0:
        logger.info(
            f"  Cache hits: {cache_hits} papers "
            f"({cache_hits_with_extraction} with extraction)"
        )
        # 标记已缓存论文被本 task 使用
        for cached_paper in cached_results:
            pid = cached_paper.get("paper_id", "")
            if pid:
                cache.mark_used_by_task(pid, task_id)

    # Step 3: 搜索学术数据库（补充缓存中没有的）
    all_papers: list[Paper] = []
    for kw_group in keywords_groups:
        papers = retriever.search(kw_group, category, max_total=8)
        all_papers.extend(papers)
        time.sleep(1)

    # 去重
    seen = set()
    unique_papers = []
    for p in all_papers:
        key = p.title.lower().strip()
        if key not in seen:
            seen.add(key)
            unique_papers.append(p)
    all_papers = unique_papers

    result["papers_searched"] = len(all_papers)
    logger.info(f"  Found {len(all_papers)} papers from API search")

    if not all_papers:
        result["tokens_used"] = task_tokens.total
        result["token_breakdown"] = {
            "keyword_extraction": task_tokens.keyword_extraction,
            "relevance_check": task_tokens.relevance_check,
            "chunk_reading": task_tokens.chunk_reading,
        }
        return result

    # Step 4: 摘要相关性判断
    relevant_papers = []
    for paper in all_papers[:15]:
        relevant, expected_value, tokens = reader.check_relevance(
            question, paper.title, paper.abstract
        )
        task_tokens.add("relevance_check", tokens)
        tracker.add("relevance_check", tokens)

        if relevant:
            relevant_papers.append((paper, expected_value))
            cache.add_paper(paper, all_keywords)

        time.sleep(0.5)

    result["papers_relevant"] = len(relevant_papers)
    logger.info(f"  Relevant papers: {len(relevant_papers)}/{min(len(all_papers), 15)}")

    # Step 5: 对相关论文执行分块精读
    papers_read = 0
    for paper, expected_value in relevant_papers[:5]:
        full_text = None

        if paper.arxiv_id:
            full_text = retriever.fetch_full_text_arxiv(paper.arxiv_id)
        elif paper.pmcid:
            full_text = retriever.fetch_full_text_pmc(paper.pmcid)

        paper_id = cache._paper_id(paper)

        if not full_text:
            extracted = f"[仅摘要] {paper.abstract[:400]}"
            if paper.tldr:
                extracted = f"TLDR: {paper.tldr}\n{extracted}"
            cache.set_extracted_content(paper_id, extracted)
            cache.mark_used_by_task(paper_id, task_id)
            result["papers_cached"] += 1
            result["reading_details"].append({
                "paper_title": paper.title[:60],
                "mode": "abstract_only",
                "chunks_read": 0,
                "tokens": 0,
            })
            continue

        # 分块精读
        logger.info(f"  Reading: {paper.title[:50]}...")
        reading_result = reader.read_paper(
            question=question,
            paper_title=paper.title,
            full_text=full_text,
            expected_value=expected_value,
        )
        task_tokens.add("chunk_reading", reading_result.tokens_used)
        tracker.add("chunk_reading", reading_result.tokens_used)
        papers_read += 1

        reading_detail = {
            "paper_title": paper.title[:60],
            "mode": "full_text_chunked",
            "chunks_read": reading_result.chunks_read,
            "total_chunks": reading_result.total_chunks,
            "early_exit": reading_result.early_exit,
            "exit_reason": reading_result.exit_reason,
            "tokens": reading_result.tokens_used,
            "extracted_length": len(reading_result.extracted_info),
        }
        result["reading_details"].append(reading_detail)

        if reading_result.relevant and reading_result.extracted_info:
            cache.set_extracted_content(paper_id, reading_result.extracted_info)
            cache.mark_used_by_task(paper_id, task_id)
            result["papers_cached"] += 1
            logger.info(
                f"    ✓ Extracted {len(reading_result.extracted_info)} chars "
                f"(chunks={reading_result.chunks_read}/{reading_result.total_chunks}, "
                f"exit={reading_result.exit_reason})"
            )
        else:
            logger.info(f"    ✗ No useful info (exit={reading_result.exit_reason})")

        time.sleep(1)

    result["papers_read"] = papers_read
    result["tokens_used"] = task_tokens.total
    result["token_breakdown"] = {
        "keyword_extraction": task_tokens.keyword_extraction,
        "relevance_check": task_tokens.relevance_check,
        "chunk_reading": task_tokens.chunk_reading,
        "summarization": task_tokens.summarization,
    }
    return result


def cmd_build(args):
    """主构建流程"""
    config = ConfigLoader("config")
    log_dir = args.output_dir or config.settings.get("logging", {}).get("log_dir", "logs")

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    cache = PaperCache(f"{log_dir}/paper_cache")
    retriever = AcademicRetriever(config.settings.get("retrieval", {}))
    reader = PaperReader(config)
    llm = LLMClient(config.get_plan_agent_config())
    tracker = TokenTracker()

    # 加载历史任务
    tasks = load_historical_tasks()
    logger.info(f"Loaded {len(tasks)} historical tasks")

    # 过滤
    if args.category:
        tasks = [t for t in tasks if t["category"] == args.category]
        logger.info(f"Filtered to {len(tasks)} tasks (category={args.category})")

    if args.limit:
        tasks = tasks[:args.limit]
        logger.info(f"Limited to {args.limit} tasks")

    # 逐个处理
    results = []
    for i, task in enumerate(tasks):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{i+1}/{len(tasks)}] Processing: {task['title'][:60]}")
        logger.info(f"  Category: {task['category']}, Score: {task['score']}")

        try:
            result = build_cache_for_task(
                task, config, cache, retriever, reader, llm, tracker,
                dry_run=args.dry_run,
            )
            results.append(result)
        except Exception as e:
            logger.error(f"  Failed: {e}")
            results.append({"task_id": task["task_id"], "error": str(e)})

        # 进度报告
        if (i + 1) % 5 == 0:
            logger.info(f"\n--- Progress: {i+1}/{len(tasks)} ---")
            logger.info(f"Cache stats: {cache.stats()}")
            logger.info(tracker.report())

    # 最终报告
    print(f"\n{'='*60}")
    print(f"Cache Build Complete")
    print(f"{'='*60}")
    print(f"Tasks processed: {len(results)}")
    print(f"Cache stats: {cache.stats()}")
    print()
    print(tracker.report())

    total_searched = sum(r.get("papers_searched", 0) for r in results)
    total_relevant = sum(r.get("papers_relevant", 0) for r in results)
    total_cached = sum(r.get("papers_cached", 0) for r in results)
    total_cache_hits = sum(r.get("cache_hits", 0) for r in results)
    total_cache_hits_extracted = sum(r.get("cache_hits_with_extraction", 0) for r in results)

    print(f"\n--- Search & Retrieval ---")
    print(f"Papers searched (API):        {total_searched}")
    print(f"Papers judged relevant:       {total_relevant}")
    print(f"Papers cached (with extract): {total_cached}")

    print(f"\n--- Cache Hit Analysis ---")
    print(f"Total cache hits during build:      {total_cache_hits}")
    print(f"  With extraction (reusable):       {total_cache_hits_extracted}")
    print(f"  Without extraction (abstract only):{total_cache_hits - total_cache_hits_extracted}")

    # Per-task token breakdown
    print(f"\n--- Per-Task Token Stats ---")
    task_tokens = [r.get("tokens_used", 0) for r in results if r.get("tokens_used")]
    if task_tokens:
        avg_tokens = sum(task_tokens) / len(task_tokens)
        max_tokens = max(task_tokens)
        min_tokens = min(task_tokens)
        print(f"  Avg tokens/task: {avg_tokens:,.0f}")
        print(f"  Max tokens/task: {max_tokens:,}")
        print(f"  Min tokens/task: {min_tokens:,}")

    # Cache hit progression (when did hits start happening?)
    print(f"\n--- Cache Hit Progression ---")
    cumulative_hits = 0
    milestones = [10, 20, 30, 50, 70, 90]
    for i, r in enumerate(results):
        hits = r.get("cache_hits", 0)
        cumulative_hits += hits
        if (i + 1) in milestones:
            print(f"  After {i+1} tasks: cumulative cache hits = {cumulative_hits}")

    # 保存构建报告
    report = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks_processed": len(results),
        "cache_stats": cache.stats(),
        "token_usage": {
            "keyword_extraction": tracker.keyword_extraction,
            "relevance_check": tracker.relevance_check,
            "chunk_reading": tracker.chunk_reading,
            "summarization": tracker.summarization,
            "total": tracker.total,
        },
        "search_stats": {
            "total_papers_searched": total_searched,
            "total_papers_relevant": total_relevant,
            "total_papers_cached": total_cached,
        },
        "cache_hit_stats": {
            "total_cache_hits": total_cache_hits,
            "with_extraction": total_cache_hits_extracted,
        },
        "per_task_token_stats": {
            "avg": avg_tokens if task_tokens else 0,
            "max": max_tokens if task_tokens else 0,
            "min": min_tokens if task_tokens else 0,
        },
        "per_task_results": results,
    }
    report_path = Path(log_dir) / "cache_build_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved to: {report_path}")


def cmd_stats(args):
    """查看缓存统计"""
    config = ConfigLoader("config")
    log_dir = config.settings.get("logging", {}).get("log_dir", "logs")
    cache = PaperCache(f"{log_dir}/paper_cache")

    stats = cache.stats()
    print(f"Paper Cache Statistics:")
    print(f"  Total papers:      {stats['total_papers']}")
    print(f"  With extraction:   {stats['with_extraction']}")
    print(f"  Keywords indexed:  {stats['keywords_indexed']}")

    # 显示 top 关键词
    if cache._keyword_index:
        sorted_kws = sorted(cache._keyword_index.items(), key=lambda x: len(x[1]), reverse=True)
        print(f"\nTop 20 keywords:")
        for kw, paper_ids in sorted_kws[:20]:
            print(f"  {kw}: {len(paper_ids)} papers")

    # 显示各数据源分布
    source_count: dict[str, int] = {}
    for paper in cache._papers.values():
        src = paper.get("source", "unknown")
        source_count[src] = source_count.get(src, 0) + 1
    if source_count:
        print(f"\nBy source:")
        for src, count in sorted(source_count.items(), key=lambda x: x[1], reverse=True):
            print(f"  {src}: {count}")


def main():
    parser = argparse.ArgumentParser(description="论文缓存预建工具")
    parser.add_argument("--limit", type=int, help="最多处理 N 个任务")
    parser.add_argument("--category", type=str, help="只处理指定领域")
    parser.add_argument("--dry-run", action="store_true", help="只提取关键词，不实际检索")
    parser.add_argument("--stats", action="store_true", help="查看缓存统计")
    parser.add_argument("--output-dir", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
