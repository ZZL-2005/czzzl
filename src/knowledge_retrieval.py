"""
知识检索编排模块 (v4)
完整流程：关键词提取 → 缓存查询 → API检索 → LLM筛选 → 精读提取 → 注入上下文
"""

import logging
from dataclasses import dataclass

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader
from .retriever import AcademicRetriever, Paper
from .paper_cache import PaperCache

logger = logging.getLogger(__name__)

KEYWORD_EXTRACTION_PROMPT = """你是一个学术检索助手。根据以下题目，提取用于学术论文检索的英文关键词。

## 题目
{title}

## 内容
{content}

## 要求
1. 提取 3-5 组搜索关键词，每组 2-4 个英文词
2. 关键词应覆盖题目的核心概念、方法论、应用领域
3. 包含学术术语和同义词/近义词
4. 以 JSON 格式输出：{{"keywords": [["keyword1", "keyword2"], ["keyword3", "keyword4"]], "domain_hints": ["关键学术领域提示"]}}
"""

PAPER_SELECTION_PROMPT = """你是一个学术文献筛选专家。从以下检索到的论文中，选出与题目最相关的论文。

## 原始题目
{title}
{content}

## 检索到的论文列表
{papers_text}

## 要求
1. 选出最相关的 {top_k} 篇论文（按相关性排序）
2. 简述每篇论文对回答题目的价值
3. 以 JSON 格式输出：
{{"selected": [{{"index": 0, "reason": "与题目直接相关的原因"}}, ...]}}
"""

DEEP_EXTRACTION_PROMPT = """你是一个学术信息提取专家。从以下论文内容中，提取与研究问题相关的关键信息。

## 研究问题
{question}

## 论文标题
{paper_title}

## 论文内容
{paper_content}

## 要求
提取与研究问题直接相关的：
1. 关键发现和结论
2. 重要数据、数值、实验结果
3. 方法论要点
4. 与问题相关的理论依据

限制在 300 字以内，只保留最有价值的信息。直接输出提取的内容，不要加任何前缀。
"""


@dataclass
class RetrievalResult:
    """检索结果"""
    context_text: str
    papers_used: list[dict]
    total_papers_found: int
    cache_hits: int
    tokens_used: int


class KnowledgeRetrieval:
    """
    知识检索编排器
    协调关键词提取、论文检索、缓存查询、LLM精读的完整流程
    """

    def __init__(self, config_loader: ConfigLoader, paper_cache: PaperCache):
        self.config = config_loader
        self.cache = paper_cache
        retrieval_config = config_loader.settings.get("retrieval", {})
        self.retriever = AcademicRetriever(retrieval_config)
        self.llm = LLMClient(config_loader.get_plan_agent_config())

        self.enabled = retrieval_config.get("enabled", True)
        self.max_papers = retrieval_config.get("max_papers_to_use", 5)
        self.enable_deep_read = retrieval_config.get("enable_deep_read", False)
        self.max_context_chars = retrieval_config.get("max_context_chars", 4000)

    def retrieve(self, title: str, content: str, category: str, task_id: str = "") -> RetrievalResult:
        """
        完整检索流程
        返回可直接注入 Expert prompt 的上下文文本
        """
        if not self.enabled:
            return RetrievalResult("", [], 0, 0, 0)

        tokens_used = 0
        cache_hits = 0

        # Stage 1: LLM 提取关键词
        keywords_groups, kw_tokens = self._extract_keywords(title, content)
        tokens_used += kw_tokens

        if not keywords_groups:
            logger.warning("No keywords extracted, skipping retrieval")
            return RetrievalResult("", [], 0, 0, tokens_used)

        all_keywords = [kw for group in keywords_groups for kw in group]
        logger.info(f"Extracted keywords: {keywords_groups}")

        # Stage 2: 先查缓存
        cached_papers = self.cache.search_by_keywords(all_keywords, max_results=self.max_papers)
        cache_hits = len(cached_papers)

        # Stage 3: API 检索新论文
        fresh_papers = []
        if cache_hits < self.max_papers:
            for kw_group in keywords_groups:
                papers = self.retriever.search(kw_group, category, max_total=10)
                fresh_papers.extend(papers)
                # 添加到缓存
                self.cache.add_papers(papers, kw_group)

        # 合并去重：缓存论文 + 新检索论文
        all_candidates = self._merge_candidates(cached_papers, fresh_papers)
        total_found = len(all_candidates)
        logger.info(f"Total candidates: {total_found} (cache={cache_hits}, fresh={len(fresh_papers)})")

        if not all_candidates:
            return RetrievalResult("", [], 0, cache_hits, tokens_used)

        # Stage 4: LLM 筛选最相关论文
        selected, select_tokens = self._select_papers(
            title, content, all_candidates, top_k=self.max_papers
        )
        tokens_used += select_tokens

        if not selected:
            # 筛选失败，fallback 使用前 N 篇
            selected = all_candidates[:self.max_papers]

        # Stage 5: 构建上下文（精读或仅用摘要）
        context_parts = []
        papers_used = []

        for i, paper_info in enumerate(selected):
            paper_id = paper_info.get("paper_id", "")
            title_p = paper_info.get("title", "")

            # 检查是否有缓存的精读内容
            extracted = self.cache.get_extracted_content(paper_id) if paper_id else None

            if extracted:
                context_parts.append(f"[{i+1}] {title_p}\n{extracted}")
                logger.info(f"  Paper {i+1}: reusing cached extraction for {title_p[:40]}")
            elif self.enable_deep_read and paper_info.get("full_text"):
                # 精读提取
                extracted, ext_tokens = self._deep_extract(
                    title, content, title_p, paper_info["full_text"]
                )
                tokens_used += ext_tokens
                if extracted and paper_id:
                    self.cache.set_extracted_content(paper_id, extracted)
                context_parts.append(f"[{i+1}] {title_p}\n{extracted}")
            else:
                # 使用摘要 + TLDR
                abstract = paper_info.get("abstract", "")
                tldr = paper_info.get("tldr", "")
                entry = f"[{i+1}] {title_p}"
                if tldr:
                    entry += f"\nTLDR: {tldr}"
                entry += f"\nAbstract: {abstract[:500]}"
                context_parts.append(entry)

            if paper_id:
                self.cache.mark_used_by_task(paper_id, task_id)
            papers_used.append({
                "title": title_p,
                "paper_id": paper_id,
                "source": paper_info.get("source", ""),
                "year": paper_info.get("year"),
            })

        # 组装最终上下文
        context_text = "\n\n".join(context_parts)
        if len(context_text) > self.max_context_chars:
            context_text = context_text[:self.max_context_chars] + "\n...[truncated]"

        return RetrievalResult(
            context_text=context_text,
            papers_used=papers_used,
            total_papers_found=total_found,
            cache_hits=cache_hits,
            tokens_used=tokens_used,
        )

    def _extract_keywords(self, title: str, content: str) -> tuple[list[list[str]], int]:
        """用 LLM 从题目提取搜索关键词"""
        import json
        prompt = KEYWORD_EXTRACTION_PROMPT.format(
            title=title,
            content=content[:1000],
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            result, response = self.llm.chat_json(messages)
            keywords = result.get("keywords", [])
            tokens = response.total_tokens
            return keywords, tokens
        except Exception as e:
            logger.error(f"Keyword extraction failed: {e}")
            # Fallback: 简单分词
            fallback = self._simple_keyword_extract(title)
            return [fallback] if fallback else [], 0

    def _simple_keyword_extract(self, title: str) -> list[str]:
        """简单 fallback 关键词提取"""
        import re
        words = re.findall(r"[a-zA-Z]{3,}", title)
        stop_words = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were"}
        keywords = [w.lower() for w in words if w.lower() not in stop_words]
        return keywords[:5]

    def _select_papers(
        self, title: str, content: str, candidates: list[dict], top_k: int
    ) -> tuple[list[dict], int]:
        """LLM 从候选论文中筛选最相关的"""
        import json

        if len(candidates) <= top_k:
            return candidates, 0

        papers_text = ""
        for i, p in enumerate(candidates[:20]):
            tldr = p.get("tldr", "")
            abstract = p.get("abstract", "")[:200]
            papers_text += f"\n[{i}] {p['title']} ({p.get('year', '?')})\n"
            if tldr:
                papers_text += f"    TLDR: {tldr}\n"
            papers_text += f"    Abstract: {abstract}\n"

        prompt = PAPER_SELECTION_PROMPT.format(
            title=title,
            content=content[:500],
            papers_text=papers_text,
            top_k=top_k,
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            result, response = self.llm.chat_json(messages)
            selected_indices = [s["index"] for s in result.get("selected", [])]
            selected = [candidates[i] for i in selected_indices if i < len(candidates)]
            return selected, response.total_tokens
        except Exception as e:
            logger.warning(f"Paper selection failed: {e}, using top-{top_k} by order")
            return candidates[:top_k], 0

    def _deep_extract(
        self, question_title: str, question_content: str,
        paper_title: str, paper_text: str,
    ) -> tuple[str, int]:
        """LLM 从论文全文中定向提取相关信息"""
        question = f"{question_title}\n{question_content[:300]}"
        # 截断过长的论文文本
        paper_content = paper_text[:15000]

        prompt = DEEP_EXTRACTION_PROMPT.format(
            question=question,
            paper_title=paper_title,
            paper_content=paper_content,
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            response = self.llm.chat(messages)
            return response.content.strip(), response.total_tokens
        except Exception as e:
            logger.error(f"Deep extraction failed for {paper_title[:30]}: {e}")
            return "", 0

    def _merge_candidates(self, cached: list[dict], fresh: list[Paper]) -> list[dict]:
        """合并缓存论文和新检索论文，去重"""
        seen_titles = set()
        merged = []

        for p in cached:
            key = p["title"].lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                merged.append(p)

        for p in fresh:
            key = p.title.lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                merged.append({
                    "paper_id": PaperCache._paper_id(p),
                    "title": p.title,
                    "abstract": p.abstract,
                    "authors": p.authors,
                    "year": p.year,
                    "doi": p.doi,
                    "url": p.url,
                    "source": p.source,
                    "tldr": p.tldr,
                    "arxiv_id": p.arxiv_id,
                    "pmcid": p.pmcid,
                    "full_text": p.full_text,
                })

        return merged
