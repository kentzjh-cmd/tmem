# `tmem` — 面向 DeepSeek Harness (`dsh`) 的上下文压缩切片与确定性工程记忆引擎

**中文文档** | [English](./README_EN.md)

<p align="center">
  <img src="./docs/assets/tmem-demo.gif" alt="tmem Demo: Deterministic Trace Slicing and Strong-Signal Interception" width="100%" />
</p>

`tmem`（**Trace Memory**，npm 包名 [`dsh-tmem`](https://www.npmjs.com/package/dsh-tmem)）是一个**零外部依赖、聚焦 `@deepseek-ai/dsh` (DeepSeek Harness)、仓库级（`.tmem/`）**的**上下文压缩切片 + 倒排索引渐进披露记忆与确定性工程 Trace 引擎**。

它与 DSH 宿主生命周期深度整合：
1. **压缩即切片（Compaction-Triggered Episode Slicing）**：自动监听 DSH `session/event`（`compaction/summary`、`compaction/end`、`compaction/prune`），无需额外 LLM 总结 Token，即可将每次上下文压缩折叠的会话区间与被 `toolResultPruner` 截断的超长日志中段完整归档为阶段切片（`.tmem/episodes/`，支持 30 天 TTL + 200MB LRU 自动回收），支持**多意图子切片切分**与**跨多轮 Compact 时间线串联**；高价值轨迹同步晋升为仓库级可共享 Playbook（`.tmem/traces/` + `MEMORIES.md`）。
2. **零 LLM 多维倒排索引 + L0→L3 渐进式披露**：在 `index.db` 中构建 Turn 级 8 维语义角色加权倒排表（`intent`、`error`、`decision`、`symbol`、`file`、`command`、`diff`、`raw_output`），并在压缩后向对话注入 `<tmem_compacted_index>` 路标；通过注册于 DSH 的 `tmem_search` 与 `tmem_expand` 工具，支持从 `index`（L0）→ `slice`（L1）→ `turn` / `diff`（L2）→ `raw`（L3）按需无损回放。
3. **运行时强信号即时拦截与 AST 签名守卫**：错误发生时即时注入 L2 同源修复，编辑源码时基于 Git/Trace 统计共变即时提醒 `<tmem_cochange_hint>`（支持基于遥测忽略历史自动降噪），并通过多语言结构化 AST 解析提供 `symbol_present` 与 `signature_match` 签名级守卫降级保护。

---

## 一、真实评测结果（Empirical Benchmarks）

所有脚本与数据均可在 `benchmarks/` 下复现（CI 通过 `node scripts/verify_benchmarks.mjs` 自动校验回归阈值、报告文件与文档表格一致性）：

### 1. DSH 端到端强信号回放与遥测评测
复现命令：`node benchmarks/eval_dsh_e2e.mjs`（[评测脚本](./benchmarks/eval_dsh_e2e.mjs)）：
- **端到端交互步数精简**：**37.5%**（8 步试错基线 → 5 步完成）
- **运行时遥测命中收益**：L2 报错修复转绿率 **100%**（`1/1`），L1 共变提醒采纳率 **100%**（`1/1`）
- **泛自然语言负例误注入率**：**0.0%**（10 条无代码/报错锚点的日常对话均保持静默）
- **Cordis 插件热路径耗时**：21 次高频事件（6 轮 `agent/pre-step` + 15 次只读 `tools/post-execute`）总耗时 **~5 ms**（纯 Node.js ESM 进程内直调，零子进程冷启动）

### 2. 时间因果切片与 50 轨迹 LLM 盲测评审
复现命令：`node benchmarks/eval_slicing_fidelity.mjs --gold-path benchmarks/gold/gold_annotations_50.json` 与 `node benchmarks/eval_llm_judge.mjs`（[50 轨迹盲测报告](./benchmarks/runs/llm_judge_50_report.json) / [Gold-50 标注集](./benchmarks/gold/gold_annotations_50.json)）：

在扩展至 **50 条高质量真实轨迹**（原有 25 条 SWE-bench/DSH + 从 `sailplane/swe-agent-trajs` 筛选的 25 条 `resolved=True` 多仓库修复轨迹）上，由 5 组独立 LLM 盲测评审对随机打乱的 A/B 切片结果打分：

| 切片策略 | Precision (精确率) | Recall (召回率) | F1-Score | 平均保留步数 | LLM 盲测综合分 (1–5) |
|---|:---:|:---:|:---:|:---:|:---:|
| **B0 简单基线（修改文件 + 终测）** | 81.1% | 66.5% | 71.4% | 4.7 步 | 3.20 |
| **B1 `tmem` Phase 1 时间因果切片** | **93.8%** | **95.0%** | **93.6%** | 5.1 步 | **4.80**（成对胜率 **49:1 / 98.0%**） |
| **B2 Oracle 上限（LLM Gold-50 标注）** | 98.0% | 98.0% | 98.0% | 5.0 步 | — |

> **诚实局限**：在极少数样本（如 `sympy__sympy-22005`）中，若 Agent 在最后一次通过测试之后又编辑了测试文件却未再运行验证，`tmem` 在离线无 Git 状态下仍会保留该末尾未验证修改；在原始 25 标注集（`benchmarks/gold/gold_annotations.json`，复现命令：`node benchmarks/eval_slicing_fidelity.mjs --gold-path benchmarks/gold/gold_annotations.json`）基准评测中，B1 Precision 亦已从 63.2% 提升至 92.3%（F1 81.8% > B0 81.0%）。

### 3. 50 轨迹规模压力测试与独立检索评测
复现命令：`node benchmarks/eval_plugin_scale.mjs`（[规模报告](./benchmarks/runs/plugin_scale_50_report.json)）与 `node benchmarks/eval_retrieval.mjs`（[检索日志](./benchmarks/runs/retrieval_report.json)）：
- **50 轨迹全量压缩入库**：50 条编译为 **49** 条 Playbook（自动过滤空变更 `pytest-dev__pytest-7168`），原始 **898 步 → L0 157 步**（步数精简 **82.5%**，字符体积压缩 **94.8%**），纯 Node.js 单条切片耗时在毫秒级。
- **`handleDshHook` 规模运行验证**：49 条库规模下 `pre_step` 负例误注入 **0.0%**（`0/10`）、显式锚点召回 **100.0%**（`48/48`）；`post_tool_use` L2 同源报错拦截与验证转绿率 **100.0%**（`7/7`）。
- **独立原始 Issue 检索**：24 条含原始需求文本的样本在 `eval_retrieval.mjs` 取得 **Recall@1 = 100.0% / Recall@3 = 100.0% / MRR = 1.0**，词边界与词元覆盖率门控生效后 **负例误召回率降至 0.0%**（49 条全库压力下 FTS5 负例误召回 **10.0%**，且已被 `pre_step` 显式锚点策略兜底为 **0.0%**）。

### 4. 跨格式轨迹压缩与运行时开销评测（9 条真实轨迹）
复现命令：`node tmem/cli.mjs evaluate`：
- **L0 步骤精简率**：**56.5%**（106 步 → 43 步，5,406 → 1,480 字符，~1,348 tok → ~367 tok）
- **高熵 L2 因果修复对**：**8 组**（经同源验证命令与堆栈交集双重校验）
- **平均确定性切片耗时**：**0.88 ms**（零外部依赖与零 LLM Token 耗时）

### 5. 上下文压缩后倒排渐进恢复基准（`eval_compact_recall.mjs`）
复现命令：`node benchmarks/eval_compact_recall.mjs`（[评测脚本](./benchmarks/eval_compact_recall.mjs)）：

覆盖 4 类典型“压缩/截断后遗忘”场景（早期架构约束召回、18KB 被截断工具日志中段根因定位、多意图分簇与跨轮时间线串联、精确 Diff 恢复 + 尾部未验证编辑裁剪 + AST `signature_match` 守卫）：
- **关键细节召回率**：原生 Compact 摘要 + Prune 基线 **0.0%**（`0/4`）→ `Compact + tmem` 倒排渐进披露 **100.0%**（`4/4`）
- **上下文 Token 节省率**：L0 索引路标注入 **91.6%**（`7,157 tok` → `603 tok`）；L0 + 按需 L1/L2 定向展开总节省 **87.0%**（`933 tok`）
- **平均检索 / 展开延迟**：单次倒排检索 **~6 ms**，按需层级展开 **< 10 ms**

---

## 二、工作原理与核心机制

```mermaid
flowchart LR
    DSH["DSH Cordis 插件<br/>tmem/dsh_plugin.mjs"] -->|"session/event"| Cmp["compaction/end & prune<br/>多意图切分 + 时间线串联"]
    Cmp --> Eps[".tmem/episodes/*.json<br/>TTL + LRU 统一存储"]
    Eps --> Inv["inverted_index.mjs<br/>Turn 级 8 维语义角色倒排索引"]
    DSH -->|"agent/pre-step"| Pre["pre_invocation<br/><tmem_compacted_index> + 显式锚点召回"]
    Pre -->|"按需查询"| Tools["tmem_search / tmem_expand<br/>L0 index → L1 slice → L2 turn/diff → L3 raw"]
    DSH -->|"报错 / 编辑 / 验测"| Post["post_tool_use 强信号拦截"]
    Post -->|"命令报错"| L2["lookupL2Repair<br/>即时注入同源 L2 修复"]
    Post -->|"编辑代码文件"| L1["cochange.mjs<br/>方向置信度 + 忽略自动抑制"]
    Post -->|"验证通过 / 转绿"| Tel["telemetry.jsonl + GC<br/>统计采纳率与自动淘汰"]
    Log["session.v4.jsonl + Git Diff"] --> Gate{"Default-Deny + 写前合并 + AST 签名守卫"}
    Gate -->|"常规单文件"| Env[".tmem/env_facts.json + episodes/"]
    Gate -->|"高价值轨迹"| Traces[".tmem/traces/*.json + MEMORIES.md"]
```

1. **压缩切片与倒排渐进披露引擎**（[`tmem/episodes.mjs`](./tmem/episodes.mjs)、[`tmem/inverted_index.mjs`](./tmem/inverted_index.mjs)、[`tmem/dsh_tools.mjs`](./tmem/dsh_tools.mjs)）：
   - **非侵入式 DSH 宿主挂载**：通过宿主 `session/event` 拦截 `compaction/summary`、`compaction/end`、`compaction/prune`，兼容 DSH CLI / Headless / Web Presets（即使压缩插件运行在隔离 preset 组内）；同时向 DSH 注册 `tmem_search` 与 `tmem_expand` 模型工具。
   - **多意图切分与时间线串联**：跨 `git commit` 边界或不相交文件模块簇自动拆分为高内聚子切片，并在 `.tmem/episodes/` 中维护同会话 `prev_episode_id` / `phase_index` 链条。
   - **8 维角色加权倒排检索**：纯内置 SQLite 持久化 `inverted_postings`，按 `intent`、`error`、`decision`、`symbol`、`file`、`command`、`diff`、`raw_output` 语义权重与同 Turn 局部共现打分，支持中英文混合分词及驼峰/蛇形符号拆解，无需额外 LLM 总结或改写。
2. **DSH 热路径瘦身与强信号即时拦截**（[`tmem/dsh_plugin.mjs`](./tmem/dsh_plugin.mjs)、[`tmem/hooks/dsh.mjs`](./tmem/hooks/dsh.mjs)）：
   - **进程内零冷启动**：基于纯 Node.js ESM 实现，DSH Cordis 插件进程内直调钩子逻辑；`agent/pre-step` 自动识别新落盘 `compact-checkpoint` 注入 `<tmem_compacted_index>` 路标，同输入常规推理步直接放行；`tools/post-execute` 跳过常规 `read`/`grep`/`glob`。
   - **降级泛猜测 & L2/L1 即时拦截**：报错时通过 `lookupL2Repair` 即时注入同源 L2 修复动作；修改源码文件后基于方向性 Git/Trace 共变统计（叠加 `.git/HEAD` 缓存与基于遥测的自动抑制）注入 `<tmem_cochange_hint>`。
3. **时间因果切片（Phase 2）、AST 签名守卫与漂移淘汰**（[`tmem/slicer.mjs`](./tmem/slicer.mjs)、[`tmem/guards.mjs`](./tmem/guards.mjs)、[`tmem/store.mjs`](./tmem/store.mjs)）：
   - **时间因果图 + 尾部裁剪**：剔除语法拒改重试、`rm`/`git checkout|restore|reset` 销毁屏障前失效操作、临时复现脚本及末次通过 `VERIFY_GATE` 后的未验证试探性编辑（后者保留于 Episode `raw` 层取证）。
   - **多语言结构化 AST 解析与 `signature_match`**：[`tmem/ast_parser.mjs`](./tmem/ast_parser.mjs) 提取文件符号与参数签名指纹，一旦函数签名重构，自动将 Playbook 从 `L0_FULL` 降级为 `L1_TOPOLOGY` / `L2_INVARIANT`。
   - **写前合并与双轨 GC**：`.tmem/traces/` 通过 `tmem gc --prune` 淘汰长期漂移条目；`.tmem/episodes/` 默认按 30 天 TTL 与 200MB LRU 自动回收，并统计 L2 转绿率与 L1 采纳率遥测。

---

## 三、快速开始（Node.js 版本）

> 运行环境要求：**Node.js >= 22.5.0**（使用内置 `node:sqlite` FTS5，零外部 npm 依赖，无需 Python；原始 Python 版本归档于 `python` 分支）。

### 安装与初始化
```bash
npm install -g dsh-tmem                       # 或 git clone 后执行 npm link
tmem init                                     # 初始化 .tmem/、安装 DSH Cordis 插件与 git post-commit hook
dsh plugin add npm:dsh-tmem                   # 或通过 dsh plugin add github:kentzjh-cmd/tmem 安装
npm test                                      # 运行 node --test 完整测试套件（50/50）
npm run verify:benchmarks                     # 运行基准回归门禁、报告校验与 README 同步校验
```

### 常用 CLI 与 DSH Slash / 工具
```bash
tmem status                                      # 查看 Playbook、Episode 与实时遥测统计
tmem telemetry                                   # 查看 L2 纠偏率与 L1 共变采纳率明细
tmem capture --agent dsh                         # 同步归档 Episode 至 .tmem/episodes/ 并编译高价值 Playbook
tmem search "jwt_guard.ts leewaySec"             # 多维倒排索引检索（支持 --files / --symbols / --errors / --roles）
tmem episodes --session <id>                     # 列出已归档的压缩阶段切片与时间线
tmem expand <episode_id> --level slice           # 渐进展开：index (L0) -> slice (L1) -> turn / diff (L2) -> raw (L3)
tmem lookup "TypeError" --file src/validator.py  # 本地 Playbook 检索 + 探针校验 + 分级预览
tmem inspect <trace_id> --level playbook         # 查看三层 Playbook 详情
tmem sync                                        # 校验探针并刷新 MEMORIES.md
tmem gc --prune                                  # 校验 Guard 漂移，并按 --max-episodes-mb / --max-episode-days 清理缓存
```

在 DSH 会话内，模型可直接调用已注册工具 **`tmem_search`** 与 **`tmem_expand`**，用户也可使用斜杠命令 **`/tmem [status|search|episodes|expand|list|inspect|replay|diff|sync|gc|forget|telemetry|evaluate]`**。

---

## 四、设计边界与局限

1. **只聚焦 DSH 单一运行时**：主动裁剪了多宿主并行维护成本，全部运行时能力围绕 `@deepseek-ai/dsh` `session.v4.jsonl`、宿主 `session/event` 与 Cordis 生命周期打磨（保留 `swe_bench` 适配器仅用于离线评测）。
2. **启发式切片非形式化推导**：基于 Git 落盘、纯 Node 多语言结构化 AST 解析（[`tmem/ast_parser.mjs`](./tmem/ast_parser.mjs)，支持 Python / JS / TS / Go / Rust 等）和退出码反推，不构建完整跨文件调用图与数据流分析。
3. **探针聚焦存在性与签名指纹**：已通过 `signature_match` 检查函数参数列表稳定性；若函数签名未变而内部逻辑重构，仍需依赖终端验证命令确认结果。

---

## License

MIT
