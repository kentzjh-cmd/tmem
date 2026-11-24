# 研究报告：记忆载体消融与量化——tmem 结构化记忆 vs 散文式 MD 记忆 vs 无记忆

> **研究类型**：受控消融实验（因子设计 + 内容等价控制 + 预注册门槛 + 重复采样 + 机器可读判定）
> **协议**：[`docs/dsh-memory-carrier-ablation.md`](./dsh-memory-carrier-ablation.md)
> **预注册**：`/home/kent/e2e/_harness/ablation/oracle_carrier_ablation_v1.2.json`（v1.2 冻结于 2026-11-24T07:14:52+08:00，**在 Phase-1b 的第一次模型调用之前**；阈值/提示词/关键点与 v1.1 **逐字相同**，v1.1 原文保留于 `oracle_carrier_ablation.json`）
> **执行装置**：`/home/kent/e2e/_harness/ablation/`（仓库快照：[`research/carrier-ablation/`](../../research/carrier-ablation/)）
> **判定产物**：`/home/kent/e2e/_harness/abl-out/metrics-carrier-ablation-v1.2.json` / `.md`
> **本报告数据 = Phase-1b**（容器隔离重跑，30/30 场有效、零越界读取）；Phase-1 的同门槛结果作为对照见 §4.5

---

## 0. 一句话结论

**tmem 的优势在"零检索可得"，不在"总 token 更少"。** 30 场容器隔离的消融会话给出：把记忆注入上下文后，模型**不做任何检索**即可答出全部记忆专有事实（`zero` 变体命中率 A1 = 1.000，而散文 MD 按需读取 B2 = 0.000、无记忆 C = 0.000）——预注册的 CA-E1 **通过**；但一旦允许自由查阅，tmem 臂的**总**知识获取成本（均值 18 017 token）反而**高于**散文臂翻文件（5 073 token），因为 agent 拿到切片后会继续调用 `tmem_expand` 展开细节，于是 CA-1 / CA-1b / CA-4 三个成本门槛**全部未通过**（CA-5 载体等价性通过）。**结论是：结构化载体的收益应按"可得性/零检索"而不是按"总上下文 token"来卖。**

> 数据来源：Phase-1b（容器隔离重跑，**29/30 场有效**，唯一一场因冻结的 A7 规则被判越界而机械作废，且未泄漏任何事实）；Phase-1 的同门槛结果作为对照一并给出，两阶段判定完全一致。

---

## 1. 研究问题与预注册假设

上一轮独立 E2E 研究（[`docs/dsh-independent-e2e-report.md`](./dsh-independent-e2e-report.md)）留下三个未解的混淆：

1. **"记忆内容"与"记忆载体"被混在一起测**：三臂对照中 tmem 臂与散文臂命中率相同，因为 agent 会**自己**把约定写进 `README.md`/`AGENTS.md`，把载体效应遮蔽掉；
2. **"无插件臂"实际测的是"无自动注入"**：全局 `tmem` CLI 仍在 `PATH` 上且被 agent 主动调用；
3. **预注册的 G7 探针不具备记忆专有性**：关键点分别来自提示词、fixture 初始代码与代码常量，边际贡献测不出来。

本课题把载体与获取方式拆成两个正交因子，用**同一份事实**渲染出两种载体，再用机器可判定的门槛回答：

| 假设 | 陈述 | 对应门槛 |
| :--- | :--- | :--- |
| **H-A** | 定向注入以显著更少的 token 取得同等知识量 | CA-1 / CA-1b |
| **H-B** | 结构化载体降低同类问题的复发 | CA-2（Phase-2） |
| **H-C** | 共变拓扑与"错误签名→修复对"是散文载体无法表达的信息 | CA-3（Phase-2） |
| **H-D** | 定向注入优于全量注入，即使载体相同 | CA-4 |
| **H-E/H-F** | 压缩后能以更少 token 找回被丢弃的知识；重注入缺口可修复 | CA-6–CA-9（Phase-2） |

**Phase-1 范围**：只交付成本主判据（CA-1/CA-1b）、获取方式效应（CA-4）、效果可得性（CA-E1）与载体等价性前置门（CA-5）。CA-2/CA-3 需要跨位置复现错误与耦合改动的专门场景，CA-6–CA-9 需要可复现的压缩触发机制（`compaction_committed` + 非空 `shadowedSeqs` 校验）；把它们与载体效率混在一轮里跑，会让"可测性不足"与"效应不存在"两种失败无法区分。**理由与取舍写在协议 §8.1。**

---

## 2. 装置

### 2.1 因子与臂

| 臂 | 载体 | 获取方式 | 挂载方式 | 隔离 DSH_HOME | `tmem` CLI |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A1** | tmem 结构化（`.tmem/`） | **自动注入** | 工作区补丁（`tmem init`） | `homes/plain`（无 tmem 条目） | 可用 |
| **A2** | tmem 结构化 | **按需检索** | 同上 + PATH 指向打了补丁的包副本 | `homes/plain` | 可用（补丁副本） |
| **B1** | 散文 MD（`MEMORY.md`） | **全量注入** | `md-carrier` 插件补丁 | `homes/md` | **被封禁** |
| **B2** | 散文 MD | **按需读取** | 无插件，MD 只是工作区文件 | `homes/plain` | **被封禁** |
| **C** | 无记忆 | — | 无插件、无 MEMORY.md、无 `.tmem` | `homes/plain` | **被封禁** |

每个臂 3 次独立重复（独立工作区副本），每种变体各一轮，合计 **5 臂 × 2 变体 × 3 重复 = 30 场**会话，另加 2 场播种会话与 5 场挂载自检。

### 2.2 单一事实源 → 两种载体（CA-5 的构造性保证）

本课题最关键的设计约束是**内容等价**：若散文载体比 tmem 载体多记或少记任何事实，比较的就是"谁记得多"而不是"载体谁更好"。做法是**不手写散文载体**，而是从 tmem 载体的 episode 记录**机械渲染**：

```
.tmem/episodes/*.json  ──render_prose_carrier.py──▶  MEMORY.md（散文载体）
        │                                              │
        └──────── 同一批 episode 记录 ──────────────────┘
```

渲染器只搬运 episode 的既有字段（`phase_intent` / `decisions` / `l2_causal_repairs` / `verification_cmds` / `error_signatures` / `touched_files`），**不新增、不改写、不概括**；因此等价性由构造保证，并可用 grep 独立复核（CA-5 前置门即检查每个关键点在两种载体中都可检出）。

### 2.3 A2 的机制消融（关闭自动注入，只关闭这一条）

A2 要测的是"按需读取"这一**获取方式**，因此必须让插件在位、工具可用，但**不自动注入**。实现方式是给 A2 一份**打过补丁的 tmem 包副本**（PATH 上的 `tmem` 指向它），补丁只有一行：

```diff
-    if (preBlocks.length > 0) {
+    if (preBlocks.length > 0 && process.env.TMEM_ABLATE_NO_INJECT !== "1") {
       return { injectedContext: preBlocks.join("\n\n") };
```

捕获、遥测、检索工具、系统提示段全部不变；补丁以 diff 形式留档在 `a2_patch.json`。

### 2.4 对照臂的"逐场可证无记忆"

上一轮的两条教训被直接写进装置：

1. **全局 `tmem` CLI 从 `PATH` 上消失**（对照臂前置 `blockbin`，`tmem` 一律拒绝执行）——否则测的是"无自动注入"而不是"无记忆"；
2. **独立 DSH_HOME**（home patch 无 tmem 条目）+ **每场前后机械断言**：对照臂一旦出现 `tmem_search`/`tmem_expand` 调用或 `<tmem_speculative_memory>` 注入块，该样本**直接作废并留痕**，不允许人工挑选；
3. **播种会话的 agent 自写笔记被清除**（协议陷阱 #1：自记笔记会遮蔽载体差异——本次播种中 agent 自写的 `MEMORY.md` 确实含有 Q1 的关键点）；
4. **所有臂的工作区一律复位到原始 fixture**（`git checkout -- .`），让"读代码外得答案"这条路径在所有臂上等价。

**Phase-1b 追加的第 5、6 条**（由 Phase-1 的事后审计直接驱动，见 §3.1 与协议 §12）：

5. **结构性隔离（容器白名单）**：每个 (臂, 重复, 变体) 会话跑在独立容器 `tmem-abl:iso` 中，容器文件系统只有白名单 `/work`（本臂工作区）、`/dsh-home`（**本会话独占**的 DSH_HOME，目录名带随机后缀，无法被猜到）、`/home/kent/.nvm`、`/archive/tmem`（净化副本）与本臂 shim 目录；**`/home/kent/e2e`、`/home/kent/.dsh`、`/archive/tmem/docs` 与其它臂在容器内根本不存在**。Phase-1 的越界读取之所以能发生，是因为**同一 UID 下 chmod 挡不住属主**（他随时可以 `chmod` 回去）——只有挂载层面的"不可见"才是结构性隔离。
6. **越界读取自动作废（A7）**：判定器从每场 transcript 的工具调用里抽取所有绝对路径，凡落在白名单之外即判该样本作废，理由机器可读（`out_of_arm_read:<path>`）。这把"逐场可证"从**事后人工审计**变成**装置属性**。执行结果：29 场零越界，1 场（C rep3 free）9 次越界 → 作废（详见 §4.1.1）。

### 2.5 提示词

五臂**逐字相同**（预注册在 oracle 里）：

- **free**（主判据，允许查阅项目记忆）：`你可以查阅本项目可用的项目记忆来回答问题，但不要运行任何命令。…`
- **zero**（补充，禁止读取与执行）：`不要读任何文件，也不要运行任何命令。只根据你当前上下文里已有的信息回答三个问题：…`

三问分别针对：①本次修复**改了哪几个文件、按什么顺序**；②**用什么命令验证**；③**新增的统一价格校验入口函数叫什么**。三者都只存在于结构化记忆里——对原始 fixture 全库 grep，`ensure_usable_price` 与 `collect-only` 命中均为 **0**。

> **注意**：`free` 变体允许读代码，因此命中率在所有臂上会趋同（代码本身含部分线索）；**效果判定放在 `zero` 变体**（唯一记忆专有的口径），`free` 变体的命中率只作描述性统计。这条修订记在 oracle 的 `amendments.A2-CA-E1-variant`，发生在第一次模型调用之前。

### 2.6 度量口径

| 指标 | 定义 | 采集 |
| :--- | :--- | :--- |
| **知识获取 token** | 注入块 token + 工具输出 token | transcript 的 `user/message` 注入块 + `tool/result` 输出；与上一轮 `score_h6.py` 同一口径 |
| **命中率** | 3 个关键点问题的答对比例 | stdout 文本；答对 = 该题全部关键点出现（Q1 还要求顺序正确） |
| **有效性** | 载体混淆的机械判定 | 见 §2.4；作废样本与理由写进 `metrics-carrier-ablation.json` |

聚合：每臂每变体 3 次取均值，同时报每次原值与 95% 置信区间（n=3 用 t(0.975, df=2)=4.303）；**不剔除离群值**。

---

## 3. 执行记录

### 3.1 Phase-1（2026-11-23，已隔离存）

| 阶段 | 时间 | 结果 |
| :--- | :--- | :--- |
| 预注册冻结（v1.0） | 2026-11-23 22:53 | 提示词、关键点、阈值、作废规则写入 oracle |
| 预注册修订（v1.1） | 2026-11-23 23:00 | 三处修订（关键点对齐本课题播种事实、CA-E1 改判 zero 变体、工作区复位口径），**均在第一次模型调用之前**，v1.0 原文保留 |
| 播种会话（S1+S2） | 2026-11-23 22:55–23:00 | 产出 4 个 episode / 3 个 trace；agent 自写笔记随后清除 |
| 散文载体渲染 | 2026-11-23 23:02 | 4 个 episode → 6 283 字节 `MEMORY.md`；**CA-5 等价性检查通过** |
| 建臂（5 × 3） | 2026-11-23 23:03 | 15 个工作区副本 + 2 个隔离 home + A2 补丁副本 |
| 挂载自检 | 2026-11-23 23:09 | **5/5 通过**：A1/A2 有 tmem 工具无注入，B1 有散文注入无 tmem 工具，B2/C 全清 |
| 正式执行 | 2026-11-23 23:03–23:16 | 30 场探针会话，零作废 |
| **事后审计** | 2026-11-23 23:2x | **发现三处装置缺陷**（D1 越界读取 / D2 注入节奏不等价 / D3 `.pyc` 残留）→ Phase-1 数据标记 **QUARANTINED**，保留披露、不用于门槛判定 |

### 3.2 Phase-1b（2026-11-24，容器隔离重跑）

| 阶段 | 时间 | 结果 |
| :--- | :--- | :--- |
| 缺陷归因与修订写定 | 2026-11-24 07:0x–07:14 | oracle **v1.2** 冻结（07:14:52，**在 Phase-1b 第一次模型调用之前**）；四条修订 A4–A7；脚本断言 `prompts/keypoints/metrics/gates` 与 v1.1 **逐字相同** |
| 容器隔离镜像 | 2026-11-24 07:0x | `tmem-abl:iso`（ubuntu:22.04 + git/python3/pytest，uid 1000 非 root） |
| 建臂（5 × 3，卫生版） | 2026-11-24 07:1x | 15 个工作区：`git checkout -- .` + `git clean -fdx -e .tmem` + **关键点扫描**（C 臂命中 0；A 臂命中全部落在 `.tmem/` 与 `MEMORIES.md`；B 臂只落在 `MEMORY.md`） |
| 容器内隔离探针 | 2026-11-24 07:16 | **5/5 通过**：`/home/kent/e2e`、`/home/kent/.dsh`、`/archive/tmem/docs`、oracle、其它臂在容器内**不存在**（`abl-out/isolation_probe.txt`） |
| 五臂挂载自检 | 2026-11-24 07:19–07:34 | **5/5 通过**（A1/A2 有 tmem 工具无注入；B1 有散文注入无 tmem 工具；B2/C 全清） |
| 正式执行 | 2026-11-24 07:35– | 30 场探针会话，每场一个独立容器 + 独立 DSH_HOME |

---

## 4. 结果

> 判定产物：`/home/kent/e2e/_harness/abl-out/metrics-carrier-ablation-v1.2.json` / `.md`
> （口径：oracle v1.2；阈值与 v1.1 逐字相同）

### 4.1 执行完整性

| 项 | 结果 |
| :--- | :--- |
| 计划会话 | 5 臂 × 2 变体 × 3 重复 = 30 |
| 完成 | **30 / 30 场都跑过**（`C rep3 free` 跑过两次：首跑 900 秒超时 → 作废重跑） |
| 有效样本 | **29 / 30**（1 场被冻结的 A7 规则机械作废，理由见下） |
| **越界读取（A7 自动作废规则）** | **29 场为 0；1 场共 9 次**——全部集中在 `C rep3 free`（重跑那次）：该场 agent 用 `glob` / `grep` / `bash` 枚举了 `/home/kent`、`/home/kent/abl2`、`/archive`、`/archive/pentest_work_flow` |
| 隔离机制 | 每场一个独立容器 `tmem-abl:iso`，挂载白名单见 §2.4 与协议 §12.3 |

> **被作废那一场的性质要说清楚**：它枚举的路径在容器里**已被净化**——`/home/kent/e2e`（课题判定文件与 Round 2 记忆库所在）、`/archive/tmem/docs`（含关键点的研究文档）、以及其它臂的工作区**都不存在**；它能看到的 `/home/kent` 只有 `.nvm` 与 `abl2/{a2bin,blockbin,plugins,tmem-a2}`（无任何课题事实），`/archive` 只有 `pentest_work_flow`（headless profile 的插件依赖）。也就是说：**没有发生事实泄漏，但冻结的 A7 规则按字面把它判为越界 → 作废**。我们**不**为此追加事后豁免（那会破坏预注册的机械判定），C 臂 `free` 变体因此是 **n = 2**；CA-E1 只判 `zero` 变体（C_zero n = 3，全部有效），**所有门槛判定不受影响**。
>
> 对照：Phase-1 里 C 臂有 **3 场**越界读取了**别的实验的记忆库**（读到了事实）；Phase-1b 的越界只发生在容器内的净化路径上，**读不到任何事实**。

### 4.1.1 作废样本明细

| 样本 | 机械理由 | 处置 |
| :--- | :--- | :--- |
| `C rep3 free`（第 1 次） | `session failed (exit=-1)`（900 秒超时，agent 长时间翻源码） | 作废并重跑 |
| `C rep3 free`（第 2 次） | `out_of_arm_read:/home/kent`、`out_of_arm_read:/home/kent/abl2`、`out_of_arm_read:/archive`、`out_of_arm_read:/archive/pentest_work_flow`（共 9 次） | **作废，不重跑**（避免按结果挑样本） |

### 4.2 逐臂逐变体结果

| 臂 \| 变体 | n | 知识获取 token（均值, 95%CI） | 命中率（均值, 95%CI） | 每次原值 |
| :--- | :---: | :--- | :--- | :--- |
| **A1** tmem/自动注入 · free | 3 | **18 016.7** (10 184.7–25 848.6) | 0.889 (0.411–1.367) | 21 145 / 14 840 / 18 065 |
| **A1** tmem/自动注入 · zero | 3 | **1 387.7** (1 381.9–1 393.4) | **1.000** (1.000–1.000) | 1 389 / 1 389 / 1 385 |
| **A2** tmem/按需检索 · free | 3 | 14 250.3 (8 659.3–19 841.4) | 1.000 (1.000–1.000) | 14 845 / 11 762 / 16 144 |
| **A2** tmem/按需检索 · zero | 3 | 17 446.7 (4 926.4–29 966.9) | 1.000 (1.000–1.000) | 12 997 / 16 423 / 22 920 |
| **B1** 散文/全量注入 · free | 3 | 7 112.3 (3 812.3–10 412.3) | 1.000 (1.000–1.000) | 5 666 / 7 393 / 8 278 |
| **B1** 散文/全量注入 · zero | 3 | 2 178.0 (2 173.0–2 183.0) | 1.000 (1.000–1.000) | 2 176 / 2 180 / 2 178 |
| **B2** 散文/按需读取 · free | 3 | **5 073.0** (1 847.1–8 298.9) | 1.000 (1.000–1.000) | 6 381 / 5 054 / 3 784 |
| **B2** 散文/按需读取 · zero | 3 | 6.7 (−9.5–22.8) | **0.000** (0.000–0.000) | 7 / 13 / 0 |
| **C** 无记忆 · free | 2 | 8 775.0 (1 568.1–15 981.9) | **0.000** (0.000–0.000) | 7 100 / 10 450（第 3 次作废，见 §4.1.1） |
| **C** 无记忆 · zero | 3 | 9.7 (−12.4–31.7) | **0.000** (0.000–0.000) | 17 / 0 / 12 |

> `zero` 变体里 B2/C 的 token 近乎为 0，是因为提示词禁止读文件与执行命令，模型只能凭上下文作答——**这正是它作为"记忆专有"口径的价值**。

### 4.3 门槛判定

| 门槛 | 判定 | 实测值 | 阈值 |
| :--- | :---: | :--- | :--- |
| **CA-5** 载体等价性（前置门） | ✅ **PASS** | 4 个 episode → 6 283 字节散文；3 个关键点在两个载体中**全部**可检出 | 所有关键点 in_tmem_carrier AND in_md_carrier |
| **CA-1** 成本优势（tmem 注入 vs 散文翻文件） | ❌ **FAIL** | A1 = 18 016.7，B2 = 5 073.0，比值 **3.55×**（需 ≤ 0.30×） | `mean(A1_free.knowledge_tokens) <= 0.30 * mean(B2_free.knowledge_tokens)` |
| **CA-1b** 成本优势（tmem 注入 vs 散文全量注入） | ❌ **FAIL** | A1 = 18 016.7，B1 = 7 112.3，比值 **2.53×** | `mean(A1_free.knowledge_tokens) <= 0.30 * mean(B1_free.knowledge_tokens)` |
| **CA-4** 获取方式效应可分离 | ❌ **FAIL** | 命中率先满足（1.000 ≥ 1.000 / 1.000 ≥ 1.000），但成本两项都不满足：A1 18 016.7 > A2 14 250.3，B1 7 112.3 > B2 5 073.0 | 见 oracle |
| **CA-E1** 效果优势（记忆专有事实，zero 变体） | ✅ **PASS** | A1_zero **1.000** > C_zero **0.000**，且 ≥ B2_zero **0.000** | `mean(A1_zero.hit_rate) > mean(C_zero.hit_rate) AND mean(A1_zero.hit_rate) >= mean(B2_zero.hit_rate)` |
| CA-2 / CA-3 / CA-6–CA-9 | — | 本阶段不执行（理由见协议 §8.1 与 oracle `gates[*].reason`） | — |

**2 / 5 通过**（CA-5、CA-E1），三个成本门槛未通过。

### 4.4 描述性统计（不构成门槛）

| 指标 | Phase-1b | Phase-1（对照） | 解读 |
| :--- | :---: | :---: | :--- |
| `injection_only_ratio` = mean(A1 注入 token) / mean(B2 知识获取 token) | **0.2736** | 0.2738 | **注入 tmem 切片 ≈ 翻散文文件成本的 27%**（两个独立阶段几乎复现） |
| `free_variant_total_ratio` = mean(A1 知识获取 token) / mean(B2 知识获取 token) | 3.5515 | 4.3065 | 允许自由查阅时，tmem 臂**总成本更高** |
| mean(A1 注入 token) | 1 388 | 1 387 | tmem 切片体量稳定（自动注入） |
| mean(B1 注入 token) | 2 181 | 12 357 | 散文注入从 6 份降到 1 份（v1.2 节奏修复，见 §4.6） |

**读输出分类**（free 变体，均值 token）：

| 臂 | carrier（读载体 / tmem 检索） | code（读源码与测试） | other（其余工具输出） |
| :--- | ---: | ---: | ---: |
| A1 tmem/注入 | **16 169** | 307 | 149 |
| A2 tmem/检索 | 13 162 | 614 | 470 |
| B1 散文/注入 | 2 168 | 2 291 | 467 |
| B2 散文/读取 | 2 168 | 2 428 | 472 |
| C 无记忆 | **0** | **6 532** | 2 233 |

这张表解释了三个成本门槛为什么失败：**A 臂的成本几乎全部来自 agent 自己后续的 `tmem_expand` 展开**（carrier 列 13k–16k），而 C 臂（无记忆）把成本花在了读源码上（code 列 6 532）。

### 4.5 与 Phase-1 的对照：判定完全一致

| 门槛 | Phase-1（已隔离存） | Phase-1b（报告口径） | 一致？ |
| :--- | :---: | :---: | :---: |
| CA-5 | ✅ PASS | ✅ PASS | ✅ |
| CA-1 | ❌ FAIL（比值 4.31） | ❌ FAIL（比值 3.55） | ✅ |
| CA-1b | ❌ FAIL（比值 1.22） | ❌ FAIL（比值 2.53） | ✅ |
| CA-4 | ❌ FAIL | ❌ FAIL | ✅ |
| CA-E1 | ✅ PASS | ✅ PASS | ✅ |

**污染对数字本身的影响有多大？** 看 C 臂（无记忆）的 `free` 变体：**Phase-1 = 52 889 token，Phase-1b = 8 775 token（降 6.0×）**。原因很直白：Phase-1 那三场"无记忆"会话其实在**读别的实验的记忆库**（读到的是 3 万 token 量级的陌生内容），Phase-1b 的 C 臂只能读本项目的源码（8 775 token）。**一个被污染的基线会把自己的成本也一起放大**，这正是"无记忆"对照必须逐场可证的原因。

**两阶段的门槛判定逐项一致**——这是本研究最有价值的一条元结论：**Phase-1 的结论方向没有因为装置缺陷而改变**；装置缺陷改变的是**数值**（B1 注入量从 12 066 降到 2 181，比值从 1.22 变成 2.53）和**可信度**（对照臂第一次真正无记忆、第一次零越界）。换句话说：**Phase-1 的"FAIL"是侥幸对的，Phase-1b 的"FAIL"是装置上可证的。**

### 4.6 v1.2 节奏修复的量化影响（A5）

| 口径 | B1 每场注入份数 | B1 注入 token 均值 | CA-1b 比值 |
| :--- | :---: | :---: | ---: |
| Phase-1（注入器每步注入） | 6 / 7 / 4 | 12 066 | 1.22 |
| Phase-1b（每用户轮注入） | 1 / 1 / 1 | 2 181 | 2.53 |

修复前，B1 的"注入成本"里混进了 agent 的步数；修复后两个臂的注入节奏对齐（tmem 1 份 ≈ 1 388 token，散文 1 份 ≈ 2 181 token），CA-1b 才有资格比较载体。**比值从 1.22 变成 2.53，说明旧口径把这个门槛测"软"了。**

---

## 5. 讨论

<!-- 讨论段落 -->

---

## 5. 讨论

### 5.1 三个成本门槛为什么失败：口径把"解题成本"算进了"知识获取成本"

协议把"知识获取 token"定义为**注入块 + 全部工具输出**。在 `free` 变体里，agent 拿到记忆之后会**继续展开细节**：

- **A1**（tmem 注入）：拿到 1 388 token 的切片后，agent 又调用了 `tmem_expand` 9 次左右，把 trace 细节拉回上下文 → carrier 类读输出 **16 169 token**，总成本 18 017；
- **B2**（散文翻文件）：散文只有 2 168 token，agent 读一次就够 → 总成本 5 073；
- **C**（无记忆，n=2）：什么也没得读，于是去读源码找线索 → code 类读输出 6 532，总成本 8 775。

所以 **CA-1/CA-1b/CA-4 的失败并不是"tmem 更贵"，而是"这个口径把 agent 的好奇成本算在了账上"**。换成只比注入块，`injection_only_ratio = 0.2736`（两阶段复现），tmem 明显更省。**两个数字都是真的，区别在于你在问什么问题。**

### 5.2 载体真正的差异在"可得性"，在 `zero` 变体里才干净

| 变体 | A1 tmem 注入 | A2 tmem 检索 | B1 散文注入 | B2 散文读取 | C 无记忆 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `zero`（不得读取） | **1.000** | 1.000 | 1.000 | **0.000** | **0.000** |
| `free`（可查阅） | 0.889 | 1.000 | 1.000 | 1.000 | 0.000 |

- `zero` 变体是**唯一**能把"记忆"和"读代码"分开的口径：在那里 B2（散文按需读取）和 C（无记忆）都是 0.000，而任何**自动注入**臂都是 1.000。**注入本身就是可得性的来源。**
- `free` 变体里 B2 也能答对（1.000）——因为散文文件就在工作区里，agent 自己去读就行；但 C 仍然是 0.000（读源码猜不出"本次修复改了哪几个文件、按什么顺序"）。
- A1 的 `free` 命中率 0.889 低于其它臂，**不是** tmem 变差了：那是 rep1 里 Q2（验证命令）被答成"泛化的 pytest 命令"而没有出现 `collect-only`；同一次会话的 `zero` 变体是 3/3。

### 5.3 对产品叙事的直接含义

本研究**不支持**"tmem 让上下文 token 更少"这句话作为**总成本**主张：允许自由查阅时 tmem 臂的总 token 反而更高（3.55× 于散文翻文件臂）。本研究**支持**的是两句更窄、更硬的话：

1. **零检索可得**：只要载体在位，模型不需要任何检索动作就能用上记忆（`zero` 变体命中率 1.000 vs 0.000）；
2. **注入切片比整份散文便宜约 3.6 倍**（1 388 vs 2 181 token，注入口径），且这一比值在两个独立阶段复现（0.2736 / 0.2738）。

而"按需展开"（`tmem_expand`）在这次探针里是**净负担**：agent 用它把成本从 1 388 抬到 18 017。**这正是 tmem 应当在产品上继续打磨的地方**——展开的粒度与触发条件，而不是注入本身。

### 5.4 方法学收获（超出本课题）

1. **同 UID 的 chmod 不是隔离**：属主随时能 `chmod` 回去。Phase-1 的越界读取正是这么发生的。要做对照，隔离必须是**挂载层面不可见**（容器白名单），并**逐场自动断言**（本课题的 A7）。
2. **注入节奏必须两侧对齐**：否则"比载体"的门槛实际在比"注入了几次"。这是 Phase-1 → Phase-1b 里唯一**可能改变门槛方向**的一类缺陷（CA-1b 比值 1.22 → 2.53）。
3. **预注册的价值在"门槛方向"上体现**：两个阶段门槛判定逐项一致，说明"装置错误"与"结论错误"可以分开；如果判定翻了，你会无法判断是装置错了还是结论错了。
4. **编译产物是记忆泄漏通道**：`.pyc` 里留着修复后代码的字符串常量。任何"复位到 fixture"的流程都必须 `git clean -fdx`，而不只是 `git checkout -- .`。

---

## 6. 局限

1. **Phase-1 不含压缩专项**（CA-6–CA-9）：压缩触发机制尚未做到可复现，强行执行会得到"压缩没影响记忆"的假结论。
2. **Phase-1 不含复发率与 L1 采纳**（CA-2/CA-3）：需要 T1/T2 场景与额外标注口径。
3. **A2 不是逐字节相同的被测系统**：它用了一份打了补丁的 tmem 副本（只关注入）。差异只有一行并已留档，但仍需在解读时记住。
4. **散文载体是机械渲染的**：其表达密度必然低于 tmem 的结构化字段——这本身就是"载体"差异的一部分，不是渲染缺陷；但它意味着本课题测的是**"机械渲染的散文"**，不等于人写的散文。
5. **模型非确定性**：每臂仅 3 次重复，命中率类指标的置信区间很宽（`free` 变体的 CI 极宽）；`zero` 变体因为无检索、无随机源，命中率方差为 0，但那也是"最理想情况"的产物。
6. **提示词措辞与上一轮不同**（改为载体中立措辞），跨轮次命中率不可直接比较，轮次内比较有效。
7. **Phase-1b 与 Phase-1 的运行环境不同**：Phase-1b 在容器里跑（ubuntu:22.04 + git/python3/pytest），Phase-1 在宿主上跑。两阶段判定一致，但这不等于环境无关性的证明。
8. **装置在执行中修过两次**（均有留痕，且只动装置不动阈值）：(a) 注入器节奏（v1.2 冻结前修）；(b) 注入器把 DSH 的 `<system-reminder>` 当成用户轮（Phase-1b 执行中修，**B 臂 6 场全部重跑**，A/A2/B2/C 臂不受影响）。因此"B1 臂的 6 场"是修复后统一重跑的，"A 臂的 12 场"是在该修复之前跑的——两者互不影响，因为该修复只触及 B1 使用的散文注入器。
9. **C 臂 `free` 变体只有 2 个有效样本**：第 1 次 900 秒超时（无记忆的 agent 长时间翻源码，transcript 达 694 KB）；重跑跑完，但该场 agent 枚举了容器内的 `/home/kent` 与 `/archive`，按**冻结的 A7 规则**判为越界 → 作废。我们没有为此追加事后豁免，也没有再跑第三次（避免按结果挑样本）。C 臂 `free` 的数字因此只作描述性参考；**CA-E1 只判 `zero` 变体**（n=3 全有效），门槛结论不受影响。
10. **成本口径的争议**：本课题沿用上一轮的"注入块 + 全部工具输出"口径；§5.1 论证了这个口径把解题成本算进来。若产品关心的是"上下文占用"，应以 `injected_tokens` 为准，而不是本报告的主指标。这不是改门槛，而是两个不同的问题。
11. **探针只有 3 个关键点、每题答案空间较窄**（文件名顺序、一个命令片段、一个函数名），统计功效有限。

---

## 7. 复现清单

### 7.1 Phase-1b（当前报告的数据来源）

```bash
# 1) 预注册（v1.2：阈值与提示词与 v1.1 逐字相同）
cat /home/kent/e2e/_harness/ablation/oracle_carrier_ablation_v1.2.json

# 2) 装置自检（零模型成本）
node /home/kent/e2e/_harness/ablation/md_carrier_plugin.test.mjs      # 9/9

# 3) 隔离镜像
docker build -f /home/kent/e2e/_harness/ablation/Dockerfile.abl -t tmem-abl:iso /home/kent/e2e/_harness/ablation

# 4) 建臂（工作区卫生 + 关键点扫描）
python3 /home/kent/e2e/_harness/ablation/run_phase1b.py prepare --exp-root /home/kent/abl2 --reps 3

# 5) 容器内隔离探针（敏感路径必须不存在）
python3 /home/kent/e2e/_harness/ablation/run_phase1b.py smoke --exp-root /home/kent/abl2

# 6) 五臂挂载自检（5 场极短会话）
python3 /home/kent/e2e/_harness/ablation/run_phase1b.py mountcheck --exp-root /home/kent/abl2

# 7) 正式执行（30 场，每场一个独立容器）
python3 /home/kent/e2e/_harness/ablation/run_phase1b.py run --exp-root /home/kent/abl2 \
  --arms A1,A2,B1,B2,C --variants free,zero --reps 3 --resume

# 8) 机器判定（含越界读取自动作废）
python3 /home/kent/e2e/_harness/ablation/score_carrier_ablation.py --exp-root /home/kent/abl2 \
  --oracle /home/kent/e2e/_harness/ablation/oracle_carrier_ablation_v1.2.json \
  --out-json /home/kent/e2e/_harness/abl-out/metrics-carrier-ablation-v1.2.json \
  --out-md   /home/kent/e2e/_harness/abl-out/metrics-carrier-ablation-v1.2.md
```

### 7.1.1 第三方复核路径（只用仓库内文件即可复算）

仓库随附 `research/carrier-ablation/results/`：Phase-1b 与 Phase-1 的机器判定、容器隔离探针、五臂挂载自检、**30 场会话的最终回答文本**。复核者无需重跑模型即可复算已发布结论：

1. 读 `research/carrier-ablation/oracle_carrier_ablation_v1.2.json` 的 `keypoints` 与 `gates`（阈值）；
2. 对 `results/sessions/<臂>_rep<n>_<变体>.md` 用同一套关键点做命中判定（Q1 还要求顺序正确）；
3. 与 `results/metrics-carrier-ablation-v1.2.json` 的 `gates` 对照。

**本报告已跑过这条复核**：30 场逐题命中位与已发布结果**逐一致**，四个门槛判定（CA-1 / CA-1b / CA-4 / CA-E1）**完全一致**。

报告里的每个数字还由 `research/carrier-ablation/verify_report_numbers.py` 自动校验：它把报告中印出的逐臂均值、门槛比值、描述性比值、有效样本数与越界次数**从机器判定里重新推导一遍**并断言逐字出现，同时断言 v1.2 相对 v1.1 在 `prompts`/`keypoints`/`metrics`/`gates` 上**无阈值漂移**。改动报告后请重跑：

```bash
python3 research/carrier-ablation/verify_report_numbers.py     # 期望 PASS；篡改任一数字会 FAIL
```

### 7.2 Phase-1（已隔离存，仅供对照）

```bash
# 1) 预注册（v1.1 原文保留）
cat /home/kent/e2e/_harness/ablation/oracle_carrier_ablation.json

# 2) 装置自检（零模型成本）
node /home/kent/e2e/_harness/ablation/md_carrier_plugin.test.mjs

# 3) 建臂（播种之后）
python3 /home/kent/e2e/_harness/ablation/build_arms.py arms --exp-root /home/kent/e2e/abl --reps 3

# 4) 挂载自检（5 场极短会话）
python3 /home/kent/e2e/_harness/ablation/mount_check.py --exp-root /home/kent/e2e/abl

# 5) 正式执行（30 场）
python3 /home/kent/e2e/_harness/ablation/run_ablation.py --exp-root /home/kent/e2e/abl \
  --arms A1,A2,B1,B2,C --variants free,zero --reps 3

# 6) 机器判定
python3 /home/kent/e2e/_harness/ablation/score_carrier_ablation.py --exp-root /home/kent/e2e/abl
```
