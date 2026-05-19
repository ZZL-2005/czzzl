# 版本记录

## v3 — `1eb8c86` — Plan Agent DAG分解 + 跨Expert并行执行 + 策略自进化

**核心升级：**
- Plan Agent 从简单分类器升级为**任务分解模型**，输出 DAG 结构（子问题 + 依赖关系 + synthesis 指令）
- DAG Executor 引擎：拓扑排序执行，无依赖节点并行，有依赖串行（前驱结果注入 context）
- 跨 Expert 分配：不同子问题可分配给不同领域专家（如 law + finance 协作）
- 最终 synthesis 步骤：由 LLM 融合所有子问题结果为统一答案
- 简单题退化：Plan Agent 判断不需分解时输出单节点 DAG，等价于 v2 流程

**策略自进化系统：**
- Strategy Learner：每题完成后从 feedback 提取分解教训（LLM 提炼，≤100字）
- 策略索引 `strategies/index.json`：按 decomposition_mode 聚合，记录平均分+教训
- Plan Agent 启动时加载索引，注入 system prompt，影响后续分解决策
- 闭环：执行 → 反馈 → 学习 → 下轮改进分解策略

**其他改动：**
- 全量并发模式（`--all`）：所有任务一次性提交并行
- 测试模式（`--test N`）：只处理 N 个任务，快速验证流水线
- `decomposition_report.json`：记录每题 DAG 图、节点 Expert 分配、得分、category
- Expert Agent 加载 `logv2/refined/` 已优化的 system prompt（`refined_prompt_dir` 配置）
- `resume_task` 也返回 plan_result，支持分解信息记录

**分解模式示例：**
- `single_direct`：单一问题直接回答
- `multi_step_reasoning`：多步推理，后续依赖前置结论
- `cross_domain_synthesis`：跨领域协作（如 `A(law) ; B(law) ; C(law) ; A,B,C->D(fin)`）
- `data_extraction_then_analysis`：先提取数据，再分析推理
- `multi_perspective_comparison`：多视角对比分析

**运行命令：**
```bash
python main.py --all                # 全量并发
python main.py --test 2             # 测试模式（2题）
```

**测试结果（2题样本）：**
- 任务 #262（cross_domain_synthesis）：得分 50
- 任务 #5001（multi_step_reasoning）：得分 20

---

## v2 — `238e1bb` — 并发执行 + token优化 + batch refine

**改动要点：**
- 10并发处理（ThreadPoolExecutor），支持 `--concurrency` 参数
- 反馈回复改为针对性补充（不再重写完整答案），回复时拼接原始答案+补充
- refine prompt 限制500字以内，防止膨胀
- 批次结束后统一 refine（收集所有反馈，per-category 做一次综合 refine）
- SummaryLogger 线程安全（单例+锁）
- 统一 `--output-dir` 控制所有存储路径（logs、refined、snapshots）
- 终端只显示进度条，日志只写文件
- 记录完整 task_detail JSON 到 task 日志
- feedback_loop 修复为 1次回复 + 批次结束后1次refine

**运行命令：**
```bash
python main.py --all --output-dir logv2 --concurrency 7
```

**预估效果：**
- 单 task token 从 ~10000-30000 降到 ~5000-15000
- 7并发，总耗时从 3.5h 降到 ~30-40min

---

## v1 — `d45ef9c` — prompt refining 基础版

**改动要点：**
- 基础流水线：Plan Agent 分类 → Expert Agent 答题 → 提交 → 轮询评分 → 反馈回复 → refine prompt
- 顺序执行，无并发
- 每个 task 内即时 refine（feedback_loop 内执行）
- max_feedback_rounds=2，导致对同一反馈回复两次
- Expert Agent max_tokens=8192

**运行命令：**
```bash
python main.py --all --output-dir logv1
```

---

## Initial — `545c122` — 初始提交

项目骨架搭建。
