"""
论文缓存与跨任务知识共享模块 (v4)
全局论文池 + 倒排索引，支持跨任务复用检索结果和精读提取
"""

import json
import logging
import hashlib
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from threading import Lock

from .retriever import Paper

logger = logging.getLogger(__name__)


class PaperCache:
    """
    全局论文缓存池
    - 缓存检索到的论文（摘要 + 全文提取）
    - 倒排索引支持关键词快速匹配
    - 精读结果(LLM提取)跨任务复用
    - 线程安全
    """

    def __init__(self, cache_dir: str = "logs/paper_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.papers_file = self.cache_dir / "papers.json"
        self.index_file = self.cache_dir / "keyword_index.json"
        self._lock = Lock()
        self._papers: dict[str, dict] = {}
        self._keyword_index: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        """从磁盘加载缓存"""
        if self.papers_file.exists():
            try:
                self._papers = json.loads(self.papers_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load paper cache: {e}")
                self._papers = {}

        if self.index_file.exists():
            try:
                self._keyword_index = json.loads(self.index_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load keyword index: {e}")
                self._keyword_index = {}

    def _save(self):
        """落盘"""
        self.papers_file.write_text(
            json.dumps(self._papers, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.index_file.write_text(
            json.dumps(self._keyword_index, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _paper_id(paper: Paper) -> str:
        """生成论文唯一标识"""
        if paper.doi:
            return f"doi:{paper.doi}"
        if paper.arxiv_id:
            return f"arxiv:{paper.arxiv_id}"
        key = f"{paper.title}:{paper.year or ''}"
        return f"hash:{hashlib.md5(key.encode()).hexdigest()[:12]}"

    def add_paper(self, paper: Paper, keywords: list[str]) -> str:
        """添加论文到缓存，返回 paper_id"""
        paper_id = self._paper_id(paper)
        with self._lock:
            if paper_id not in self._papers:
                self._papers[paper_id] = {
                    "title": paper.title,
                    "abstract": paper.abstract,
                    "authors": paper.authors,
                    "year": paper.year,
                    "doi": paper.doi,
                    "url": paper.url,
                    "source": paper.source,
                    "tldr": paper.tldr,
                    "arxiv_id": paper.arxiv_id,
                    "pmcid": paper.pmcid,
                    "full_text_extracted": None,
                    "cached_at": time.time(),
                    "used_by_tasks": [],
                }
            # 更新倒排索引
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower not in self._keyword_index:
                    self._keyword_index[kw_lower] = []
                if paper_id not in self._keyword_index[kw_lower]:
                    self._keyword_index[kw_lower].append(paper_id)
            self._save()
        return paper_id

    def add_papers(self, papers: list[Paper], keywords: list[str]) -> list[str]:
        """批量添加论文"""
        ids = []
        for p in papers:
            ids.append(self.add_paper(p, keywords))
        return ids

    def search_by_keywords(self, keywords: list[str], max_results: int = 10) -> list[dict]:
        """
        在缓存中按关键词搜索
        返回匹配的论文（按命中关键词数量排序）
        """
        with self._lock:
            score_map: dict[str, int] = {}
            for kw in keywords:
                kw_lower = kw.lower()
                matching_ids = self._keyword_index.get(kw_lower, [])
                for pid in matching_ids:
                    score_map[pid] = score_map.get(pid, 0) + 1

            # 也做标题/摘要模糊匹配
            for pid, paper_data in self._papers.items():
                title_lower = paper_data["title"].lower()
                abstract_lower = paper_data["abstract"].lower()
                for kw in keywords:
                    kw_lower = kw.lower()
                    if kw_lower in title_lower:
                        score_map[pid] = score_map.get(pid, 0) + 2
                    elif kw_lower in abstract_lower:
                        score_map[pid] = score_map.get(pid, 0) + 1

            ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
            results = []
            for pid, score in ranked[:max_results]:
                if pid in self._papers:
                    entry = self._papers[pid].copy()
                    entry["paper_id"] = pid
                    entry["match_score"] = score
                    results.append(entry)
            return results

    def get_extracted_content(self, paper_id: str) -> str | None:
        """获取已缓存的精读提取内容"""
        with self._lock:
            paper = self._papers.get(paper_id)
            if paper:
                return paper.get("full_text_extracted")
        return None

    def set_extracted_content(self, paper_id: str, extracted: str):
        """保存精读提取内容（跨任务复用）"""
        with self._lock:
            if paper_id in self._papers:
                self._papers[paper_id]["full_text_extracted"] = extracted
                self._save()

    def mark_used_by_task(self, paper_id: str, task_id: str):
        """标记论文被哪个 task 使用"""
        with self._lock:
            if paper_id in self._papers:
                used = self._papers[paper_id].get("used_by_tasks", [])
                if task_id not in used:
                    used.append(task_id)
                    self._papers[paper_id]["used_by_tasks"] = used
                    self._save()

    def get_paper(self, paper_id: str) -> dict | None:
        with self._lock:
            return self._papers.get(paper_id)

    def stats(self) -> dict:
        """缓存统计"""
        with self._lock:
            total = len(self._papers)
            with_extraction = sum(
                1 for p in self._papers.values() if p.get("full_text_extracted")
            )
            return {
                "total_papers": total,
                "with_extraction": with_extraction,
                "keywords_indexed": len(self._keyword_index),
            }
