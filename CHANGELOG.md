# 版本记录

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
