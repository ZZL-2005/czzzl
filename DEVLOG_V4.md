# V4 开发日志：学术检索增强

## 版本概述
在 v2（增量 refine + 中断恢复）基础上，新增学术论文检索功能（RAG），将检索到的论文摘要/TLDR 作为上下文注入 Expert Agent，提升回答的专业性和引用质量。

## 架构设计

```
题目 → PlanAgent(分类) → KnowledgeRetrieval → ExpertAgent(带论文上下文)
                              │
                  ┌───────────┼────────────┐
                  ▼           ▼            ▼
            PaperCache   AcademicRetriever  LLM(关键词提取+筛选)
            (全局共享)    (多数据源)
```

### 新增模块
1. `src/retriever.py` — 多数据源学术检索器（Semantic Scholar、arXiv、OpenAlex、PubMed）
2. `src/paper_cache.py` — 全局论文缓存池，支持跨任务知识复用
3. `src/knowledge_retrieval.py` — 检索编排器（关键词提取→搜索→筛选→注入）

### 修改模块
- `src/task_worker.py` — 在 Expert 生成答案前插入检索步骤
- `src/expert_agent.py` — `generate_answer()` 和 `_build_initial_prompt()` 新增 `reference_context` 参数
- `src/logger.py` — 新增 `log_retrieval()` 方法和 `retrieval_total` token 统计
- `src/__init__.py` — 导出新模块
- `main.py` — 创建共享 `PaperCache` 实例传递给各 worker
- `config/settings.yaml` — 新增 `retrieval` 配置段
- `requirements.txt` — 添加 `tqdm`

## 遇到的问题与解决方案

### 问题 1: 论文全文太长，不适合直接加入上下文
**表现**: 论文 PDF 10000+ token，纯 LLM 模型无法处理图片/表格  
**解决**: 采用两阶段方案：
- 默认只用 摘要(abstract) + TLDR，约 200-400 词/篇
- 可选精读模式：对 arXiv 论文下载 LaTeX 源码 → 正则清洗为纯文本 → LLM 定向提取关键信息（限 300 字）
- 通过 `enable_deep_read: false` 配置关闭精读以节省 token

### 问题 2: arXiv API 在国内网络环境下不稳定
**表现**: 请求 `export.arxiv.org` 超时（>30s 无响应）  
**解决**:
- 为 arXiv 请求单独设置 15s 短超时
- 在 `_search_arxiv()` 中 catch 所有异常，超时后 graceful 降级
- 数据源列表中保留 arXiv 但实际会 fallback 到 OpenAlex/PubMed
- OpenAlex 也索引了大量 arXiv 论文，可作为替代

### 问题 3: Semantic Scholar 速率限制严格 (100 req/5min)
**表现**: 首次请求就返回 429  
**解决**:
- 429 时 wait 5s 后重试一次
- 设 15s 超时避免阻塞
- 多数据源降级：Semantic Scholar 失败后自动尝试 OpenAlex

### 问题 4: OpenAlex 部分论文无 abstract
**表现**: `abstract_inverted_index` 字段返回 null，导致过滤后 0 结果  
**解决**:
- 不再强制要求有 abstract 才保留
- 无 abstract 时使用 placeholder `[No abstract available for: {title}]`
- 在 LLM 筛选阶段根据 title 判断相关性

### 问题 5: 跨任务论文重复检索浪费 token 和 API 额度
**表现**: 同一 category 的多个题目可能需要同一批论文  
**解决**:
- 设计 `PaperCache` 全局缓存池（JSON on disk）
- 倒排索引支持关键词快速匹配已缓存论文
- 精读提取结果(LLM 输出)也缓存，跨任务直接复用
- `cmd_process_all()` 创建单一 `PaperCache` 实例，所有 worker 共享
- 线程安全（`threading.Lock`）

### 问题 6: 检索 token 开销控制
**表现**: 每个 task 如果做全流程检索（关键词提取 + 摘要筛选 + 精读×3），约消耗 30000 token  
**解决**:
- 默认关闭精读 (`enable_deep_read: false`)，只用摘要模式，额外开销 ~6000 token
- `max_context_chars: 4000` 限制注入上下文长度
- `max_papers_to_use: 5` 限制使用论文数量
- 配置项全部可调，可按实际 token 预算灵活开关

### 问题 7: LaTeX 源码清洗质量
**表现**: arXiv .tex 文件包含大量宏、公式、表格，直接去标签后可读性差  
**解决**:
- 分层清洗：先去注释 → 去图表环境 → 去公式(替换为[公式]) → 提取 section 标题 → 去 LaTeX 命令
- 保留关键文字内容（textbf/textit 内容保留）
- 截断 25000 字符上限
- 支持 tar.gz 和单个 gzip 压缩的 .tex 两种格式

## 配置说明

```yaml
retrieval:
  enabled: true              # 总开关
  max_results_per_source: 10 # 每个数据源最多返回数
  max_papers_to_use: 5       # 最终使用论文数
  enable_deep_read: false    # 是否开启全文精读（耗 token）
  max_context_chars: 4000    # 注入上下文最大字符数
  timeout_seconds: 30        # HTTP 请求超时
```

## 各数据源能力矩阵

| 数据源 | 免费API | 摘要 | TLDR | 全文 | 速率限制 | 国内可用性 |
|--------|---------|------|------|------|---------|-----------|
| OpenAlex | ✅ | 部分 | ❌ | ❌ | 宽松 | ✅ 稳定 |
| PubMed | ✅ | ✅ | ❌ | PMC有 | 宽松 | ✅ 稳定 |
| Semantic Scholar | ✅ | ✅ | ✅ | ❌ | 严格(100/5min) | ⚠️ 偶有429 |
| arXiv | ✅ | ✅ | ❌ | LaTeX源码 | 中等 | ❌ 超时 |

## Token 预算分析

| 模式 | 额外开销/task | 说明 |
|------|-------------|------|
| 仅摘要(默认) | ~6000 token | 关键词提取(500) + 摘要筛选(5000) + 少量overhead |
| 摘要+精读 | ~30000 token | 上述 + 全文精读(8000×3) |
| 缓存命中 | ~500 token | 仅关键词提取（如果cache全部命中，跳过API和筛选） |

## 后续优化方向
1. 添加 embedding 模型做本地语义匹配（替代 LLM 筛选，省 token）
2. 增加 Google Scholar 爬虫（需处理反爬）
3. 法律领域增加中国裁判文书网、北大法宝数据源
4. 金融领域接入 SEC EDGAR、Wind 数据
5. 论文缓存增加 TTL 过期机制
