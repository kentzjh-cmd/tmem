# 独立端到端验证场景设计方案（与 tmem 仓库无关）

> 状态：**仅方案，未落地**。本轮未创建任何被测目录、未发起任何模型调用、未修改 `/archive/tmem` 的任何代码。
> 目标：为「真实效果验证」提供一个**与本仓库无关**的测试场景，用它检验 tmem 的记忆闭环在外部项目上是否成立。
> 关联文档：`docs/dsh-validation-report.md`（既有战役结论）、`docs/dsh-validation-plan.md`（既有执行计划）、`docs/evidence/dsh-0.4.1-multiframe-check.mjs`（P0-4 证据脚本）。

---

## 0. 一句话方案

在 `/home/kent/e2e/ledger-svc` 造一个与 tmem 毫无关系的合成真实项目（Python 发票台账服务，含真实 bug、跨文件共变、真实 git 历史），用手写 `oracle/ground_truth.yml` 固定真值，用**真实 `dsh headless` 会话**（复用 `~/.dsh` 现有凭据）跑 8 场多会话任务，逐场 `tmem capture`，最后用 9 条硬门槛自动判定「记忆是否真的被生产出来、被模型消费掉、并在下一场会话里改变行为」。

---

## 1. 为什么必须另造场景（现状的三个偏差）

此前所有轨迹侧验证都基于 `/archive/tmem/.tmem/` 内的既有轨迹，这是**自证循环**：用被测工具自己产生的、且在同一套代码库上产生的数据，来证明同一个工具正确。三个具体偏差：

| 偏差 | 具体表现 | 新场景如何打破 |
| --- | --- | --- |
| **语料偏差** | 仓库内轨迹的改动对象就是 tmem 自身源码，几乎必然伴随「改实现 + 改测试」的固定共变模式；无第三方语言/框架多样性 | 被测项目用 **Python**（tmem 自身是纯 JS/ESM），且刻意构造非对称共变（如 `rates.py` 改动**不**伴随测试改动） |
| **形态偏差** | e2e fixture 的 zstd 是 `zlib.zstdCompressSync` 产出的**单帧**，与 DSH 真实写出的**多帧拼接容器**形态不同（P0-4 假绿根源） | 场景直接使用 DSH 原生产出的 `session.v4.jsonl.zstd`，并以 `zstd -dc` 行数 ≠ Node 单帧解压行数为硬判定 |
| **规模/会话偏差** | 单仓、单作者、会话密度低，缺少「跨会话、跨天、增量捕获」的完整形态 | 8 场独立 headless 会话，跨多轮 git 提交、含增量与全量两种捕获路径 |

**这个场景要回答的核心问题**（既有报告无法回答）：

- Q1 在**外部项目**上，`tmem capture` 能否稳定产出非空轨迹？（P0-4 的真实暴露面）
- Q2 产出的记忆**内容**是否真的正确？（真值召回，不是"文件非空"）
- Q3 记忆**是否被模型消费**并改变了行为？（闭环最后一跳，仓库内验证从未验证过）

---

## 2. 被测场景设计：`ledger-svc`

### 2.1 载体

- 路径：`/home/kent/e2e/ledger-svc`（家目录下的独立路径；**不用 `/tmp`**——上一轮 `/tmp/tmem-validation/` 已被系统回收，证据全丢）
- 与 `/archive/tmem` 的关系：**零**。无 git remote、无 tmem 依赖、无 `.tmem/` 预置数据、无 `AGENTS.md` 继承
- 语言：**Python 3.12**（与 tmem 自身 JS 语料刻意不同）
- 构建者身份：`e2e-bot`（与 tmem 战役的 `tmem-tester` 区分，便于识别污染）

### 2.2 业务形态（发票台账服务）

刻意包含真实项目中让 agent "踩坑并需要记忆"的四类结构：

| 模块 | 内容 | 埋入的可记忆信号 |
| --- | --- | --- |
| `src/ledger/settlement.py` | 结算计算，含金额精度、汇率、舍入 | **L2 错误签名**：`decimal.InvalidOperation` |
| `src/ledger/rates.py` | 汇率表 + 缓存 | **L2 错误签名**：`KeyError: 'rate_cache'` |
| `src/ledger/idempotency.py` | 幂等键去重 | 陷阱：空 body 时 key 冲突 |
| `src/ledger/pagination.py` | 游标分页 | 陷阱：游标编码含时间戳，跨天不稳定 |
| `src/ledger/invoicing.py` | 发票开具 | 与 `settlement.py` **强共变** |
| `src/ledger/audit.py` | 审计日志 | 与 `invoicing.py` **弱共变** |
| `tests/test_*.py` (4 个) | pytest 用例 | 共变对的另一端 |

- 规模：15–20 个源文件、约 2500 行、4 个测试文件
- git 历史：**≥ 25 次提交、≥ 3 个分支、含 ≥ 2 次文件 rename**（`git log -M` 才能检出 rename 路径；否则共变引擎无法被真正检验）
- 仓库内**刻意预埋**两条负样本（改了但不该被记为共变），用于测共变引擎的精确率

### 2.3 真值（oracle）——与 tmem 完全独立

`tmem` 不参与真值的产生。真值由**我们自己手写**：

```yaml
# /home/kent/e2e/ledger-svc/oracle/ground_truth.yml
meta:
  project: ledger-svc
  version: 1
facts:                       # L0 事实：应出现在 MEMORIES.md / lookup 命中
  - id: F01
    question: "这个项目跑单测的正确命令是什么？"
    expect_any: ["pytest -q", "python -m pytest"]
  - id: F02
    question: "汇率表的默认兜底常量在哪？"
    expect_file: "src/ledger/rates.py"
  - id: F03
    question: "结算为什么不能用 float？"
    expect_any: ["Decimal", "decimal"]
repair_actions:               # L2 修复：期望被捕获的「错误签名 → 修复动作」
  - id: R01
    error_signature: "decimal.InvalidOperation"
    expect_fix_any: ["Decimal(str(", "quantize("]
  - id: R02
    error_signature: "KeyError: 'rate_cache'"
    expect_fix_any: ["rates or DEFAULT_RATES", "DEFAULT_RATES"]
cochange_pairs:               # L1 共变：期望被检出的强共变对
  - ["src/ledger/settlement.py", "src/ledger/invoicing.py"]
  - ["src/ledger/settlement.py", "tests/test_settlement.py"]
  - ["src/ledger/rates.py", "src/ledger/settlement.py"]
negative_pairs:               # 不应被判为共变
  - ["src/ledger/audit.py", "src/ledger/pagination.py"]
episodes:                     # 应被切出的 episode 阶段
  - id: E01
    intent_contains: "修 decimal 精度"
    expect_files: ["src/ledger/settlement.py"]
recall_questions:              # 消费侧：直接问模型的问题（每个带判定关键点）
  - id: Q01
    ask: "这个项目的结算为什么不能用 float？给我一句话结论。"
    keypoints: ["Decimal", "精度"]
    answerable_without_memory: false   # 必须靠记忆才能答对
```

**真值的三重隔离**：

1. oracle 文件写在被测项目的 `oracle/` 目录，**不被 agent 触及**（不 add 进 prompt，不出现在任何会话上下文）
2. oracle 判定脚本在 `_harness/` 下运行，与被测项目分离
3. oracle 由人工撰写，**不参考任何 tmem 输出**

---

## 3. 任务剧本：8 场真实会话

每场 = 一次独立的 `dsh headless "<任务>"`（真实模型调用、真实工具执行、真实 transcript 落盘）。

| 场次 | 任务（交给 DSH 的自然语言） | 期望 tmem 侧产出 | 检验点 |
| --- | --- | --- | --- |
| **S0** 探针 | "看一眼这个项目的结构，告诉我怎么跑测试" | — | **先量成本**：token/费用/耗时，作为后续预算闸的基准 |
| **S1** 基线 | "跑一遍单测，把失败原因记下来" | `capture` → `compiled`，trace/episodes 非空 | 管线基本可用；捕获**真实多帧** transcript |
| **S2** 精度修复 | "结算在某些金额上报错，修好它" | L2 错误签名 `decimal.InvalidOperation` 入库 + 修复动作 | R01 命中；动作可读 |
| **S3** 跨文件重构 | "把结算逻辑拆成独立模块，同步改测试" | episode 覆盖 settlement/invoicing/测试 | E01 文件集合 recall |
| **S4** 换会话 | **新 session**，同 workspace："现在这个项目的关键约定有哪些？" | `lookup` 命中 F01–F03 | 跨会话召回（跨 session_id 的持久性） |
| **S5** 消费侧 | **新 session**，只问不查："结算为什么不能用 float？" | 模型答案命中 keypoints | **闭环最后一跳**：记忆是否被消费 |
| **S6** 修复闭环 | **新 session**，人为制造同款错误后修复 | 注入的 L2 修复动作被采纳 | G6 采纳率 |
| **S7** 幂等/增量 | 对同一 session 重复 capture；再跑一次 `--full-session` | 增量不重复、全量可重放 | 幂等 + `last_captured_step_idx` 正确性 |

**对照组（必须做，否则 Q3 无意义）**：在**同一个 ledger-svc 的干净副本**上跑 S5 同款问题，但**不安装 tmem**（不加载 plugin、无 `.tmem/`）。若无记忆时模型也能答对（因为它会直接去读代码），则说明题目设计失败——因此 `recall_questions` 里标注 `answerable_without_memory: false`，并在验收时核对对照组的正确率显著低于实验组。

---

## 4. 执行环境与隔离

### 4.1 目录布局

```
/home/kent/e2e/ledger-svc/            # 被测项目（唯一安装 tmem 的地方）
  src/ledger/*.py  tests/*.py
  oracle/ground_truth.yml            # 真值（agent 不可见）
  .dsh/{plugins,tmem.patch.yml,skills}
  .tmem/{traces,episodes,raw}
  MEMORIES.md  AGENTS.md
/home/kent/e2e/ledger-svc-control/    # 对照组副本（无 tmem）
/home/kent/e2e/_harness/              # 驱动与取证（不放进被测项目）
  run_session.sh  capture_all.sh  score_gates.mjs
  raw/<scene-id>/{stdout.log,stderr.log,tmem-status.json,transcript-meta.txt}
/archive/tmem/docs/dsh-independent-e2e-report.md   # 结论报告（新增 untracked）
```

> 证据必须落 `/home/kent/e2e/_harness/raw/`，不要落 `/tmp`——上一轮 `/tmp` 证据被系统回收，结论一度失去支撑。

### 4.2 安装（被测项目内）

```bash
cd /home/kent/e2e/ledger-svc
tmem init                      # 生成 .dsh/plugins/tmem-dsh-plugin.mjs + .dsh/tmem.patch.yml
                               # + .dsh/skills/tmem/SKILL.md + AGENTS.md + post-commit hook
                               # + .tmem/ + MEMORIES.md
```

### 4.3 跑真实会话

```bash
cd /home/kent/e2e/ledger-svc
dsh --profile headless \
    --patch /home/kent/e2e/ledger-svc/.dsh/tmem.patch.yml \
    headless "跑一遍单测，把失败原因记下来"
```

> 关键点：0.4.1 已把 patch 内插件路径从 `./.dsh/plugins/...` 改为 `./plugins/...`（`tmem/installer.mjs:158`），配合 `--patch` 指向工作区 patch 才能正确挂载。这一点在既有报告 P0-1 中已验证过。

### 4.4 捕获与取证

```bash
tmem capture --agent dsh --workspace /home/kent/e2e/ledger-svc --force
tmem capture --agent dsh --workspace /home/kent/e2e/ledger-svc --full-session   # S7
tmem sync --workspace /home/kent/e2e/ledger-svc        # 重生成 MEMORIES.md
tmem status --workspace /home/kent/e2e/ledger-svc
```

**多帧真伪判定（每场必做，P0-4 的现场探针）**：

```bash
# Node 单帧解压 vs zstd -dc 全量解压，行数必须不同才说明是多帧容器
node -e 'const z=require("node:zlib"),f=require("node:fs");const p=process.argv[1];
console.log("node_bytes",z.zstdDecompressSync(f.readFileSync(p)).length)' "$T"
zstd -dc "$T" | wc -l
```

若两者一致 → 该场 transcript 是单帧，**这场的 P0-4 覆盖不成立**，必须在报告中标注并补跑。

### 4.5 凭据与费用

- 复用 `~/.dsh/.credentials.yaml`（现有 `refs:` 机制），**只作为进程环境注入，值不打印、不写入 ledger-svc、不写入报告**
- 先跑 S0 探针测单场成本 → 设单场 token 上限 → 超限即停并上报
- 全程不触碰 `~/.dsh/.credentials.yaml` 本身

### 4.6 全局污染与回滚

| 污染点 | 动作 |
| --- | --- |
| `~/.dsh/cordis.patch.yml`（`tmem init` 会追加 tmem 条目） | 执行前 `cp ~/.dsh/cordis.patch.yml /home/kent/e2e/_harness/cordis.patch.yml.bak`，收尾还原并 diff 确认 |
| `~/.dsh/sessions/<编码路径>/` | 会新增 `--home-kent-e2e-ledger-svc--` 目录（收尾保留为证据，或归档到 `_harness/raw/` 后删除） |
| `~/.dsh/profiles/headless/` | **不改**（复用现有 headless profile，不新建 profile——新建需另配凭据 refs，收益低风险高） |
| `/archive/tmem` | tracked 文件零改动；仅新增 untracked 报告与 `docs/evidence/` |
| 仓库 `.tmem/`、`benchmarks/runs/` | 本方案不写入这些目录 |

回滚清单（收尾必须逐项确认）：

```bash
cp /home/kent/e2e/_harness/cordis.patch.yml.bak ~/.dsh/cordis.patch.yml
diff ~/.dsh/cordis.patch.yml /home/kent/e2e/_harness/cordis.patch.yml.bak
cd /archive/tmem && git status --porcelain    # 期望：仅 4–5 项 untracked
```

---

## 5. 判定门（Gate 表）

每条门都是**可自动判定**的硬指标；任一 FAIL 即视为该维度不成立。

| 门 | 指标 | 阈值 | 判定方式 |
| --- | --- | --- | --- |
| **G1** | capture 成功率 | S1–S7 中 `status=compiled` ≥ 6/7 | 解析 `tmem-status.json` |
| **G2** | 真实多帧覆盖 | ≥ 5 场 transcript 呈多帧形态 | 4.4 的行数对比 |
| **G3** | 切片质量（内容） | episode 覆盖 oracle 文件集的 recall ≥ 0.8、precision ≥ 0.5 | 对 `ground_truth.yml` 求交 |
| **G4** | 真值召回 | 20+ 条 facts 经 `tmem lookup` 命中 ≥ 0.8；MEMORIES.md 三节覆盖率 ≥ 0.9 | `tmem lookup --format json` + 解析 MEMORIES.md |
| **G5** | **消费侧（关键）** | 实验组 keypoints 命中率 ≥ 0.7，且对照组 ≤ 0.2（差值 ≥ 0.5） | 解析 S5 回答文本 |
| **G6** | L2 修复闭环 | 注入的修复动作被模型采纳 ≥ 0.8 | 对比 S6 的 patch 是否含 `expect_fix_any` |
| **G7** | 共变检出 | 3 条 `cochange_pairs` 检出 ≥ 3，置信度 ≥ 0.5；`negative_pairs` 误报 ≤ 1 | `tmem` 共变统计 |
| **G8** | 性能 | 单次 capture < 5s；expand latency < 25ms（与 CI 门同口径） | 计时；**同机同时跑 10 次 `npm test`** 记录 P0-3 红门是否复现，作为负载对照 |
| **G9** | 污染 | 仓库 tracked 零改动；`~/.dsh` 之外零改动；凭据值零泄漏 | `git status` + 文件清单 diff + 报告全文检索 |

**G5 是本方案的核心**：它检验「记忆 → 模型行为」的最后一跳，既有验证从未覆盖过。若 G1/G4 通过但 G5 失败，结论是「记忆被生产出来了但没被消费」——这属于**产品级缺陷**，不是实现 bug。

---

## 6. 分阶段流程

| 阶段 | 内容 | 产出 | 预计耗时 |
| --- | --- | --- | --- |
| **P0 准备** | 建 `/home/kent/e2e/`；备份 `~/.dsh/cordis.patch.yml`；跑 S0 探针量成本；确认凭据注入可用 | 成本基准 + 回滚备份 | 0.5 天 |
| **P1 Fixture** | 写 ledger-svc 源码/测试/git 历史（≥25 提交、含 rename、含 2 条负样本共变）；写 `oracle/ground_truth.yml` | 可复现的项目 + 真值 | 1 天 |
| **P2 管线自检**（**不调模型**） | 用 P1 产物手工构造 DSH 形态的**多帧** transcript，喂 `tmem capture --transcript`；验证 G1/G3/G7 的解析与切片链路；**同时复现 P0-4 在真实多帧下的行为** | 管线级结论 + P0-4 真实暴露面结论 | 0.5 天 |
| **P3 真实会话** | S1–S7 逐场 `dsh headless`，每场立即 capture + 记录原始输出；对照组跑 S5 同题 | `_harness/raw/` 全量证据 | 1–2 天 |
| **P4 判定** | `score_gates.mjs` 跑 G1–G9，产出 `metrics.json` | 机器可读判定 | 0.5 天 |
| **P5 报告与回滚** | 写 `docs/dsh-independent-e2e-report.md`；按 4.6 回滚 | 报告 + 干净环境 | 0.5 天 |

**P2 是关键的前置闸**：它不需要模型调用就能验证「P0-4 是否真的挡住真实多帧」。若 P2 发现未修复，则 P3 的 G1/G5 在修复前**注定 FAIL**，此时应先修 tmem 再跑 P3，而不是拿一个已知坏的版本去消耗模型费用。

---

## 7. 硬前置依赖（必须先解决）

> **P0-4（zstd 多帧只解首帧）未修复，是本方案的硬前置。**

已验证事实（见 `docs/dsh-validation-report.md` §10）：

- DSH 写的是**拼接帧容器**（`scanZstdFrames` / `PublicZstdFrameDecoder`，见 `@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`）
- 0.4.1 的 `readTranscriptText`（`tmem/adapters/dsh.mjs:201-209`）整文件 `zstdDecompressSync` → 只解首帧；因 Node ≥22.15 存在该 API，**永远走不到 `zstd -dc` 的 fallback 分支**
- 真实多帧 transcript 上：`zstd -dc` 得 291,433 字节 / 163 行，Node 只得 189 字节 / 1 行
- 端到端表现：`tmem capture --agent dsh --force` 返回 `{"status":"skipped","reason":"no_effective_actions"}`（0.4.0 是 `no_transcript_found`）——**更隐蔽的静默失败**

因此，**在修 P0-4 之前**：

- 方案 A（**推荐**）：先修 `readTranscriptText` 为逐帧解码（对齐 DSH 的 `scanZstdFrames`，或直接调 `zstd -dc`），并把 e2e fixture 从单帧改成多帧拼接，再跑 P3
- 方案 B（仅用于取证，不作为验收）：显式 `tmem capture --transcript <解压后的明文 .jsonl>` 绕过压缩层——能验证语义，但**验证不到真实格式链路**，G2 门必须标 FAIL
- 方案 C：给 `tmem` 加"帧数不一致即告警"的显式失败而非静默 skip（最小止血，可与方案 A 合并）

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| **R1 P0-4 未修** | 真实多帧下 capture 静默 skip，G1/G5 必 FAIL | 第 7 节硬前置；P2 阶段先暴露，不浪费模型费用 |
| **R2 模型非确定性** | 同一任务两次结果不同，指标抖动 | 关键场（S5/S6）每场重复 3 次取中位数；对照组同重复 |
| **R3 费用失控** | 8 场真实调用 | S0 探针测成本 → 单场上限 → 超限停机；对照组用同一模型以保证可比 |
| **R4 全局污染** | 改到 `~/.dsh` 或真实工作区 | 4.6 回滚清单；被测项目放家目录独立路径；所有原始日志先落 `_harness/raw` |
| **R5 `/tmp` 回收** | 证据丢失 | 证据目录定在 `/home/kent/e2e/_harness/raw`，报告引用绝对路径 |
| **R6 fixture 质量不足** | 共变引擎检不出（git 历史太干净） | P1 硬性要求：≥25 提交、含 rename、含 2 条负样本；先自测共变检出再进 P3 |
| **R7 对照组不公平** | 无 tmem 组也能答对 → G5 无意义 | `answerable_without_memory: false` 标注 + 对照组结果必须先看；对照组若能答对，改题目 |
| **R8 与既有结论冲突** | 两套结论打架 | 以独立场景为准；冲突即**新缺陷**，写入报告而非覆盖既有 §1–§11 |
| **R9 tmem 依赖被测项目的 git 质量** | 无 git 则共变/ground-truth 全失效 | fixture 必须自带真实 git 历史，且 P2 阶段先验证 |

---

## 9. 与既有报告的关系

- 新结论写入**独立文档** `docs/dsh-independent-e2e-report.md`，不修改 `docs/dsh-validation-report.md` 的 §1–§11（既有结论保持原样，便于对照"仓库内验证 vs 外部项目验证"）
- 两份报告的差异本身是结论：**若两者不一致，说明 tmem 对仓库内形态存在过拟合或特定假设**，这是高价值发现
- 复现命令、门槛实现、原始证据路径全部在 P4/P5 固化，证据落 `docs/evidence/`（沿用既有惯例）

---

## 10. 执行前需要用户确认的事项

1. **目录**：`/home/kent/e2e/` 是否可用作独立场景根目录（家目录，避免 `/tmp` 回收）？
2. **费用上限**：8 场 + 对照组的模型调用预算上限是多少？
3. **P0-4 是否先修**：P3 之前是否允许修 `readTranscriptText` 的多帧解码（否则 G2/G5 无法验收）？
4. **profile 策略**：确认复用现有 `headless` profile（改 `~/.dsh/cordis.patch.yml` 后还原）——是否接受？替代方案是新建隔离 profile，但需要额外配凭据 `refs:`，成本与风险更高。

---

## 11. 本方案明确不做的事

- 不修改 `/archive/tmem` 的任何 tracked 代码（本轮只新增 untracked 文档）
- 不发布 npm、不打 tag
- 不发起任何模型调用（本轮只出方案）
- 不触碰 `~/.dsh/.credentials.yaml`，不打印凭据值
- 不修改真实 `~/.dsh/sessions/` 中既有会话
- 不复用 `/tmp/tmem-validation/`（已被回收）
