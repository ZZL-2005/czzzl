"""
渐进式分块论文阅读器 (v4)

策略：
1. LLM 读摘要判断是否与题目匹配
2. 若匹配 → 获取全文 → 分块阅读
3. 每块阅读后 LLM 判断：
   - 已找到关键信息 → 输出提取结果，退出
   - 需要继续 → 滑动窗口（移除前半上下文），读下一块
   - 无价值 → 提前退出
4. 最终缓存提取的关键信息
"""

import logging
from dataclasses import dataclass, field

from .llm_client import LLMClient, LLMResponse
from .config_loader import ConfigLoader

logger = logging.getLogger(__name__)

# 摘要相关性判断
ABSTRACT_RELEVANCE_PROMPT = """判断以下论文是否与研究问题相关，是否值得深入阅读全文。

## 研究问题
{question}

## 论文标题
{paper_title}

## 论文摘要
{abstract}

## 要求
以 JSON 格式回答：
{{
  "relevant": true/false,
  "reason": "简述相关/不相关的原因（一句话）",
  "expected_value": "如果相关，预期能从全文中获取什么信息（一句话）"
}}
"""

# 分块阅读 + 判断是否继续
CHUNK_READ_PROMPT = """你正在为一个研究问题从论文中提取关键信息。

## 研究问题
{question}

## 期望获取的信息
{expected_value}

## 当前已提取的信息
{accumulated_info}

## 当前阅读的内容（第 {chunk_num}/{total_chunks} 块）
{chunk_content}

## 要求
以 JSON 格式回答：
{{
  "found_info": "从本块中提取到的与研究问题相关的关键信息（若无则为空字符串）",
  "should_continue": true/false,
  "reason": "是否需要继续阅读下一块的原因（一句话）",
  "confidence": "对已获取信息的充分程度：high/medium/low"
}}
"""

# 最终汇总
FINAL_SUMMARY_PROMPT = """根据从论文分块阅读中提取的信息，生成一份精炼的知识摘要。

## 研究问题
{question}

## 论文标题
{paper_title}

## 提取的原始信息
{raw_extractions}

## 要求
生成一份 200-400 字的精炼摘要，只保留：
1. 与研究问题直接相关的关键发现
2. 重要的数据、数值、实验结果
3. 方法论要点或理论依据
直接输出摘要内容，不要前缀说明。
"""


@dataclass
class ReadingResult:
    """阅读结果"""
    paper_title: str
    relevant: bool
    extracted_info: str = ""
    chunks_read: int = 0
    total_chunks: int = 0
    early_exit: bool = False
    exit_reason: str = ""
    tokens_used: int = 0


class PaperReader:
    """
    渐进式分块论文阅读器
    - 先判断摘要相关性
    - 再分块精读全文，滑动窗口管理上下文
    - 每步都可提前退出
    """

    def __init__(self, config_loader: ConfigLoader):
        self.config = config_loader
        self.llm = LLMClient(config_loader.get_plan_agent_config())
        reader_config = config_loader.settings.get("reader", {})
        self.chunk_size = reader_config.get("chunk_size_chars", 3000)
        self.max_chunks = reader_config.get("max_chunks", 8)
        self.context_window_chunks = reader_config.get("context_window_chunks", 2)

    def check_relevance(self, question: str, paper_title: str, abstract: str) -> tuple[bool, str, int]:
        """
        判断论文摘要是否与问题相关
        返回 (relevant, expected_value, tokens_used)
        """
        prompt = ABSTRACT_RELEVANCE_PROMPT.format(
            question=question,
            paper_title=paper_title,
            abstract=abstract,
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            result, response = self.llm.chat_json(messages)
            relevant = result.get("relevant", False)
            expected_value = result.get("expected_value", "")
            logger.info(
                f"Relevance check: '{paper_title[:40]}' → "
                f"{'RELEVANT' if relevant else 'NOT RELEVANT'} ({result.get('reason', '')})"
            )
            return relevant, expected_value, response.total_tokens
        except Exception as e:
            logger.warning(f"Relevance check failed for '{paper_title[:40]}': {e}")
            return False, "", 0

    def read_paper(self, question: str, paper_title: str, full_text: str, expected_value: str = "") -> ReadingResult:
        """
        分块渐进式阅读论文全文

        策略：
        - 将全文分块（每块 chunk_size 字符）
        - 逐块阅读，每块后 LLM 判断是否继续
        - 滑动窗口：只保留最近 context_window_chunks 块的提取结果在上下文中
        - 达到 high confidence 或判定无需继续时提前退出
        """
        chunks = self._split_into_chunks(full_text)
        total_chunks = len(chunks)

        if total_chunks == 0:
            return ReadingResult(
                paper_title=paper_title, relevant=False,
                exit_reason="empty_text", total_chunks=0,
            )

        # 限制最大块数
        chunks = chunks[:self.max_chunks]
        total_chunks = len(chunks)

        accumulated_extractions: list[str] = []
        tokens_used = 0

        for i, chunk in enumerate(chunks):
            chunk_num = i + 1

            # 构建已积累信息的摘要（滑动窗口：只保留最近 N 块的提取）
            recent_extractions = accumulated_extractions[-self.context_window_chunks:]
            accumulated_info = "\n".join(recent_extractions) if recent_extractions else "（暂无）"

            # LLM 阅读当前块
            prompt = CHUNK_READ_PROMPT.format(
                question=question,
                expected_value=expected_value or "与研究问题相关的关键信息",
                accumulated_info=accumulated_info,
                chunk_num=chunk_num,
                total_chunks=total_chunks,
                chunk_content=chunk,
            )
            messages = [{"role": "user", "content": prompt}]

            try:
                result, response = self.llm.chat_json(messages)
                tokens_used += response.total_tokens
            except Exception as e:
                logger.warning(f"Chunk {chunk_num} read failed: {e}")
                break

            found_info = result.get("found_info", "")
            should_continue = result.get("should_continue", False)
            confidence = result.get("confidence", "low")

            if found_info:
                accumulated_extractions.append(f"[块{chunk_num}] {found_info}")
                logger.debug(
                    f"  Chunk {chunk_num}/{total_chunks}: found info "
                    f"(confidence={confidence}, continue={should_continue})"
                )

            # 判断是否退出
            if not should_continue:
                reason = result.get("reason", "LLM decided to stop")
                logger.info(
                    f"  Early exit at chunk {chunk_num}/{total_chunks}: {reason}"
                )
                return self._build_result(
                    paper_title, accumulated_extractions, question,
                    chunks_read=chunk_num, total_chunks=total_chunks,
                    early_exit=True, exit_reason=reason,
                    tokens_used=tokens_used,
                )

            if confidence == "high" and accumulated_extractions:
                logger.info(
                    f"  High confidence at chunk {chunk_num}/{total_chunks}, stopping"
                )
                return self._build_result(
                    paper_title, accumulated_extractions, question,
                    chunks_read=chunk_num, total_chunks=total_chunks,
                    early_exit=True, exit_reason="high_confidence",
                    tokens_used=tokens_used,
                )

        # 读完所有块
        return self._build_result(
            paper_title, accumulated_extractions, question,
            chunks_read=len(chunks), total_chunks=total_chunks,
            early_exit=False, exit_reason="all_chunks_read",
            tokens_used=tokens_used,
        )

    def _build_result(
        self, paper_title: str, extractions: list[str], question: str,
        chunks_read: int, total_chunks: int,
        early_exit: bool, exit_reason: str, tokens_used: int,
    ) -> ReadingResult:
        """将分块提取的信息汇总为最终结果"""
        if not extractions:
            return ReadingResult(
                paper_title=paper_title, relevant=False,
                chunks_read=chunks_read, total_chunks=total_chunks,
                early_exit=early_exit, exit_reason=exit_reason,
                tokens_used=tokens_used,
            )

        # 如果提取内容不多，直接拼接
        raw_text = "\n".join(extractions)
        if len(raw_text) <= 500:
            extracted_info = raw_text
        else:
            # 内容较多时用 LLM 做最终汇总
            extracted_info, summary_tokens = self._summarize_extractions(
                question, paper_title, raw_text
            )
            tokens_used += summary_tokens

        return ReadingResult(
            paper_title=paper_title,
            relevant=True,
            extracted_info=extracted_info,
            chunks_read=chunks_read,
            total_chunks=total_chunks,
            early_exit=early_exit,
            exit_reason=exit_reason,
            tokens_used=tokens_used,
        )

    def _summarize_extractions(self, question: str, paper_title: str, raw_text: str) -> tuple[str, int]:
        """汇总多块提取结果"""
        prompt = FINAL_SUMMARY_PROMPT.format(
            question=question,
            paper_title=paper_title,
            raw_extractions=raw_text,
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self.llm.chat(messages)
            return response.content.strip(), response.total_tokens
        except Exception as e:
            logger.warning(f"Summary failed: {e}")
            return raw_text[:500], 0

    def _split_into_chunks(self, text: str) -> list[str]:
        """
        将文本分块，尽量在段落边界切割
        """
        if not text:
            return []

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= self.chunk_size:
                chunks.append(remaining)
                break

            # 在 chunk_size 附近找段落边界
            cut_point = self.chunk_size
            # 向后找最近的段落分隔
            newline_pos = remaining.rfind("\n\n", 0, cut_point + 200)
            if newline_pos > self.chunk_size * 0.6:
                cut_point = newline_pos + 2
            else:
                # 找句号
                period_pos = remaining.rfind(". ", 0, cut_point + 100)
                if period_pos > self.chunk_size * 0.7:
                    cut_point = period_pos + 2

            chunks.append(remaining[:cut_point])
            remaining = remaining[cut_point:]

        return chunks
