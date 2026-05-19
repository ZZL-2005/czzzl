# Arena Agent 系统设计文档

## 1. 系统总体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Main Process (未来)                              │
│  负责调度多个 Task Worker，管理并发、优先级排序等                              │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │ 启动
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Task Worker (单任务处理进程)                            │
│                                                                         │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │ 获取题目  │───▶│  Plan Agent  │───▶│ Web Search   │───▶│  Expert   │ │
│  │          │    │ (分析+标签)   │    │  (多路并发)    │    │  Agent    │ │
│  └──────────┘    └──────────────┘    └──────────────┘    └─────┬─────┘ │
│                                                                │       │
│       ┌────────────────────────────────────────────────────────┘       │
│       ▼                                                                │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐ │
│  │  提交答案  │───▶│ 轮询评分结果  │───▶│ 反馈分析      │───▶│ 回复评审  │ │
│  │          │    │ (10s间隔)     │    │ (需要新信息?) │    │          │ │
│  └──────────┘    └──────────────┘    └──────────────┘    └──────────┘ │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## 2. 模块设计

### 2.1 Config Module (`config/`)

所有配置提取到 YAML 文件，方便修改：

```
config/
├── settings.yaml          # 全局设置（重试策略、超时、日志级别等）
├── llm.yaml               # LLM API 配置（Plan Agent、Expert Agent）
├── search.yaml            # Web Search API 配置 + 各领域指定网站
├── experts.yaml           # 五大领域 Expert Agent 的 system prompt + 注意事项
└── categories.yaml        # 题目标签 → 领域的映射规则
```

### 2.2 Plan Agent

**职责：**
1. 接收题目原文（title + content）
2. 分析题目所需的额外信息（输出多个搜索 query）
3. 为题目打标签（映射到五大领域之一）

**接口：** OpenAI SDK 格式（chat/completions），支持 extended thinking / reasoning

**输入：** 题目标题 + 正文
**输出结构化 JSON：**
```json
{
  "category": "medical_health",
  "reasoning": "该题涉及肝胆胰外科手术方案...",
  "search_queries": [
    {"query": "胰十二指肠切除术 Whipple手术 并发症处理", "priority": 1},
    {"query": "胆管癌 TNM分期 2024指南更新", "priority": 2}
  ],
  "key_points": ["需要最新手术指南", "需要循证医学证据"]
}
```

### 2.3 Web Search Module

**架构：** 独立 REST API 调用，根据 Plan Agent 输出的 queries 并发搜索

**流程：**
1. 从 Plan Agent 获取 N 个 search_queries
2. 根据 category 确定指定搜索网站列表
3. 并发调用搜索 API（每个 query 一个请求）
4. 收集、去重、整理搜索结果

**API 接口设计（配置中定义）：**
```yaml
# search.yaml
search_api:
  base_url: "https://your-search-api.com"
  endpoint: "/v1/search"
  api_key: "YOUR_KEY"
  method: "POST"
  request_format:
    query: "{query}"
    domains: "{domains_list}"   # 限定搜索域名
    max_results: 5
  response_format:
    results_path: "results"     # JSON path to results array
    title_field: "title"
    snippet_field: "snippet"
    url_field: "url"
    content_field: "content"    # 如果有全文
```

**各领域指定网站：**
```yaml
domain_restrictions:
  natural_science:
    - scholar.google.com
    - arxiv.org
    - nature.com
    - science.org
    - aps.org
    - acs.org
    - pubmed.ncbi.nlm.nih.gov
    - nasa.gov
    - nist.gov
  law:
    - hkex.com.hk
    - sfc.hk
    - sec.gov
    - justice.gov
    - legislation.gov.uk
    - law.cornell.edu
    - npc.gov.cn
    - court.gov.cn
    - pkulaw.com
  finance:
    - sec.gov
    - irs.gov
    - federalreserve.gov
    - treasury.gov
    - imf.org
    - worldbank.org
    - oecd.org
    - bis.org
  industrial_engineering:
    - siemens.com
    - rockwellautomation.com
    - profinet.com
    - iso.org
    - iec.ch
    - ieee.org
    - nist.gov
  medical_health:
    - pubmed.ncbi.nlm.nih.gov
    - who.int
    - cdc.gov
    - nih.gov
    - fda.gov
    - nice.org.uk
    - cochranelibrary.com
    - clinicaltrials.gov
```

### 2.4 Expert Agents（五大领域）

每个 Expert Agent 本质上是同一套代码，区别在于：
- **system_prompt**: 领域专用 prompt
- **注意事项 (guidelines)**: 领域专用的回答规范
- 模型参数可以不同（比如某些领域用更强的模型）

**接口：** OpenAI SDK 格式（chat/completions），支持 reasoning

**配置示例 (`experts.yaml`)：**
```yaml
experts:
  natural_science:
    name: "自然科学专家"
    model: "deepseek-r1"           # 用户自填
    base_url: "https://xxx"        # 用户自填
    api_key: "YOUR_KEY"
    temperature: 0.1
    max_tokens: 8192
    system_prompt: |
      你是一位自然科学领域的专家，擅长化学、生物、物理、数学、通信等学科。
      回答时需要：
      1. 提供严谨的学术论证
      2. 引用具体的理论、公式、实验数据
      3. 区分已证实的事实和假说
      ...
    guidelines: |
      - 数学公式使用 LaTeX 格式
      - 化学方程式需要配平
      - 生物学名使用拉丁文斜体
      - 引用文献时标注年份
      ...

  law:
    name: "法律事务专家"
    # ...

  finance:
    name: "金融分析专家"
    # ...

  industrial_engineering:
    name: "工业工程专家"
    # ...

  medical_health:
    name: "医疗健康专家"
    # ...
```

## 3. 单任务处理流程（Task Worker 详细流程）

```
输入：task_id

Step 1: 获取题目
  ├── studio-arena task show <task_id>
  └── 解析 title（提取分类标签）+ agora_post.content（题目正文）

Step 2: Plan Agent 分析
  ├── 输入：题目标题 + 正文
  ├── 输出：category + search_queries + key_points
  └── 记录：输入tokens, 输出tokens, reasoning tokens, 耗时

Step 3: Web Search（并发）
  ├── 根据 category 获取 domain_restrictions
  ├── 并发执行所有 search_queries
  ├── 整理搜索结果为结构化文本
  └── 记录：每个 query 的输入输出

Step 4: Expert Agent 生成答案
  ├── 输入：题目 + 搜索到的额外信息
  ├── Expert 根据 category 选择对应的 system_prompt
  ├── 输出：完整答案文本
  └── 记录：输入tokens, 输出tokens, reasoning tokens, 耗时

Step 5: 提交答案
  ├── studio-arena submit <task_id> <answer_text>
  └── 记录提交时间

Step 6: 轮询评分结果
  ├── 每 10s 调用 studio-arena my-answer <task_id>
  ├── 检查 scored_at 是否不为 null
  ├── 超时阈值：5分钟（可配置）
  └── 获取 score + score_detail.reply_text

Step 7: 反馈分析 + 回复
  ├── 将 reply_text（评审反馈）追加到 Expert Agent 上下文
  ├── Expert Agent 判断：是否需要新信息？
  │   ├── 需要 → 启动新一轮 Web Search → 获取信息 → 生成回复
  │   └── 不需要 → 直接根据反馈生成改进回复
  ├── 通过 agora comment create 提交回复
  │   └── studio-arena agora comment create <post_id> <reply_text> --parent-type answer --parent-id <agora_answer_id>
  └── 记录：Expert Agent 的 tokens 消耗

Step 8: （可选）再次轮询是否有新评分
  └── 根据配置决定是否进行多轮反馈
```

## 4. 反馈循环详细设计

```
                    ┌─────────────────┐
                    │  提交初始答案    │
                    └────────┬────────┘
                             ▼
                    ┌─────────────────┐
                    │  轮询评分 (10s)  │◀─── 超时 5min 后终止
                    └────────┬────────┘
                             ▼
                    ┌─────────────────┐
              ┌─────│  收到评审反馈    │
              │     └────────┬────────┘
              │              ▼
              │     ┌─────────────────┐
              │     │ Expert 分析反馈  │
              │     │ 是否需要新信息？  │
              │     └───┬─────────┬───┘
              │     需要│         │不需要
              │         ▼         ▼
              │  ┌──────────┐ ┌──────────┐
              │  │Web Search│ │直接生成   │
              │  │获取新信息 │ │改进回复   │
              │  └────┬─────┘ └────┬─────┘
              │       ▼            │
              │  ┌──────────┐      │
              │  │生成带新   │      │
              │  │信息的回复 │      │
              │  └────┬─────┘      │
              │       └──────┬─────┘
              │              ▼
              │     ┌─────────────────┐
              │     │ agora comment   │
              │     │ create 提交回复  │
              │     └────────┬────────┘
              │              ▼
              │     ┌─────────────────┐
              │     │ 本轮结束        │
              │     │ (可配置多轮次数) │
              │     └─────────────────┘
              │
              └── (达到最大轮次) → 结束
```

**关于多轮上下文：**
- Expert Agent 的 messages 数组会累积：初始问题 → 初始回答 → 评审反馈 → 改进回复
- 这样做的好处是 Expert 有完整上下文
- **注意**：不能"重新提问"，因为新的 chat/completions 调用不会命中之前的 cache，所以保持同一个 messages 数组追加是最优策略

## 5. 异常处理设计

### 5.1 重试策略

```yaml
# settings.yaml
retry:
  max_retries: 3
  backoff_strategy: "exponential"   # exponential / linear / fixed
  initial_delay_seconds: 2
  max_delay_seconds: 60
  retry_on_status_codes: [429, 500, 502, 503, 504]
  retry_on_exceptions:
    - "ConnectionError"
    - "Timeout"
    - "RateLimitError"
```

### 5.2 异常分类与处理

| 异常类型 | 处理方式 |
|---------|---------|
| API 429 (Rate Limit) | 指数退避重试，记录到日志 |
| API 500/502/503 | 重试，3次失败后标记任务为 failed |
| 网络超时 | 重试，增大超时时间 |
| 搜索 API 无结果 | 跳过该 query，继续其他查询 |
| CLI 命令失败 | 解析 stderr，重试或标记 |
| 题目内容解析失败 | 标记为需要人工检查 |
| Expert 输出格式异常 | 尝试重新生成，最多2次 |
| 评分轮询超时(5min) | 记录警告，继续下一步或结束 |

### 5.3 全局保护

- **总 token 预算**：可配置单题最大 token 消耗，超出则终止
- **钱包余额监控**：每次操作前检查余额是否充足
- **并发限制**：信号量控制同时进行的 API 调用数

## 6. 日志系统设计

### 6.1 日志级别

- `DEBUG`: 每次 API 请求/响应的完整内容
- `INFO`: 关键节点（开始处理、完成搜索、提交答案、收到评分）
- `WARNING`: 重试事件、超时、异常恢复
- `ERROR`: 处理失败、不可恢复错误

### 6.2 结构化日志（per-task）

每道题生成一个独立的 JSON 日志文件：

```
logs/
├── task_72fa533d_建筑设计_11913.json
├── task_xxxxx_股票_xxxxx.json
└── ...
```

**单题日志结构：**
```json
{
  "task_id": "72fa533d-d9d1-457a-a016-f4cbea1926fd",
  "title": "[建筑设计] 任务 #11913",
  "category": "industrial_engineering",
  "reward_pool": 3181,
  "started_at": "2026-05-18T21:00:00Z",
  "completed_at": "2026-05-18T21:05:30Z",

  "plan_agent": {
    "input_tokens": 1200,
    "output_tokens": 450,
    "reasoning_tokens": 800,
    "total_tokens": 2450,
    "latency_ms": 3200,
    "input_text": "...(题目摘要)...",
    "output_json": { "category": "...", "search_queries": [...] },
    "reasoning_text": "...(如果API返回)"
  },

  "web_search": [
    {
      "query": "...",
      "domains_used": ["ieee.org", "iso.org"],
      "results_count": 5,
      "latency_ms": 1200,
      "results_summary": "..."
    }
  ],

  "expert_agent": {
    "round_1": {
      "input_tokens": 3500,
      "output_tokens": 2000,
      "reasoning_tokens": 1500,
      "total_tokens": 7000,
      "latency_ms": 8000,
      "answer_preview": "...(前200字)..."
    },
    "round_2_feedback": {
      "feedback_received": "...(评审反馈)...",
      "needs_new_search": true,
      "additional_search": [...],
      "input_tokens": 5000,
      "output_tokens": 1800,
      "reasoning_tokens": 1200,
      "total_tokens": 8000,
      "latency_ms": 7500,
      "reply_preview": "...(前200字)..."
    }
  },

  "submission": {
    "submitted_at": "2026-05-18T21:03:00Z",
    "answer_length": 2500
  },

  "scoring": {
    "score": 22.0,
    "max_score": 22,
    "min_score": -22,
    "final_score": 22,
    "scoring_mode": "absolute",
    "review_text": "...(评审反馈全文)...",
    "scored_at": "2026-05-18T21:04:30Z",
    "poll_attempts": 9
  },

  "reply": {
    "replied_at": "2026-05-18T21:05:20Z",
    "reply_length": 1500
  },

  "token_summary": {
    "plan_agent_total": 2450,
    "search_calls": 3,
    "expert_agent_total": 15000,
    "grand_total_tokens": 17450
  },

  "cost_summary": {
    "reward_pool": 3181,
    "score_earned": 22.0,
    "money_earned": "计算方式待定"
  },

  "exceptions": [
    {
      "timestamp": "2026-05-18T21:01:15Z",
      "type": "RateLimitError",
      "message": "429 Too Many Requests",
      "action": "retry",
      "retry_count": 1,
      "resolved": true
    }
  ]
}
```

### 6.3 汇总日志

```
logs/
├── summary.json             # 全局汇总：总分、总token、各领域统计
├── leaderboard_snapshots/   # 排行榜快照（定期采集）
│   ├── 2026-05-18T21:00.json
│   └── ...
└── bounty_market/           # 外包市场快照
    └── ...
```

**summary.json：**
```json
{
  "last_updated": "2026-05-18T22:00:00Z",
  "total_tasks_processed": 45,
  "total_tasks_succeeded": 42,
  "total_tasks_failed": 3,
  "total_score": 850.0,
  "total_tokens_used": 500000,
  "wallet_balance": 19400,
  "by_category": {
    "natural_science": { "count": 12, "score": 200, "tokens": 120000 },
    "law": { "count": 8, "score": 180, "tokens": 100000 },
    "finance": { "count": 10, "score": 220, "tokens": 130000 },
    "industrial_engineering": { "count": 8, "score": 150, "tokens": 80000 },
    "medical_health": { "count": 7, "score": 100, "tokens": 70000 }
  },
  "leaderboard_position": 5,
  "exception_stats": {
    "rate_limit_hits": 12,
    "timeouts": 3,
    "total_retries": 18
  }
}
```

## 7. 项目文件结构

```
czzzl/
├── config/
│   ├── settings.yaml          # 全局配置
│   ├── llm.yaml               # LLM API 配置
│   ├── search.yaml            # 搜索 API + 域名限制
│   ├── experts.yaml           # Expert Agent prompts
│   └── categories.yaml        # 分类映射
│
├── src/
│   ├── __init__.py
│   ├── config_loader.py       # 配置加载器
│   ├── llm_client.py          # OpenAI SDK 格式的 LLM 客户端封装
│   ├── search_client.py       # Web Search REST API 客户端
│   ├── arena_client.py        # studio-arena CLI 封装
│   ├── plan_agent.py          # Plan Agent 逻辑
│   ├── expert_agent.py        # Expert Agent 逻辑
│   ├── task_worker.py         # 单任务处理主流程
│   ├── feedback_loop.py       # 反馈循环逻辑
│   ├── logger.py              # 结构化日志系统
│   └── exceptions.py          # 自定义异常 + 重试装饰器
│
├── logs/                      # 运行时生成的日志目录
├── main.py                    # 入口：处理单个/多个任务
├── monitor.py                 # 排行榜 + 外包市场监控
└── requirements.txt
```

## 8. 关键接口定义

### 8.1 LLM Client（统一封装）

```python
class LLMClient:
    """OpenAI SDK 格式的 LLM 调用封装，支持 reasoning"""

    def __init__(self, config: dict):
        # config 包含: base_url, api_key, model, temperature, max_tokens 等
        pass

    def chat(self, messages: list, **kwargs) -> LLMResponse:
        """
        调用 chat/completions
        返回 LLMResponse 包含:
          - content: str (回答文本)
          - reasoning: str (推理过程，如果有)
          - usage: {input_tokens, output_tokens, reasoning_tokens, total_tokens}
          - latency_ms: int
        """
        pass

    def chat_json(self, messages: list, schema: dict, **kwargs) -> dict:
        """强制 JSON 格式输出（用于 Plan Agent）"""
        pass
```

### 8.2 Search Client

```python
class SearchClient:
    """独立 REST 搜索 API 客户端"""

    def __init__(self, config: dict):
        # config: base_url, endpoint, api_key, request/response format
        pass

    def search(self, query: str, domains: list[str], max_results: int = 5) -> list[SearchResult]:
        """
        执行搜索
        返回 SearchResult 列表，每个包含:
          - title, url, snippet, content(可选)
        """
        pass

    async def batch_search(self, queries: list[str], domains: list[str]) -> list[list[SearchResult]]:
        """并发批量搜索"""
        pass
```

### 8.3 Arena Client

```python
class ArenaClient:
    """studio-arena CLI 封装"""

    def get_tasks(self) -> list[dict]: ...
    def get_task_detail(self, task_id: str) -> dict: ...
    def submit_answer(self, task_id: str, text: str) -> dict: ...
    def get_my_answer(self, task_id: str) -> dict | None: ...
    def poll_score(self, task_id: str, interval: int = 10, timeout: int = 300) -> dict: ...
    def reply_to_review(self, post_id: str, agora_answer_id: str, content: str) -> dict: ...
    def get_leaderboard(self) -> list[dict]: ...
    def get_bounty_list(self) -> list[dict]: ...
    def get_me(self) -> dict: ...
```

## 9. 排行榜 & 外包市场监控

**功能：**
- 定期采集排行榜快照（可配置间隔）
- 采集外包市场（bounty list）状态
- 记录到 `logs/leaderboard_snapshots/` 和 `logs/bounty_market/`

**用途：**
- 分析竞争对手得分趋势
- 发现高价值外包任务
- 评估自身排名变化

## 10. 配置文件模板

### `config/settings.yaml`
```yaml
# 全局设置
retry:
  max_retries: 3
  backoff_strategy: "exponential"
  initial_delay_seconds: 2
  max_delay_seconds: 60
  retry_on_status_codes: [429, 500, 502, 503, 504]

polling:
  score_check_interval_seconds: 10
  score_check_timeout_seconds: 300

limits:
  max_token_per_task: 50000        # 单题最大 token 消耗
  max_search_queries_per_task: 5   # 单题最多搜索次数
  max_feedback_rounds: 2           # 最多反馈轮次

logging:
  level: "INFO"
  log_dir: "logs"
  save_full_responses: true        # 是否保存完整 API 响应

monitor:
  leaderboard_interval_seconds: 300  # 排行榜采集间隔
  bounty_check_interval_seconds: 60  # 外包市场检查间隔
```

### `config/llm.yaml`
```yaml
# Plan Agent 配置
plan_agent:
  base_url: "https://your-api.com/v1"
  api_key: "YOUR_API_KEY"
  model: "your-model-name"
  temperature: 0.3
  max_tokens: 2048
  timeout_seconds: 60
  # 如果支持 reasoning/extended thinking
  reasoning:
    enabled: true
    # 不同 API 的 reasoning 参数不同，这里预留
    extra_params: {}

# Expert Agent 默认配置（可被 experts.yaml 中各领域覆盖）
expert_agent_default:
  base_url: "https://your-api.com/v1"
  api_key: "YOUR_API_KEY"
  model: "your-model-name"
  temperature: 0.1
  max_tokens: 8192
  timeout_seconds: 120
  reasoning:
    enabled: true
    extra_params: {}
```

### `config/search.yaml`
```yaml
search_api:
  base_url: "https://your-search-api.com"
  endpoint: "/v1/search"
  api_key: "YOUR_SEARCH_KEY"
  method: "POST"
  timeout_seconds: 30
  max_results_per_query: 5

  # 请求体模板
  request_template:
    query: "{query}"
    domains: "{domains}"
    num_results: "{max_results}"

  # 响应解析
  response_parsing:
    results_path: "results"
    title_field: "title"
    snippet_field: "snippet"
    url_field: "url"
    content_field: "content"

# 各领域域名限制
domain_restrictions:
  natural_science:
    - scholar.google.com
    - arxiv.org
    - nature.com
    - science.org
    - aps.org
    - acs.org
    - pubmed.ncbi.nlm.nih.gov
    - nasa.gov
    - nist.gov

  law:
    - hkex.com.hk
    - sfc.hk
    - sec.gov
    - justice.gov
    - legislation.gov.uk
    - law.cornell.edu
    - npc.gov.cn
    - court.gov.cn
    - pkulaw.com

  finance:
    - sec.gov
    - irs.gov
    - federalreserve.gov
    - treasury.gov
    - imf.org
    - worldbank.org
    - oecd.org
    - bis.org

  industrial_engineering:
    - siemens.com
    - rockwellautomation.com
    - profinet.com
    - iso.org
    - iec.ch
    - ieee.org
    - nist.gov

  medical_health:
    - pubmed.ncbi.nlm.nih.gov
    - who.int
    - cdc.gov
    - nih.gov
    - fda.gov
    - nice.org.uk
    - cochranelibrary.com
    - clinicaltrials.gov
```

## 11. 待确认/待讨论事项

1. **回复评审后是否会重新评分？** 如果只是追加回复不重新评分，那反馈循环的意义主要在于补充信息影响最终人工评审？

2. **外包任务(bounty)** 是否也要自动回答？还是只做监控？

3. **多轮反馈的轮次限制**：建议默认最多 2 轮，避免 token 消耗过大。

4. **token 预算管理**：比赛系统是否对 token 使用有计费？从 API 看 `token_used` 字段目前为 0，但需要确认。

5. **评审反馈的判断逻辑**：Expert Agent 判断"是否需要新信息"时，如果评审指出"信息不准确"，应该搜索还是直接修正？建议：事实性错误→搜索验证，逻辑/表述问题→直接改进。
