"""
学术论文检索模块 (v4)
支持多数据源检索，根据领域自动选择合适的学术搜索平台。
两阶段检索：粗筛(摘要) + 精读(全文提取，可选)
"""

import logging
import time
import re
import tarfile
import io
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .exceptions import retry_with_backoff, RetryableError

logger = logging.getLogger(__name__)

DOMAIN_SOURCES = {
    "natural_science": ["semantic_scholar", "openalex", "arxiv"],
    "law": ["semantic_scholar", "openalex"],
    "finance": ["semantic_scholar", "openalex"],
    "industrial_engineering": ["semantic_scholar", "openalex", "arxiv"],
    "medical_health": ["pubmed", "semantic_scholar", "openalex"],
}


@dataclass
class Paper:
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    source: str = ""
    tldr: str | None = None
    full_text: str | None = None
    arxiv_id: str | None = None
    pmcid: str | None = None


class AcademicRetriever:
    """学术论文检索器，支持多数据源"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.timeout = self.config.get("timeout_seconds", 30)
        self.max_results_per_source = self.config.get("max_results_per_source", 10)
        self.client = httpx.Client(timeout=self.timeout, follow_redirects=True)

    def search(self, keywords: list[str], category: str, max_total: int = 20) -> list[Paper]:
        """
        根据领域选择数据源，检索论文
        返回按相关性排序的 Paper 列表
        """
        sources = DOMAIN_SOURCES.get(category, ["semantic_scholar", "openalex"])
        all_papers = []
        seen_titles = set()

        for source in sources:
            if len(all_papers) >= max_total:
                break
            try:
                papers = self._search_source(source, keywords)
                for p in papers:
                    title_key = p.title.lower().strip()
                    if title_key not in seen_titles:
                        seen_titles.add(title_key)
                        all_papers.append(p)
            except Exception as e:
                logger.warning(f"Search failed for source={source}: {e}")
                continue

        return all_papers[:max_total]

    def _search_source(self, source: str, keywords: list[str]) -> list[Paper]:
        dispatch = {
            "semantic_scholar": self._search_semantic_scholar,
            "arxiv": self._search_arxiv,
            "arxiv_qfin": self._search_arxiv_qfin,
            "openalex": self._search_openalex,
            "pubmed": self._search_pubmed,
        }
        fn = dispatch.get(source)
        if not fn:
            logger.warning(f"Unknown source: {source}")
            return []
        return fn(keywords)

    def _search_semantic_scholar(self, keywords: list[str]) -> list[Paper]:
        """Semantic Scholar API - 免费，有 TLDR，但有速率限制(100 req/5min)"""
        query = " ".join(keywords)
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": self.max_results_per_source,
            "fields": "title,abstract,authors,year,externalIds,tldr,url",
        }
        try:
            resp = self.client.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                logger.warning("Semantic Scholar rate limited, waiting 5s...")
                time.sleep(5)
                resp = self.client.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Semantic Scholar returned {resp.status_code}")
                return []
        except Exception as e:
            logger.warning(f"Semantic Scholar request failed: {e}")
            return []

        data = resp.json()
        papers = []
        for item in data.get("data", []):
            abstract = item.get("abstract") or ""
            if not abstract:
                continue
            tldr_obj = item.get("tldr")
            tldr = tldr_obj.get("text") if tldr_obj else None
            external_ids = item.get("externalIds") or {}
            papers.append(Paper(
                title=item.get("title", ""),
                abstract=abstract,
                authors=[a.get("name", "") for a in (item.get("authors") or [])[:5]],
                year=item.get("year"),
                doi=external_ids.get("DOI"),
                url=item.get("url", ""),
                source="semantic_scholar",
                tldr=tldr,
                arxiv_id=external_ids.get("ArXiv"),
            ))
        return papers

    def _search_arxiv(self, keywords: list[str]) -> list[Paper]:
        """arXiv API (注意：必须用 HTTPS)"""
        query = " AND ".join(f"all:{kw}" for kw in keywords)
        url = "https://export.arxiv.org/api/query"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": self.max_results_per_source,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        try:
            resp = self.client.get(url, params=params, timeout=15)
        except Exception as e:
            logger.warning(f"arXiv request timeout/failed: {e}")
            return []
        if resp.status_code != 200:
            logger.warning(f"arXiv returned {resp.status_code}")
            return []

        return self._parse_arxiv_atom(resp.text)

    def _search_arxiv_qfin(self, keywords: list[str]) -> list[Paper]:
        """arXiv quantitative finance category"""
        query = f"cat:q-fin.* AND ({' OR '.join(f'all:{kw}' for kw in keywords)})"
        url = "https://export.arxiv.org/api/query"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": self.max_results_per_source,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        resp = self.client.get(url, params=params)
        if resp.status_code != 200:
            return []
        return self._parse_arxiv_atom(resp.text)

    def _parse_arxiv_atom(self, xml_text: str) -> list[Paper]:
        """解析 arXiv Atom feed"""
        papers = []
        entries = re.findall(r"<entry>(.*?)</entry>", xml_text, re.DOTALL)
        for entry in entries:
            title = self._extract_xml_tag(entry, "title").strip().replace("\n", " ")
            abstract = self._extract_xml_tag(entry, "summary").strip().replace("\n", " ")
            if not abstract:
                continue

            authors = re.findall(r"<name>(.*?)</name>", entry)
            published = self._extract_xml_tag(entry, "published")
            year = int(published[:4]) if published else None

            id_url = self._extract_xml_tag(entry, "id")
            arxiv_id = id_url.split("/abs/")[-1] if "/abs/" in id_url else None

            papers.append(Paper(
                title=title,
                abstract=abstract,
                authors=authors[:5],
                year=year,
                url=id_url,
                source="arxiv",
                arxiv_id=arxiv_id,
            ))
        return papers

    def _search_openalex(self, keywords: list[str]) -> list[Paper]:
        """OpenAlex API - 免费，覆盖面广"""
        query = " ".join(keywords)
        url = "https://api.openalex.org/works"
        params = {
            "search": query,
            "per_page": self.max_results_per_source,
            "select": "title,authorships,publication_year,doi,abstract_inverted_index,id",
        }
        headers = {"User-Agent": "ArenaAgent/1.0 (mailto:research@example.com)"}
        try:
            resp = self.client.get(url, params=params, headers=headers, timeout=15)
        except Exception as e:
            logger.warning(f"OpenAlex request failed: {e}")
            return []
        if resp.status_code != 200:
            logger.warning(f"OpenAlex returned {resp.status_code}")
            return []

        data = resp.json()
        papers = []
        for item in data.get("results", []):
            abstract = self._reconstruct_abstract(item.get("abstract_inverted_index"))
            title = item.get("title", "")
            if not title:
                continue
            # 跳过没有摘要的论文（对后续流程无价值）
            if not abstract or len(abstract) < 50:
                continue
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in (item.get("authorships") or [])[:5]
            ]
            doi = item.get("doi", "")
            papers.append(Paper(
                title=title,
                abstract=abstract,
                authors=authors,
                year=item.get("publication_year"),
                doi=doi,
                url=doi or item.get("id", ""),
                source="openalex",
            ))
        return papers

    def _search_pubmed(self, keywords: list[str]) -> list[Paper]:
        """PubMed E-utilities API"""
        query = " ".join(keywords)
        # Step 1: esearch 获取 ID 列表
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {
            "db": "pubmed",
            "term": query,
            "retmax": self.max_results_per_source,
            "retmode": "json",
            "sort": "relevance",
        }
        resp = self.client.get(search_url, params=params)
        if resp.status_code != 200:
            return []

        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

        # Step 2: efetch 获取详情
        fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        params = {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
        }
        resp = self.client.get(fetch_url, params=params)
        if resp.status_code != 200:
            return []

        return self._parse_pubmed_xml(resp.text)

    def _parse_pubmed_xml(self, xml_text: str) -> list[Paper]:
        """解析 PubMed XML"""
        papers = []
        articles = re.findall(r"<PubmedArticle>(.*?)</PubmedArticle>", xml_text, re.DOTALL)
        for article in articles:
            title = self._extract_xml_tag(article, "ArticleTitle")
            abstract_parts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", article, re.DOTALL)
            abstract = " ".join(abstract_parts)
            abstract = re.sub(r"<[^>]+>", "", abstract)
            if not abstract:
                continue

            authors = []
            author_blocks = re.findall(r"<Author[^>]*>(.*?)</Author>", article, re.DOTALL)
            for ab in author_blocks[:5]:
                last = self._extract_xml_tag(ab, "LastName")
                first = self._extract_xml_tag(ab, "ForeName")
                if last:
                    authors.append(f"{last} {first}".strip())

            year_str = self._extract_xml_tag(article, "Year")
            year = int(year_str) if year_str and year_str.isdigit() else None

            pmid = self._extract_xml_tag(article, "PMID")
            doi_match = re.search(r'<ArticleId IdType="doi">(.*?)</ArticleId>', article)
            doi = doi_match.group(1) if doi_match else None

            pmc_match = re.search(r'<ArticleId IdType="pmc">(.*?)</ArticleId>', article)
            pmcid = pmc_match.group(1) if pmc_match else None

            papers.append(Paper(
                title=title,
                abstract=abstract,
                authors=authors,
                year=year,
                doi=doi,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                source="pubmed",
                pmcid=pmcid,
            ))
        return papers

    def fetch_full_text_arxiv(self, arxiv_id: str) -> str | None:
        """
        下载 arXiv 论文 LaTeX 源码并提取纯文本
        返回清洗后的文本，或 None
        """
        if not arxiv_id:
            return None
        try:
            source_url = f"https://arxiv.org/e-print/{arxiv_id}"
            resp = self.client.get(source_url, timeout=60)
            if resp.status_code != 200:
                return None

            content = resp.content
            text = self._extract_text_from_latex_tar(content)
            if text and len(text) > 200:
                return text
        except Exception as e:
            logger.warning(f"Failed to fetch arXiv full text for {arxiv_id}: {e}")
        return None

    def fetch_full_text_pmc(self, pmcid: str) -> str | None:
        """从 PubMed Central 获取全文（XML → 清洗为纯文本）"""
        if not pmcid:
            return None
        try:
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            params = {"db": "pmc", "id": pmcid, "retmode": "xml"}
            resp = self.client.get(url, params=params, timeout=30)
            if resp.status_code != 200:
                return None
            return self._clean_pmc_xml(resp.text)
        except Exception as e:
            logger.warning(f"Failed to fetch PMC full text for {pmcid}: {e}")
        return None

    def _clean_pmc_xml(self, xml_text: str) -> str | None:
        """从 PMC XML 中提取正文，去掉元数据和标签"""
        # 提取 <body> 部分（正文）
        body_match = re.search(r"<body>(.*?)</body>", xml_text, re.DOTALL)
        if not body_match:
            # fallback: 提取 <abstract> + 全文
            abstract_match = re.search(r"<abstract>(.*?)</abstract>", xml_text, re.DOTALL)
            text = abstract_match.group(1) if abstract_match else xml_text

        else:
            text = body_match.group(1)
            # 也提取 abstract
            abstract_match = re.search(r"<abstract>(.*?)</abstract>", xml_text, re.DOTALL)
            if abstract_match:
                text = abstract_match.group(1) + "\n\n" + text

        # 提取 section 标题
        text = re.sub(r"<title>(.*?)</title>", r"\n\n## \1\n", text)
        # 去掉表格和图片环境
        text = re.sub(r"<table-wrap.*?</table-wrap>", "[表格]", text, flags=re.DOTALL)
        text = re.sub(r"<fig .*?</fig>", "[图片]", text, flags=re.DOTALL)
        # 去掉引用标记但保留文字
        text = re.sub(r"<xref[^>]*>(.*?)</xref>", r"\1", text)
        # 去掉所有剩余 XML 标签
        text = re.sub(r"<[^>]+>", " ", text)
        # 清理 HTML entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#x2019;", "'")
        # 清理多余空白
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = text.strip()

        if len(text) > 30000:
            text = text[:30000]
        return text if len(text) > 500 else None

    def _extract_text_from_latex_tar(self, content: bytes) -> str | None:
        """从 tar.gz 中提取 .tex 文件并清洗为纯文本"""
        try:
            tar = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz")
        except (tarfile.ReadError, Exception):
            # 可能是单个 .tex 文件而非 tar
            try:
                import gzip
                text = gzip.decompress(content).decode("utf-8", errors="ignore")
                return self._clean_latex(text)
            except Exception:
                return None

        tex_content = ""
        for member in tar.getmembers():
            if member.name.endswith(".tex") and member.size > 1000:
                f = tar.extractfile(member)
                if f:
                    tex_content += f.read().decode("utf-8", errors="ignore") + "\n"

        if tex_content:
            return self._clean_latex(tex_content)
        return None

    def _clean_latex(self, tex: str) -> str:
        """将 LaTeX 源码清洗为可读纯文本"""
        # 去掉 preamble (documentclass 到 begin{document} 之间的内容)
        doc_begin = tex.find("\\begin{document}")
        if doc_begin != -1:
            tex = tex[doc_begin + len("\\begin{document}"):]
        doc_end = tex.find("\\end{document}")
        if doc_end != -1:
            tex = tex[:doc_end]

        # 去掉注释
        tex = re.sub(r"(?m)%.*$", "", tex)
        # 去掉 newcommand/renewcommand/def 定义
        tex = re.sub(r"\\(new|renew)command\{[^}]*\}(\[[^\]]*\])?\{[^}]*\}", "", tex)
        tex = re.sub(r"\\def\\[a-zA-Z]+[^{]*\{[^}]*\}", "", tex)
        # 去掉 usepackage, input, include
        tex = re.sub(r"\\(usepackage|input|include|bibliography|bibliographystyle)(\[[^\]]*\])?\{[^}]*\}", "", tex)
        # 去掉图表环境
        tex = re.sub(r"\\begin\{(figure|table|tikzpicture)\}.*?\\end\{(figure|table|tikzpicture)\}", "[图表]", tex, flags=re.DOTALL)
        # 去掉数学公式环境
        tex = re.sub(r"\\begin\{(equation|align|gather|multline|eqnarray)\*?\}.*?\\end\{(equation|align|gather|multline|eqnarray)\*?\}", "[公式]", tex, flags=re.DOTALL)
        tex = re.sub(r"\$\$.*?\$\$", "[公式]", tex, flags=re.DOTALL)
        # 保留短行内公式，去掉长公式
        tex = re.sub(r"\$([^$]{1,30})\$", r"\1", tex)
        tex = re.sub(r"\$[^$]+\$", "[公式]", tex)
        # 提取 section 标题
        tex = re.sub(r"\\(section|subsection|subsubsection)\*?\{([^}]*)\}", r"\n\n## \2\n", tex)
        # 去掉格式命令但保留参数
        tex = re.sub(r"\\(textbf|textit|emph|underline|texttt|mathrm)\{([^}]*)\}", r"\2", tex)
        tex = re.sub(r"\\(title|author|date|maketitle|abstract)", "", tex)
        # 去掉引用
        tex = re.sub(r"\\(cite|ref|label|eqref|footnote|url)\{[^}]*\}", "", tex)
        # 列表项
        tex = re.sub(r"\\item\s*", "\n- ", tex)
        # 去掉环境标记
        tex = re.sub(r"\\(begin|end)\{[^}]*\}", "", tex)
        # 去掉其余 LaTeX 命令
        tex = re.sub(r"\\[a-zA-Z]+(\[[^\]]*\])?\{([^}]*)\}", r"\2", tex)
        tex = re.sub(r"\\[a-zA-Z]+", " ", tex)
        # 去掉花括号
        tex = re.sub(r"[{}]", "", tex)
        # 清理多余空白
        tex = re.sub(r"\n{3,}", "\n\n", tex)
        tex = re.sub(r"[ \t]+", " ", tex)
        # 去掉只有空白/符号的短行
        lines = tex.split("\n")
        lines = [l for l in lines if len(l.strip()) > 3 or l.strip().startswith("##")]
        tex = "\n".join(lines)
        # 截断过长文本
        if len(tex) > 25000:
            tex = tex[:25000]
        return tex.strip()

    def _reconstruct_abstract(self, inverted_index: dict | None) -> str:
        """从 OpenAlex 倒排索引重建摘要文本"""
        if not inverted_index:
            return ""
        word_positions = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort(key=lambda x: x[0])
        return " ".join(w for _, w in word_positions)

    @staticmethod
    def _extract_xml_tag(text: str, tag: str) -> str:
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL)
        return match.group(1).strip() if match else ""

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
