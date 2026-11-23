# 研究报告：记忆载体消融与量化——tmem 结构化记忆 vs 散文式 MD 记忆 vs 无记忆

> **研究类型**：受控消融实验（因子设计 + 内容等价控制 + 预注册门槛 + 重复采样 + 机器可读判定）
> **协议**：[`docs/dsh-memory-carrier-ablation.md`](./dsh-memory-carrier-ablation.md)
> **预注册**：`/home/kent/e2e/_harness/ablation/oracle_carrier_ablation.json`（v1.1，冻结于 2026-11-23T22:53:27+08:00，修订于 23:00:00，均在第一次模型调用之前）
> **执行装置**：`/home/kent/e2e/_harness/ablation/` ｜ **判定产物**：`metrics-carrier-ablation.json` / `.md`

---

## 0. 一句话结论

<!-- 结果段落后填 -->

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

| 阶段 | 时间 | 结果 |
| :--- | :--- | :--- |
| 预注册冻结（v1.0） | 2026-11-23 22:53 | 提示词、关键点、阈值、作废规则写入 oracle |
| 预注册修订（v1.1） | 2026-11-23 23:00 | 三处修订（关键点对齐本课题播种事实、CA-E1 改判 zero 变体、工作区复位口径），**均在第一次模型调用之前**，v1.0 原文保留 |
| 播种会话（S1+S2） | 2026-11-23 22:55–23:00 | 产出 4 个 episode / 3 个 trace；agent 自写笔记随后清除 |
| 散文载体渲染 | 2026-11-23 23:02 | 4 个 episode → 6 283 字节 `MEMORY.md`；**CA-5 等价性检查通过** |
| 建臂（5 × 3） | 2026-11-23 23:03 | 15 个工作区副本 + 2 个隔离 home + A2 补丁副本 |
| 挂载自检 | 2026-11-23 23:09 | **5/5 通过**：A1/A2 有 tmem 工具无注入，B1 有散文注入无 tmem 工具，B2/C 全清 |
| 正式执行 | 2026-11-23 23:1x | 30 场探针会话 |

---

## 4. 结果

<!-- 结果段落 -->

---

## 5. 讨论

<!-- 讨论段落 -->

---

## 6. 局限

1. **Phase-1 不含压缩专项**（CA-6–CA-9）：压缩触发机制尚未做到可复现，强行执行会得到"压缩没影响记忆"的假结论。
2. **Phase-1 不含复发率与 L1 采纳**（CA-2/CA-3）：需要 T1/T2 场景与额外标注口径。
3. **A2 不是逐字节相同的被测系统**：它用了一份打了补丁的 tmem 副本（只关注入）。差异只有一行并已留档，但仍需在解读时记住。
4. **散文载体是机械渲染的**：其表达密度必然低于 tmem 的结构化字段——这本身就是"载体"差异的一部分，不是渲染缺陷；但它意味着本课题测的是**"机械渲染的散文"**，不等于人写的散文。
5. **模型非确定性**：每臂仅 3 次重复，命中率类指标的置信区间很宽。
6. **提示词措辞与上一轮不同**（改为载体中立措辞），跨轮次命中率不可直接比较，轮次内比较有效。

---

## 7. 复现清单

```bash
# 1) 预注册（阈值与提示词）
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
