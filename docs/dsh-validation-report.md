# tmem × DSH 验证报告

> **战役**:`tmem` 在 DeepSeek Harness 上的可用性验证(调度者:mcode)
> **被测对象**:`/archive/tmem`(`dsh-tmem@0.4.0` → **`0.4.1`**,Node 纯 ESM)
> **基准环境**:`node v24.19.0` · `@deepseek-ai/dsh@0.2.2-rc.5` · `@deepseek-ai/cordis@4.1.0`
> **计划文档**:[`docs/dsh-validation-plan.md`](./dsh-validation-plan.md)
> **日期**:2026-11-20 · **范围**:只验证、出报告(**不改任何代码**)
>
> 📌 **2026-11-21 更新**:远端仓库发布了 `0.4.1`,本仓库已同步并复验 —— 见 **[§10 0.4.1 同步复验](#10-041-同步复验)**。
> 结论速览:**3 处 P0 修好 2 处、1 处只修了一半**;并**新发现 1 个 P0 级缺陷**(zstd 多帧解压)。
> **npm 上的 `latest` 仍是 `0.4.0`** —— 装了 npm 包的用户拿到的仍是有缺陷的版本。

---

## 0. 执行摘要

### 0.1 一句话结论

**tmem 的记忆引擎本身是好的,断点只有一处** —— DSH 集成层的 transcript 自动发现从未在任何真实 DSH 安装上成功过,
导致 live 路径下 `tmem capture` 恒返回 `no_transcript_found`。**这就是本仓库至今零真实 trace 的系统性根因。**

一旦绕开这一处(显式 `--transcript`),完整闭环当场成立:
`capture → slice → sync(重生 MEMORIES.md,含 L0 playbook 与 L2 因果修复)→ lookup 命中 → pre_step 召回注入`。

> **只关心"哪些是 tmem 自己的问题"** → 直接看 **[§8 排除版本因素后的结论](#8-排除版本因素后的结论)**。
> 那一节把 DSH 版本造成的 3 条噪声剥离掉,并给出**排除版本后重新排序的优先级**。

### 0.2 五件必须先知道的事

| # | 结论 | 影响 |
| :--- | :--- | :--- |
| 1 | **DSH 必须升到 0.2.2-rc.5** | 原装的 0.2.0-rc.2 不含任何 `dsh-tool-executor*` 包,仓库里唯一的"真·DSH 集成测试"**在本机从未真正跑过**(硬失败)。升级后它跑起来并通过。同时 cordis 从 4.0.4 → 4.1.0,**正好对齐 tmem 自己声明的 peerDependency**。 |
| 2 | **`tmem/adapters/dsh.mjs:207` 只匹配 `*.jsonl`,而 DSH 写 `*.jsonl.zstd`** | `tmem capture --agent dsh` 恒返回 `{"status":"skipped","reason":"no_transcript_found"}`。**这是长期系统性缺陷**:DSH 至少从 0.1.7-rc.2 起就用 zstd;Python 版 adapter 同样没有 zstd 处理(0 处)→ **跨 DSH 版本、跨 Python→Node 移植都没修过**。 |
| 3 | **`tests/dsh_e2e.test.mjs:360-361` 是恒真断言** | 路径损坏成 `.dsh/.dsh/.dsh` 时**依然为绿**。仓库里唯一用真实 dsh 加载 workspace patch 的测试,**恰恰发现不了缺陷一**。调度者已独立复核确认。 |
| 4 | **本次在途修复的修复对象,在默认启动路径下从不生效** | dsh 的 patch 层只有 `bundle → profile → home → --patch`;**工作区 `.dsh/tmem.patch.yml` 不会被自动发现**。默认路径下 tmem 靠 home 层绝对路径生效。修复本身正确,但收益被高估。 |
| 5 | ** Milestone 5 延迟门是负载敏感的 flaky 门,不是实现回退** | 机器空闲时 `npm test` 连续 3 次 **50 pass / 0 fail**;并发负载下越界。CI 在 GitHub Actions 共享 runner 上有真实的随机变红风险。 |

### 0.3 逐轨道裁决

| 轨道 | 主题 | 裁决 | 发现数 |
| :--- | :--- | :--- | :--- |
| **A** | 安装与激活回归 | `PASS`（附重要保留） | 见 §3.1 |
| **B** | 真实 DSH 会话 E2E | `FAIL(code)` | 7 |
| **C** | 钩子与生命周期闭环 | `FAIL(code)`（核心闭环 2/6/7/8 全绿） | 4 |
| **D1** | 基准门与报告一致性 | `PASS` | 4 |
| **D2** | 延迟断言稳健性 | `FLAKY` | 8 |
| **E** | 未提交改动审查 | `FAIL(code)` | 15 |
| **F1** | 记忆闭环纵向验证 | `PASS` | 10 |

### 0.4 L1/L2/L3 三层验收

| 层 | 目标 | 裁决 | 依据 |
| :--- | :--- | :--- | :--- |
| **L1** 安装与激活 | 插件在全新工作区与全局 home 层下正确解析、导入、激活;CLI 在 symlink 下可用;失败不得静默 | **PASS（带保留）** | 三层 patch 语义解析全部 exists=true;反向对照实证 `.dsh/.dsh` 双拼可复现;`tmem --version` 在 symlink 下正常。但工作区 patch 不被自动加载(§0.2 #4) |
| **L2** 运行时行为 | 真实会话 → 工具进 schema → 产出 trace → MEMORIES.md 再生 → 下轮召回命中 | **FAIL(code)** | 工具确实进了 tool schema(`tmem_search`/`tmem_expand` 各 2 次)、召回 3/3 命中、milestone 提示触发;**但自动捕获环节断链**(§0.2 #2),`MEMORIES.md` §1 仍为空 |
| **L3** 质量与性能 | 基准门全绿、README 同步;延迟断言稳定;2 条已知失败完成定性 | **部分通过** | `verify_benchmarks` PASS、6 个评测全部 exit 0;但 README 两处延迟宣称被实测证伪,且延迟门在 CI 形状下 **9/10 红**(§3.6) |

### 0.5 📌 0.4.1 同步复验速览(2026-11-21)

远端仓库发布了 `0.4.1`(3 个提交),已 fast-forward 同步并逐条复验。**原 P0 三条的状态变化**:

| 原缺陷 | 0.4.1 是否修复 | 复验结论 |
| :--- | :--- | :--- |
| **P0-1** e2e 恒真断言 | ✅ **真修好** | 新断言改为比对 `file://` 绝对 URL。损坏路径下旧断言仍 `true`、新断言 `false` → 断言**首次具备区分能力** |
| **P0-2** zstd trace 断链 | ⚠️ **只修了一半** | 能发现 `.zstd` 了,但**只解出第一帧** → 真实 DSH 数据的 actions 仍为 0。**新发现 P0-4**(见 §10.3) |
| **P0-3** CI 延迟门 | ❌ **未修复** | 10 次完整 `npm test`:**9/10 仍红**在 `avg_expand_latency_ms < 25` |
| **共变引擎 0 对** | ✅ **真修好** | `/archive/tmem` 上 **2 对 → 29 对**,置信度全部 ≥0.5 |
| **P1-4** 工作区 patch 不被自动加载 | ❌ 未变 | 属 DSH patch 层语义(无工作区层),**不是版本问题、也不是 bug 可修** |

> **一句话**:**0.4.1 修好了"看不见文件"和"测试假绿",但没修好"看不懂文件",而 CI 那道红门原封不动。**

---

## 1. 环境变更记录

### 1.1 DSH 升级

| 项 | 升级前 | 升级后 |
| :--- | :--- | :--- |
| `@deepseek-ai/dsh` | `0.2.0-rc.2` | `0.2.2-rc.5`(= npm `latest`) |
| `@deepseek-ai/cordis` | `~4.0.4` | `~4.1.0` ← **对齐 tmem 的 peerDependency** |
| `dsh-tool-executor-native` | **不存在** | `0.2.2-rc.5`,可解析并导出 `NativeToolExecutor` |
| node engines 要求 | — | `>=24.9.0`(本机 v24.19.0 ✓) |

**依据**:`dsh-tool-executor-native` 在 npm 上的最早版本是 `0.2.2-alpha.2`;0.2.0/0.2.1 系列根本没有这个包。
故原装的 0.2.0-rc.2 **相对本仓库自身声明是过时的**,升级属**修正环境错配**。

### 1.2 升级副作用:headless 的凭据行为改变(**不影响 web**)

| Profile | `credentials` 插件 | 后果 |
| :--- | :--- | :--- |
| **web** | **启用** | ✅ **用户的日常驱动不受影响**,`~/.dsh/.credentials.yaml` 照常生效 |
| **headless** | **被禁用** | 0.2.2-rc.5 的 `dsh-headless/cordis.patch.yml:38-40` 明写:*"One-shot jobs use launch-time credentials instead of the interactive store."* → headless 启动即 `MISSING_CREDENTIAL: llm-pi-ai … "minimax-cn" … MINIMAX_CN_API_KEY` |

**A/B 实测对照**(同一凭据文件、同一 home patch):

```
旧版 0.2.0-rc.2(headless,DSH_HOME 指向副本)→ OK,exit 0     ← 旧版 headless 会读交互式凭据库
新版 0.2.2-rc.5(headless,默认)              → MISSING_CREDENTIAL
新版 0.2.2-rc.5(headless,导出 env)          → OK,exit 0
新版 0.2.2-rc.5(web)                        → credentials 启用,不受影响
```

> **这是新 DSH 的有意设计,不是 bug**。headless 下要用 key,须走**启动环境变量**或 `--patch` 覆盖层把 `credentials` 重新启用。
> 战役中所有 headless live 会话均采用后者(scratch 覆盖层),**未改动真实 `~/.dsh/cordis.patch.yml`**。

### 1.3 回滚锚点

| 项 | 回滚方式 |
| :--- | :--- |
| dsh 版本 | `npm i -g @deepseek-ai/dsh@0.2.0-rc.2` |
| home patch | `cp /tmp/dsh-home-cordis.patch.yml.bak ~/.dsh/cordis.patch.yml` |
| profiles | `tar -xzf /tmp/dsh-profiles-snapshot.tar.gz -C ~` |
| 全局包快照 | `/tmp/dsh-global-rollback-snapshot.txt` |

### 1.4 仓库状态

全部轨道结束后 `/archive/tmem` 的 `git status --porcelain` 与战役开始时**逐字节一致**
(4 个 `M` + `docs/` 下 2 个 `??`)。**未改动任何仓库文件,未 commit / stash / checkout。**

---

## 2. 验证矩阵

| # | 判据 | 层 | 轨道 | 裁决 | 关键证据 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 生成的 workspace patch 语义解析指向存在的文件 | L1 | A | ✅ | `/tmp/tmem-validation/A/03_semantic_resolve.txt` |
| 2 | 仓库自带 patch 语义解析指向存在的文件 | L1 | A | ✅ | 同上(STEP 4) |
| 3 | home 层条目为绝对路径且存在 | L1 | A | ✅ | 同上(STEP 5,YAML 权威解析) |
| 4 | **反向对照**:旧写法能复现 `.dsh/.dsh` 双拼 | L1 | A | ✅ | `A/08_reverse_control.txt` |
| 5 | tmem 不在 "did not activate" 列表 | L1 | A/B | ✅ | `B/did_not_activate.txt`(0 命中) |
| 6 | `tmem --version` 在 symlink 下正常 | L1 | A/E | ✅ | `tmem 0.4.0`,exit 0 |
| 7 | `resolveRealPath` 异常路径不抛未捕获异常 | L1 | E | ✅ | `E/rp_err.mjs`:ELOOP/EACCES/ENOENT 全部 fellBack |
| 8 | `tmem_search`/`tmem_expand` 进入 agent tool schema | L2 | B | ✅ | transcript `request/header`(seq 11)工具名集合含两者,各计数 2 |
| 9 | live 会话产出 trace | L2 | B | ❌ | 自动捕获 `no_transcript_found`;显式 `--transcript` 才 compiled |
| 10 | live 会话产出 episode | L2 | B | ✅ | `ep_47c5de7816` |
| 11 | `MEMORIES.md` 由空变为含条目 | L2 | B/C/F1 | ⚠️ | 绕开断链后成立(C:F1a 产出 L0_FULL + L2 行);live 默认路径不成立 |
| 12 | 遥测出现非零值 | L2 | B/C/F1 | ⚠️ | 单文件/零报错/非 git 场景 → L1/L2 事件**在设计上不可能产生** |
| 13 | 召回闭环命中本次 trace | L2 | B/C/F1 | ✅ | B 3/3 命中;C 命中 `tr_610c34f95f`;F1 命中 `tr_cfc3fea70b` |
| 14 | milestone 提示触发 | L2 | B | ✅ | agent 输出含 `💡 是否需要将本次有效执行路径沉淀为记忆？` |
| 15 | 斜杠命令 `/tmem status` 闭环 | L2 | C | ✅ | `{"kind":"success","text":"[tmem status]…"}` |
| 16 | `pre_step` 召回注入 | L2 | C | ✅ | 捕获后返回 `<tmem_speculative_memory>…Trace tr_610c34f95f…` |
| 17 | `tmem sync` 重生 MEMORIES.md | L2 | C | ✅ | 新增 L0_FULL playbook 行 + L2 因果修复行,`gc_stats.active_l0=1` |
| 18 | `inspect` 四级渐进披露 | L2 | F1 | ✅ | index / playbook / raw / diff 均有内容 |
| 19 | `replay` 终端回放放出关键路径 | L2 | F1 | ✅ | READ → MUTATE → ENV_EXEC(npm test 2>&1) |
| 20 | 基准门 `verify_benchmarks` | L3 | D1 | ✅ | exit 0,零文件写入 |
| 21 | 语法门 / CLI 入口门 / evaluate | L3 | D1 | ✅ | 三项 exit 0 |
| 22 | 6 个评测入口全部可跑 | L3 | D1 | ✅ | 全部 exit 0;`eval_llm_judge` 无凭据时优雅离线降级 |
| 23 | 已存报告与 live 复算一致 | L3 | D1 | ✅ | 4 份报告 sha256 前后一致,关键指标逐项吻合 |
| 24 | README 宣称与 live 实测一致 | L3 | D1 | ❌ | 2 处 × 2 语言被实测证伪(§3.2) |
| 25 | 延迟断言在串行/并行下均稳定 | L3 | D2 | ❌ | 4 vCPU(CI 形状)`npm test` **9/10 红**;24 核空闲 3/3 绿、2 vCPU 3/3 绿 |
| 26 | 未提交改动经得起审查 | L3 | E | ❌ | 15 条发现,P0 恒真断言(§3.5) |
| 27 | `inspect` 四级渐进披露 | L2 | F1 | ✅ | index / playbook / raw / diff 全部 exit 0 且有内容 |
| 28 | `replay` 放出关键路径而非噪声 | L2 | F1 | ✅ | 10 步 → **3 步关键路径**(READ → MUTATE → ENV_EXEC),7 步被判噪声剪除 |
| 29 | L1 共变提示能浮现并被采纳 | L2 | F1 | ✅ | 第 2 场 live 会话:`l1_hints 2 / l1_adopted 2 / l1_adoption_rate 1`,agent 推理中直接引用注入的提示 |
| 30 | MEMORIES.md 三节同时填充 | L2 | F1 | ✅ | §1 `tr_a2444cd58d` L0_FULL 70% · §2 1 条 L2 因果修复 · §3 两条共变对 |
| 31 | 共变引擎能看见仓库真实信号 | L3 | F1 | ❌ | 引擎返回 **0 对**,而 git 历史真实信号有 20x/13x/11x 多条(§5) |

---

## 3. 逐轨道裁决

### 3.1 Track A — 安装与激活回归

**裁决:`PASS`(带重要保留)**,证据目录 `/tmp/tmem-validation/A/`

| 检查 | 结果 |
| :--- | :--- |
| `tmem init` 在 scratch 工作区的产物 | `.dsh/plugins/tmem-dsh-plugin.mjs` · `.dsh/tmem.patch.yml` · `.dsh/skills/tmem/SKILL.md` · `AGENTS.md` · `MEM.md`/`MEMORIES.md` · `.tmem/{cache,episodes,raw,traces}` · git hook;home patch 写入 **scratch** home,真实 home 未动 |
| 三层 patch 语义解析(YAML 权威解析,非字符串匹配) | 全部 `exists=true`,**无 `.dsh/.dsh` 双拼** |
| **反向对照** | 改回旧写法后,真实 `dsh --dump-config --patch` 输出 `file:///…/ws/.dsh/.dsh/plugins/tmem-dsh-plugin.mjs` → **双拼实证可复现**,证明修复点确实被测到、不是恒真断言 |
| `tmem --version`(PATH symlink) | `tmem 0.4.0`,exit 0 |
| "did not activate" | tmem 不在其中 |

**保留意见**:该轨道同时实测到 —— **不带 `--patch` 时,workspace 层 patch 完全不出现在 dump-config 里**,
dsh 的层只有 `bundle → profile → home → --patch`。这与 Track E 的结论独立吻合(见 §3.5 P1-2)。

### 3.2 Track D1 — 基准门与报告一致性

**裁决:`PASS`**,证据目录 `/tmp/tmem-validation/D1/`

`verify_benchmarks` / `npm run check` / `cli evaluate` / `cli --help` / `cli --version` 全部 exit 0 且零文件写入。
6 个评测入口全部 exit 0;`eval_plugin_scale` 会改写 `benchmarks/runs/` 的 3 个 timing 字段(已即时还原,sha256 前后一致)。

**F1 [MEDIUM] README 延迟宣称被实测证伪**
`README.md:127` / `README_EN.md:127`:"搜一次索引 **~6 ms**,展开一层细节 **< 10 ms**"。
连测 4 次:search = 9.23 / 12.76 / 12.97 / 9.37 ms,expand = 17.65 / 16.30 / 18.04 / 16.71 ms → **4/4 样本证伪 `< 10 ms`**。

**F2 [MEDIUM] README 均值取错**
`README.md:159` / `README_EN.md:159`:"Mean Deterministic Slicing Latency: **0.88 ms**"。
live `tmem/cli.mjs evaluate` 连测 4 次 = 3.2 / 3.39 / 2.93 / 2.99 ms(**3.4×–3.9×**)。
0.88 ms 恰是**单条最小值**,不是均值。

> **门禁缺口**:`verify_benchmarks.mjs:168-170` 只断言 SLA < 50 ms,`checkReadmeSync` 从不比对这两个数字 ——
> 尽管该脚本自己的 docstring(`:8`)承诺 *"README sync: benchmark tables **and figures** … match live runs"*。

**F3 [LOW]** `benchmarks/runs/plugin_scale_50_report.json` 非字节可复现(timing 漂移:`avg_slice_latency_ms` 0.46→1.37)。
**门禁是对的**(用 `TIMING_KEYS` 剥离后才比对),脏树的是入口脚本。

**F4 [LOW]** plugin-50 `l1_cases_tested 9 / l1_intercepted 0 / l1_interception_rate_pct 0`,telemetry `l1_adoption_rate: null`
—— **L1 共变拦截率为 0% 却静默通过**,因为门禁只断言 L2 拦截(`verify_benchmarks.mjs:138`)。
DSH E2E 侧却报 L1 1/1 —— 这是**覆盖不对称**,不是矛盾。

### 3.3 Track B — 真实 DSH 会话 E2E

**裁决:`FAIL(code)`,7 条发现**,证据目录 `/tmp/tmem-validation/B/`

**唯一一场真实 agent 会话**:agent 修好了 `src/cache.js` 的缓存 key 时间戳 bug,`npm test` 从 `pass 2/fail 1` 变为 **3/3 全过**。

| 判据 | 结果 |
| :--- | :--- |
| 工具进 tool schema | ✅ `tmem_search` = 2、`tmem_expand` = 2,出现在 transcript `request/header`(seq 11)的 25 个工具名中 |
| "did not activate" | ✅ 0 命中;`.tmem/runtime_state.json` 记录了 `uncommitted_mutations:["src/cache.js"]`、`verified_since_mutation:true`、`milestone_prompted_for_mutations`、`micro_skills_injected:true` |
| milestone 提示 | ✅ agent 最终输出含 `💡 是否需要将本次有效执行路径沉淀为记忆?` |
| 召回闭环 | ✅ 3/3 查询命中 `tr_82c09d9112`,携带 session 血缘与 "Verified L0 Critical Path (Guards Passed): READ → MUTATE → ENV_EXEC(npm test 2>&1)" |
| 自动捕获 | ❌ `{"status":"skipped","reason":"no_transcript_found"}` |
| 显式 `--transcript` | ✅ `skipped_trivial`(default-deny 门,单文件无报错)→ `--force` → `compiled`,`tr_82c09d9112`,10 raw steps → 3 sliced |
| `MEMORIES.md` | ⚠️ 被 `sync` 重写并新增 `## 0. Verified Env & Test Commands`,但**第 1 节仍为空** —— `mem_doc.mjs:171-173` 过滤 `!pb.is_trivial` |
| 遥测 | ⚠️ 全 0,但已定性为 **"没发生"而非"检测不到"**:`l1_cochange_hinted` 需多文件共变伙伴,`l2_injected` 需命中已知 error signature,本场景(单文件/零报错/非 git)**在设计上不可能触发** |

> **透明化**:共发起 2 次 dsh 进程。第 1 次在凭据门禁处启动即中止(**未发生任何模型调用**),按任务预案改用 `--patch` 覆盖层重试;第 2 次为唯一一场真实 agent 会话。

### 3.4 Track C — 钩子与生命周期闭环

**裁决:`FAIL(code)`,4 条发现**,证据目录 `/tmp/tmem-validation/C/`

**核心闭环成立**:斜杠命令 → `capture` → `sync`(MEMORIES.md 重生出 L0_FULL playbook 行 **与** L2 因果修复行:
`Error: TypeError: retryBudget is not a function` ⇒ `edit src/worker.js (retryBudget)`,验证命令 `npm test -- retry-budget`)→ `lookup` 命中。

**F1(阻断)重复 `tmem capture --force` 不幂等**
连续 3 次 → trace 文件数 `1 → 2 → 2`;第 2 次产出**退化 trace** `tr_bfa9653af0`(1 raw step、`touched_files=[]`、`is_trivial=true`、`problem_intent="Modify "`)。
根因:`tmem/cli.mjs:194-198` 把 `last_captured_step_idx` 设为**最后一个动作**的 seq(13,非 `turn/end` 的 15),
下次 capture 以 `minStep=13` 只切到尾部验证命令 1 个动作;而 `session.actions.length === 0 → 回到 0 重解析` 的兜底因"动作数=1"不触发,
于是 1 步平凡 playbook 被 `--force` 持久化。**HEAD 已存在**,与在途改动无关。

**F2 compaction 事件未接入 `tmem hook` CLI 契约**
`handleDshSessionEvent()` 只在进程内插件接线(`dsh_plugin.mjs:386-474`);`cli.mjs:417-428` 的 `hook` 只调 `handleDshHook()`,
后者不识别 `compaction_committed` / `compaction_prune` → CLI 投递恒返回 `{}`(**静默 no-op**)。
直接调 API 正常:`archived` / `pruned_archived` + episode 落盘 + 进倒排索引。

**F3 `tmem lookup --json` 静默失效**
`cli.mjs:455` 判的是 `flags.format === "json"`,而 `--json` 写入的是 `flags.json`(`:357`)→ 仍输出 markdown,须用 `--format json`。

**F4** compaction-only 会话 `promoted_playbook_id=null` → `lookup`(仅检索 playbook)空,而 `search`(倒排索引)能召回 episode。

### 3.5 Track E — 未提交改动审查

**裁决:`FAIL(code)`,15 条发现**,证据目录 `/tmp/tmem-validation/E/`

**代码本身无功能缺陷** —— `resolveRealPath` 与 installer 的路径修正均正确且安全。
问题在**修复对象在默认路径下从不生效**,以及**测试把 bug 固化成了恒真断言**。

**P0-1 恒真断言(调度者已独立复核)**
`tests/dsh_e2e.test.mjs:360-361`:
```js
assert.ok(dumpOut.includes("id: tmem"));
assert.ok(dumpOut.includes("tmem-dsh-plugin.mjs"));
```
调度者独立复现:喂给真实 dsh 一个**路径已损坏**的 patch(`./.dsh/plugins/…`),解析结果为
`file:///tmp/p0check/.dsh/.dsh/plugins/tmem-dsh-plugin.mjs`(**该文件不存在**),
而两个断言 **仍为 true**。→ 仓库里唯一用真实 dsh 加载 workspace patch 的测试,**无法发现缺陷一**;
即使把本次修复整个回退,该测试也不会变红。

**P1-2 修复对象在默认路径下从不生效**
dsh 的层序 = `bundle → profile → home → --patch`,**工作区 `.dsh/` 不被发现**(dsh 全部 lib 无任何 `.dsh` 工作区目录引用;
scratch `DSH_HOME` 下 `--dump-config` 的层清单里 `ws/.dsh/tmem.patch.yml` 出现次数 = **0**)。
缺陷一的真实受益者只有显式 `--patch` 的消费者(含 e2e 测试);
而 `installed.agents` 把 `.dsh/tmem.patch.yml` 列为"已安装集成",注释只讲基准不讲"不被加载" → **明确的假象**。

**P1-3 `tests/intent_graph.test.mjs:244-245` 仍是字面量断言,未按文档自身教训闭环**
排障文档 §4 自己写着:"installer 产物类测试应断言**语义**(解析后指向真实存在的文件),而非路径字面量" ——
而这条断言恰恰仍是字面量。风险:installer 若停止写 `<ws>/.dsh/plugins/tmem-dsh-plugin.mjs`,测试仍绿而 dsh import 失败(误绿);
installer 改成等价写法(如去掉 `./` 前缀)即误红。

**P1-4 排障文档 §6 两处均已过时**
① 凭据:现报 `MISSING_CREDENTIAL: llm-pi-ai … "minimax-cn" … MINIMAX_CN_API_KEY`,**不是**文档写的 `llm-deepseek`;
按文档建议导出 `DEEPSEEK_API_KEY` 无法修复 headless。
② 测试失败数:文档写 `npm test` 有 2 条失败,**现为 0 条**(机器安静时连跑 3 次均 50/50)。

**P1-5 排障文档 §5 诊断命令缺 `--patch`**
§0/§5 给的 `dsh --profile <p> --dump-config | grep -A2 tmem` **不含 `--patch`**,输出的是 home 层条目,
workspace patch 完全不在 dump 里 → **文档用来诊断缺陷一的命令,实际诊断的是 home 层**。

**P1-6 home 层可能把"工作区副本"登记为全局插件**
`installer.mjs:168`:`globalPluginPath = existsSync(pluginSrc) ? pluginSrc : dshPluginDst`,
而 `dshPluginDst` = `<ws>/.dsh/plugins/tmem-dsh-plugin.mjs`。回退分支下**全局** home 层会指向某个具体工作区,
该工作区被清理后所有 profile 的 tmem 都会 "did not activate"。

**P1-7 README 未记录全局写**
README 只说 "tmem init 在当前项目初始化",未提会写 `~/.dsh/cordis.patch.yml` → **作用域副作用对用户不可见**。

**P2 其余 8 条**:`--preserve-symlinks-main` 注释过度断言(真实 npm-link symlink 在模块加载阶段就 `ERR_MODULE_NOT_FOUND`,
到不了入口判定);`.dsh/tmem.patch.yml` 与 installer 产物**非字节一致**(差一行注释,下次 init 会抹掉);
`DSH_HOME` 空白值处理与 dsh 语义不一致(dsh 视为未设,installer 只认 env);幂等守卫是子串启发式(`includes("id: tmem")` 会命中 `id: tmem-foo`);
installer 内嵌 AGENTS.md 模板已漂移;文档标题"三个"与正文"缺陷一~四"口径不一致;文档 §7 把机器本地文件与"全局 npm"列为仓库变更;
`cli.mjs:688-694` 回退分支理论上可复现"静默不执行"(实测不可达,备查)。

### 3.6 Track D2 — 延迟断言稳健性

**裁决:`FLAKY`,8 条发现**,证据目录 `/tmp/tmem-validation/D2/`

测量前置条件:等机器真正空闲后开始 —— 23:58:58,load 1.85/2.57/2.71,24 vCPU,**0 个竞争中的 node/dsh 进程**;
每次运行前重读 load 并落盘(`evidence/*.jsonl`)。

#### 无实现回退 —— 明确结论:**NO**

| | search | expand |
| :--- | :--- | :--- |
| 工作区(在途改动) | mean **10.15** ms | mean **17.53** ms |
| HEAD 基线(`git archive`,只读) | mean 12.50 ms | mean **18.32** ms |

在工作途改动下反而**快 0.79 ms**。且在途改动的范围(`tmem/cli.mjs` symlink 入口、`tmem/installer.mjs`、`tests/intent_graph.test.mjs`)
与 `store.mjs` / `dsh_tools.mjs` / `episodes.mjs` / `inverted_index.mjs` / `guards.mjs` / `hooks/` **零重叠**。

#### 真正的触发条件:**并发跑测试文件**,不是 CPU 数

| 场景 | 结果 |
| :--- | :--- |
| 单文件,默认并发 ×3 | 3/3 通过 |
| 单文件,`--test-concurrency=1` ×3 | 3/3 通过 |
| 全量 `npm test`,24 核(11 文件并发)×3 | **3/3 通过**(2.17–2.36 s) |
| 全量 `npm test`,`--test-concurrency=1` ×3 | 3/3 通过(6.59–7.60 s) |
| 全量 `npm test` @ **2 vCPU** ×3 | **3/3 通过** |
| 全量 `npm test` @ **4 vCPU**(`taskset -c 0-3`)×10 | **9/10 失败** ❌ |
| 单文件**单独** @ 4 vCPU ×3 | 3/3 通过 |

`node --test` 的文件级并发度 = `os.availableParallelism()`(affinity 感知:24→4→2)。
对照实验证明:**触发因素是并发执行的测试文件数**,不是 CPU 数本身 ——
4 vCPU 是"并发文件够多、CPU 又不够分"的**最坏形状**。

#### 负载量化

| 条件 | expand 中位数 | expand 峰值 | 越 25ms |
| :--- | :--- | :--- | :--- |
| 24 核空闲 | 17.08 ms | 19.73 ms | 0/5 |
| 24 核 + 并发 `npm test` | 22.73 ms | **61.07 ms** | 2/6 |
| 4 vCPU + 并发 `npm test` | **32.67 ms** | **65.75 ms** | **6/8** |

**`search` 在全部 24+ 样本中从未越过 25 ms**(最大 15.21 ms)。

#### 机制(`node --cpu-prof`)

- `tmem/sqlite_compat.mjs:79-96` 的 `withDatabase`:**每次调用**都开连接 → `BEGIN`/`COMMIT` → 关闭 = **151.5 ms self-time**
- 倒排索引 token 展开 / role terms / postings ≈ 50 ms
- LRU-touch 重写 3.8–27.5 KB episode JSON 仅 ≈ 0.5 ms → **不是**主因

单次调用的 search calls 呈 `[~6.6, 19–42, ~3]` —— **第 2 次调用系统性慢 3–6 倍**(冷启动)。
进程内重复跑:search 从 12.17 ms(冷)→ 3.83–4.68 ms(热),**但 expand 在 4 vCPU 负载下中位数仍 31.7 ms,5/8 仍越界**
→ **预热 + 中位数能救 search,救不了 expand**。

#### README 差距(Step 4)

| 宣称 | 实测 | 判定 |
| :--- | :--- | :--- |
| `search ~6 ms` | 基准平均 8.86–10.68 ms(低 1.48–1.78×);但**单次调用中位数 6.63 ms** | 数字对应的是**单次调用中位数**,不是基准报告的平均值 —— 标签误导但可辩护 |
| `expand < 10 ms` | 基准平均 16.57–19.73 ms;**32 次单次调用中 0 次低于 10 ms**(最小 12.11);负载下最高 65.75 ms | ❌ **证伪**(1.7–6.6× 超标) |
| `Mean Slicing Latency 0.88 ms` | `tmem/cli.mjs evaluate` = 3.07/2.87/3.53/2.96(工作区)、3.14/2.92/3.10/3.06(HEAD),均值 **3.11 ms** | ❌ **误标**:0.88 是**单条轨迹最小值**(逐条值 13.73,3.45,2.09,2.11,0.99,**0.86**,1.48,1.23,1.07),CLI 的标签"平均/mean"与 README 的 "Mean" 都名不副实 |

> `scripts/verify_benchmarks.mjs` **没有任何 `requireMatch` 模式覆盖** "6 ms"/"10 ms"/"0.88 ms",
> 且 `TIMING_KEYS` 会剥掉 `avg_slice_latency_ms` → **门禁 PASS 而正文错误,这种漂移结构上无法被 CI 发现**。

#### CI 风险(Step 6)

- 本仓库为 **public**(`api.github.com/repos/kentzjh-cmd/tmem` → `"private": false`)→ `ubuntu-latest` = **4 vCPU / 16 GB**
- 4 vCPU 模拟:**9/10 失败** —— 正是 CI 的真实形状
- Node 22 + 24 双版本矩阵 → **P(至少一条腿红) ≈ 1 − 0.1² ≈ 99%**
- GitHub CI 磁盘是网络挂载的临时存储,fsync 延迟高于本地 NVMe → **真实失败率只会更高,不会更低**

> **诚实边界**:上述是本机 `taskset` 对 runner vCPU 形状的**本地模拟**,**未真正运行 GitHub Actions**;
> 本地模拟无法复现 GitHub 的网络存储延迟(那只会让失败率更高)。

**D2 建议**:把 `tests/compaction_inverted.test.mjs:806-807` 的绝对毫秒断言移出单元测试,
改为**独立的非阻断 perf job**(隔离运行、`--test-concurrency=1`、预热 + 中位数);
`search` 可保留一个宽松的 25 ms 上界(24 样本最大 15.21 ms),`expand` 则**不可能**在 4 vCPU CI 上做成绝对阈值。

### 3.7 Track F1 — 记忆闭环纵向验证

**裁决:`PASS`,10 条发现**,证据目录 `/tmp/tmem-validation/F1/`

全程在 **`git clone` 出来的仓库副本**里操作(42 commits 完整历史,0 次新提交),**`/archive/tmem` 127 个文件 sha256 逐一比对全部一致**。

#### F1-a 用真实 transcript 捕获

`tmem capture --agent dsh --transcript <Track B 的真实会话 transcript> --force` → `compiled`,`tr_cfc3fea70b`,10 raw steps → 3 sliced。
但 `sync` 后 **MEMORIES.md 三节全空**,两个独立原因(均已在代码中定位):

1. `is_trivial`(该会话是单文件、零报错)→ `mem_doc.mjs:171-173` 过滤
2. `status: stale` —— `file_exists` 守卫失败,因为 `normalizeRelPath`(`tmem/adapters/base.mjs`)
   对**落在捕获工作区之外**的路径**原样返回绝对路径** → 置信度 0.4 → 0.1 → 0.05 逐级衰减

其余:四级 `inspect` 全部 exit 0 且有内容;`replay` 把 10 步裁成 **3 步关键路径**;`lookup` 3/3 命中。

#### F1-b 第 2 场 live 会话 —— **L1 纵向学习闭环成立**

在 scratch 工作区里**播下真实的 git 共变基线**(5 次提交造出 `src/app.js ↔ test/app.test.js` 3x/100% 与
`tmem/cli.mjs ↔ tmem/installer.mjs` 2x/100%),再让 agent 去修同类缺陷:

```
tmem status --workspace $WS2
  project_traces: 1        project_episodes: 1
  telemetry: l1_hints 2 | l1_adopted 2 | l1_adopted_edit 2
             l1_inspected_only 0 | l1_ignored 0 | l1_adoption_rate 1
```

原始遥测事件逐条落地:

```json
{"event":"l1_cochange_hinted","edited_file":"src/app.js","suggested_files":["test/app.test.js"]}
{"event":"l1_outcome","edited_file":"src/app.js","adopted_files":["test/app.test.js"],"adopted":true,"resolution":"mutated"}
{"event":"l1_cochange_hinted","edited_file":"tmem/cli.mjs","suggested_files":["tmem/installer.mjs"]}
{"event":"l1_outcome","edited_file":"tmem/cli.mjs","adopted_files":["tmem/installer.mjs"],"adopted":true,"resolution":"mutated"}
```

**agent 自己的推理直接引用了注入的提示**:
> *"The co-change hint confirms src/app.js ↔ test/app.test.js. So I should update test/app.test.js as well."*
> *"The co-change hint confirms tmem/cli.mjs ↔ tmem/installer.mjs. So I should fix buildInstallCacheKey in installer.mjs too."*

工作树最终改了 **4 个文件** —— **两个共变伙伴都被真的改了**,提示不只是"显示"出来,而是**被采纳**。

会话中还出现了一个真实的环境缺陷并被 agent 诊断修复(Node v24 的 `node --test test/` 路径被当作 glob),
构成 **"观察到的失败 → 修复 → 验证"** 循环 —— 正是 L2 不变式抽取所需的输入(F1-a 的 trace 因无失败命令而 `l2_causal_repairs: []`)。
修复后:`tr_a2444cd58d`,`is_trivial: false`,confidence 0.7,28 → 11 steps,
`gc_stats: active_l0 1 / stale 0`,**MEMORIES.md 三节同时填充**(§1 L0_FULL 70%、§2 一条 L2 因果修复、§3 两条共变对),
`lookup` 按查询 4/4、按 `--file` 4/4 命中。

#### 阳性对照:L1 机制本身是好的

为了排除"代码有缺陷导致提示不触发",在合成仓库里做了阳性对照:

| 场景 | 结果 |
| :--- | :--- |
| clone 仓库(历史路径已不存在)里投递 `post_tool_use` | `{}`,`l1_hints 0` |
| 合成仓库(有 1 条真实共变对,4x/100%)里同样投递 | `{"additionalContext":"<tmem_cochange_hint>… src/a.js ↔ src/b.js (4x, 100%)</tmem_cochange_hint>"}`,`l1_hints 1`,MEMORIES.md §3 正常渲染 |

→ **L1 检测器功能正常**;clone 里的沉默是**数据属性**(历史路径已被 `.py→.mjs` 移植删除),不是代码缺陷。

#### F1 独立复现了 zstd 缺陷

F1-b 会话结束后 `$WS2/.tmem/` 只有 `runtime_state.json` 与 `telemetry.jsonl` —— **0 traces、0 episodes**。
原因与 Track B 完全一致:`session.v4.jsonl.zstd` vs `collectJsonlFiles` 只匹配 `*.jsonl`。
**这是该缺陷的第二次独立复现**(不同代理、不同会话、不同工作区)。

#### 纵向检验(两场会话放进同一项目)

clone 内放入 2 条 trace:`tr_1e358f4049` 🔵`L2_INVARIANT` conf 0.65 **active** + `tr_cfc3fea70b` `L1_TOPOLOGY` conf 0.05 stale。
§1 与 §2 解锁;**§3 仍只有 `Co-edited in session:` 回退,没有仓库级拓扑** ——
同一份代码在 WS2(历史路径仍在磁盘上)里能把 §1/§2/§3 全部填满。

**F1 建议的三处上游修复**:
1. `cochange.mjs` 跟随重命名(`git log --name-status -M` / `--follow`),而不是对历史路径做 `existsSync`;并且不要把文档文件排除在候选之外
2. 当捕获 transcript 的路径落在目标工作区之外时,**把 `touched_files` 相对化**,使守卫与 L2 修复动作不再泄漏绝对路径
3. DSH 会话结束捕获必须匹配 `*.jsonl.zstd`(或先解压再匹配)

---

## 4. 缺陷清单(按严重度汇总)

> 本节为**建议清单**;按 Q2 确认的范围,战役**未修改任何代码**。

### P0 —— 会导致错误结论

| # | 缺陷 | 位置 | 证据 | 建议动作 |
| :--- | :--- | :--- | :--- | :--- |
| P0-1 | e2e 测试恒真断言:路径损坏仍为绿 | `tests/dsh_e2e.test.mjs:360-361` | §3.5(调度者独立复核) | 断言解析后的 `file://` 绝对路径并校验 `existsSync`(或断言不含 `.dsh/.dsh`) |
| P0-2 | live 捕获恒返回 `no_transcript_found` —— **仓库零真实 trace 的根因** | `tmem/adapters/dsh.mjs:207` | §3.3 / §3.7(两次独立复现) | 支持 `session.v*.jsonl.zstd`(解压读取),或由插件把 live transcript 直接传给 capture |
| P0-3 | **Milestone 5 延迟门在 CI 形状下 9/10 红** —— public 仓库的 `ubuntu-latest` 就是 4 vCPU,双版本矩阵 P(至少一条腿红) ≈ 99% | `tests/compaction_inverted.test.mjs:806-807` | §3.6 | 把绝对毫秒断言移出单元测试,改为隔离的非阻断 perf job |

### P1 —— 风险

| # | 缺陷 | 位置 |
| :--- | :--- | :--- |
| P1-1 | 重复 `--force` capture 产出退化 trace(非幂等) | `tmem/cli.mjs:194-198` |
| P1-2 | compaction 事件未接入 `tmem hook` CLI(静默 no-op) | `tmem/cli.mjs:417-428` |
| P1-3 | `tmem lookup --json` 静默失效 | `tmem/cli.mjs:455` vs `:357` |
| P1-4 | workspace patch 不被自动加载,修复在默认路径无效 + 制造"已装好"假象 | `tmem/installer.mjs:157-166` |
| P1-5 | home 层可能登记"工作区副本"为全局插件 | `tmem/installer.mjs:168` |
| P1-6 | README 未记录 `tmem init` 会写全局 home 层 | `README.md:93` / `README_EN.md:93` |
| P1-7 | 排障文档 §6 凭据段与测试失败数均已过时 | `docs/plugin-install-troubleshooting.md:194-197` |
| P1-8 | 排障文档 §5 诊断命令缺 `--patch`,诊断不到目标层 | `docs/plugin-install-troubleshooting.md:13,182` |
| P1-9 | intent_graph 测试未按文档自身教训改为语义断言 | `tests/intent_graph.test.mjs:244-245` |
| P1-10 | Milestone 5 延迟门负载敏感(4 vCPU 下 9/10 红) | `tests/compaction_inverted.test.mjs:806-807` |
| P1-11 | **`withDatabase` 每次调用开/关 SQLite 连接 + BEGIN/COMMIT**(151.5 ms self-time,expand 的主因) | `tmem/sqlite_compat.mjs:79-96` |
| P1-12 | `normalizeRelPath` 对工作区外路径原样返回绝对路径 → `file_exists` 守卫失败、置信度逐级衰减 | `tmem/adapters/base.mjs` |
| P1-13 | 共变引擎不跟随 git 重命名,`.py→.mjs` 移植后的历史路径被 `existsSync` 判死 | `tmem/cochange.mjs:138` |
| P1-14 | `is_trivial` 的 trace 被完全隐藏而非降级展示 → MEMORIES.md §1 看不见真实工作 | `tmem/mem_doc.mjs:171-173` |

### P2 —— 文档与整洁度

| # | 缺陷 | 位置 |
| :--- | :--- | :--- |
| P2-1 | README 延迟宣称被实测证伪(2 处 × 2 语言) | `README.md:127,159` / `README_EN.md:127,159` |
| P2-2 | 门禁不比对 README 数字,承诺与实现不符 | `scripts/verify_benchmarks.mjs:8,168-170` |
| P2-3 | `--preserve-symlinks-main` 注释过度断言 | `docs/plugin-install-troubleshooting.md:115` |
| P2-4 | `.dsh/tmem.patch.yml` 与 installer 产物非字节一致 | `.dsh/tmem.patch.yml:2` vs `tmem/installer.mjs:161-165` |
| P2-5 | `DSH_HOME` 空白值处理与 dsh 语义不一致 | `tmem/installer.mjs:174` |
| P2-6 | 幂等守卫是子串启发式 | `tmem/installer.mjs:181-184` |
| P2-7 | installer 内嵌 AGENTS.md 模板已漂移 | `tmem/installer.mjs:216-234` |
| P2-8 | 排障文档标题"三个"vs 正文"缺陷一~四" | `docs/plugin-install-troubleshooting.md:1,157` |
| P2-9 | `plugin_scale_50_report.json` 非字节可复现(timing 漂移) | `benchmarks/eval_plugin_scale.mjs:304-311` |
| P2-10 | 门禁只断言 L2,L1 拦截率 0% 静默通过 | `scripts/verify_benchmarks.mjs:138` |

---

## 5. 根因分析:为什么本仓库至今零真实 trace

四道彼此独立的原因叠加,每一道单独就足以让真实会话产不出 trace:

```
真实 DSH 会话
   │
   ├─(1) DSH 写 session.v4.jsonl.zstd
   │        ↓
   │   adapters/dsh.mjs:207 只匹配 *.jsonl
   │        ↓  ★ 断链①:自动捕获恒 no_transcript_found
   │
   ├─(2) 即便显式喂 transcript:真实会话被判 is_trivial(单文件/零报错)
   │        ↓
   │   default-deny 门挡下 → skipped_trivial
   │        ↓  ★ 断链②:需 --force 才编译
   │
   ├─(3) 即便 --force 编译成功:is_trivial=true 的 trace 被过滤
   │        ↓
   │   mem_doc.mjs:171-173 过滤 !pb.is_trivial
   │        ↓  ★ 断链③:MEMORIES.md 第 1 节仍为空
   │
   └─(4) 即便有 trace:L1/L2 遥测需要多文件共变 / 已知错误签名
            ↓
            单文件、零报错、非 git 的会话在设计上不产生这些事件
            ↓  ★ 断链④:telemetry 恒 0
```

**旁证 —— 共变索引的信号被三个过滤器全部杀光**(Track F1 诊断):

| 过滤器 | 杀掉的真实信号 |
| :--- | :--- |
| `isDocFile` | `README.md ↔ README_EN.md` **20x**(最强信号) |
| `existsSync`(路径已不存在) | Python 时代全部共变(`tmem/slicer.py ↔ tmem/store.py` **13x** 等)—— `.py→.mjs` 移植后路径没了 |
| `maxFilesPerTx=10` | **42 个 commit 中的 17 个**被整批丢弃(近期大 commit 动辄 14–72 个文件) |

结果:**0 对存活**。反事实矩阵(放宽单个过滤器后的存活对数):

| 配置 | 存活对数 |
| :--- | :--- |
| as-shipped(doc + missing + bulk 三重过滤) | **0** |
| 仅放宽 `isDocFile` | 9 |
| 仅放宽 `missing_on_disk` | 2 |
| 仅放宽 `maxFilesPerTx` | 118 |
| 放宽 doc + missing | 13 |
| 放宽全部 | 1090 |

> 而仓库当前 `MEMORIES.md` §3 里那 2 条拓扑置信度只有 **0.400**,**恰好低于当前引擎 0.5 的门槛** ——
> 即:**已提交的索引内容是当前引擎根本不会产出的陈旧数据**,同时又漏掉了全部强信号。

### 5.1 但引擎本身是好的 —— 别把它当成坏掉的记忆系统

这是本次验证最容易被误读的一点,所以单独说明:

**F1 用阳性对照证明了 L1 共变学习机制完全正常。** 在一个播下真实 git 共变基线的仓库里,
同一条 hook 命令产出了正确的提示,并被 agent 采纳(`l1_adoption_rate = 1`)。
差别只在**数据**:
历史路径还在磁盘上 → 引擎看得见;历史路径已被 `.py→.mjs` 移植删除 → 引擎看不见。

同理,**MEMORIES.md 三节在具备条件时能同时被填满**(F1-b 实测:§1 L0_FULL 70% + §2 一条 L2 因果修复 + §3 两条共变对)。

> **一句话**:tmem 不是"记不住",而是"看不见可记的东西"。
> §5 列出的四道断链里,断链①(格式不匹配)与断链③(过滤策略)是**代码侧**的,
> 而共变索引为空则是**数据侧**的(历史被重命名/大 commit 被丢弃/文档对被排除)。

---

## 6. 建议的下一步(不在本次范围内执行)

**第一梯队 —— 不修就会继续误导人**

1. **先修 P0-2**(transcript 发现支持 zstd)与 **P0-1**(恒真断言)。
   只修断言不修实现,下次回归依然会误绿;只修实现不修断言,回归依然会被掩盖。**两者必须同时修。**
2. **修 P0-3**:把 `avg_expand_latency_ms < 25` 从单元测试里移出去,改成隔离的非阻断 perf job。
   现状是 public 仓库的 CI 有 ≈99% 概率红 —— 这是**每天都可能发生**的,不是"偶发 flake"。
3. **修 P1-11**(`withDatabase` 每次调用开/关 SQLite 连接):这是 expand 延迟的主因,
   修好之后才谈得上给 expand 设一个有意义的阈值。

**第二梯队 —— 让"已装好"变成真的**

4. **修 P1-4**:要么让 installer 明确声明"workspace patch 仅 `--patch` 生效",要么让 home 层成为唯一真源并在文档中说明。
   当前形态让用户以为"配好了",实际默认路径不受这次修复的任何保护。
5. **修 P1-13 / P1-12**:共变引擎跟随 git 重命名;`touched_files` 在跨工作区时相对化。
   这两条直接决定 §3 能不能在真实项目上填满。

**第三梯队 —— 质量与文档**

6. **决定 `is_trivial` 策略**(P1-14):是把"平凡"改为**降级展示**而非**隐藏**(MEMORIES.md §1 现在完全看不见真实工作),
   还是继续隐藏但补一条"本项目暂无有效 playbook"的显式提示。
7. **修 P1-1 的幂等语义**:`minStep` 应按会话边界推进,而非最后一个动作 seq。
8. **补齐 README 数字同步 + 门禁覆盖**(P2-1 / P2-2 / P2-10):并注意 D2 的发现 ——
   `search ~6 ms` 对应的是**单次调用中位数**而非基准平均值,`0.88 ms` 是**单条最小值**而非均值。
   修的时候要把标签一起改对,而不只是改数字。
9. **修 P1-2 / P1-3**:compaction 事件接入 `tmem hook` CLI;`lookup --json` 的 flag 判定。

**复测**

10. **修完 P0/P1 后,重跑本计划的 A / B / C / D1 / D2 / F1 六条轨道复测** ——
    本报告 §2 的验证矩阵可直接当验收清单用。

---

## 7. 证据索引

| 轨道 | 证据目录 | 关键产物 |
| :--- | :--- | :--- |
| A | `/tmp/tmem-validation/A/` | `03_semantic_resolve.txt`(YAML 权威解析)、`08_reverse_control.txt`(双拼复现) |
| B | `/tmp/tmem-validation/B/` | `session.log`、`session.v4.jsonl`(真实会话 transcript,已扫描无凭据泄漏)、`tool_schema_counts.txt`、`capture_*.json`、`lookup_{1,2,3}.txt` |
| C | `/tmp/tmem-validation/C/` | `REPORT.md`、`run_track_c.sh`(一键复现)、`step7_MEMORIES.md`、`transcript.synthetic.jsonl` |
| D1 | `/tmp/tmem-validation/D1/` | `metric_crosscheck.md`、`findings.md`、`runs_baseline.sha256`、`eval_*.out` |
| D2 | `/tmp/tmem-validation/D2/` | `SUMMARY.txt`(合并结论)、`stats_all.txt`、`evidence/test_ci4cpu_*.log`(9/10 红)、`evidence/probe_during_4cpu.jsonl`、`prof/*.cpuprofile`、`baseline/`(HEAD,只读) |
| E | `/tmp/tmem-validation/E/` | `EVIDENCE.md`(12 组实证)、`rp_err.mjs`、`dshprobe/`、`idempotency/` |
| F1 | `/tmp/tmem-validation/F1/` | `FINDINGS_F1a.md`、`FINDINGS_F1b.md`、`cochange_diag{,2,3}.{mjs,out}`(过滤器归因 + 反事实矩阵)、`f1b_verify.sh`、`session2.v4.jsonl`、`ws2-1hFjWP/`(live 会话工作区) |

**调度者独立复核产物**(不依赖任何子代理):
- 升级前后 `npm test` 基线(48/2 → 49/1 → 50/0)
- DSH 0.2.0-rc.2 vs 0.2.2-rc.5 凭据行为 A/B(隔离前缀,同一凭据文件)
- `dsh-tool-executor-native` 在 npm 上的最早版本 = `0.2.2-alpha.2`
- `.jsonl.zstd` 写入在两版 DSH 中均存在(旧版 `dsh-session-persistence-jsonl` 中 zstd 出现 24 次)
- Python 版 adapter(`5ebcebe^`)zstd 出现次数 = **0** → 缺陷跨越 Python→Node 移植
- P0-1 恒真断言的独立复现(真实 dsh + 损坏路径)
- 真实 git 历史共变统计(42 commits / 196 files / `README.md↔README_EN.md` 20x)
- web profile 的 `credentials` 插件在升级后仍启用 → 用户日常驱动未受影响

---

## 8. 排除版本因素后的结论

本节回答两个问题:**哪些发现其实是 DSH 版本造成的**(不该记在 tmem 头上),以及**把版本因素抽掉后,剩下的纯粹 tmem 问题是什么、按什么优先级排**。

### 8.1 因 DSH 版本造成、升级后已消解 —— **不计入 tmem 缺陷**

| # | 现象 | 根因 | 现状 |
| :--- | :--- | :--- | :--- |
| **V1** | `Live @deepseek-ai/dsh compact + prune integration` 硬失败 `MODULE_NOT_FOUND: @deepseek-ai/dsh-tool-executor-native` | DSH `0.2.0-rc.2` 早于 executor 包的首个发布版(`0.2.2-alpha.2`) | ✅ 升级后**跑起来并通过** —— 这条测试首次真正执行 |
| **V2** | headless profile 启动即 `MISSING_CREDENTIAL: llm-pi-ai … "minimax-cn"` | `0.2.2-rc.5` 的 dsh-headless bundle **有意**禁用交互式凭据库(*"One-shot jobs use launch-time credentials instead of the interactive store"*) | ✅ 这是**新 DSH 的设计**,非 tmem 缺陷;web profile 不受影响;headless 需 env 或 `--patch` 覆盖层 |
| **V3** | cordis `4.0.4` 与 tmem 声明的 peerDep `~4.1.0` 不符 | 装着的 DSH 相对**本仓库自身声明**过时 | ✅ 已对齐 `4.1.0` |

> 这三条是"环境错配",不是产品缺陷。**升级后它们全部消失,且不会以任何形式回流。**

### 8.2 版本无关的 tmem 缺陷 —— **排除版本后,优先级会重排**

这是本节最重要的一点:**抽掉版本噪声后,最紧急的问题换人了。**

| 新优先级 | 缺陷 | 为什么与 DSH 无关 |
| :--- | :--- | :--- |
| **①** | **P0-3 CI 门近乎必然红** —— 4 vCPU(CI 形状)`npm test` **9/10 失败**,双版本矩阵 P(至少一腿红) ≈ **99%** | 纯由 `node --test` 的文件级并发语义 + `sqlite_compat.mjs:79-96` 每次调用开关连接造成。**与 DSH 一点关系都没有** —— 换成任何 DSH 版本都一样红,而且**每一个 PR 都在承担这个风险** |
| **②** | **P0-2 真实 trace 断链** —— `adapters/dsh.mjs:207` 只匹配 `*.jsonl`,DSH 写 `*.jsonl.zstd` | DSH 至少从 `0.1.7-rc.2` 起就写 zstd;Python 版 adapter 的 zstd 出现次数 = **0**。**跨 DSH 版本、跨 Python→Node 移植都从未修过** —— 不是"升级造成的",是"一直没对过" |
| **③** | **P0-1 e2e 恒真断言** —— 路径损坏成 `.dsh/.dsh/.dsh` 仍为绿 | 纯测试缺陷。**且与 ② 构成致命组合**:实现断了链,测试还假绿 → 这个缺陷可以无限期潜伏下去。升级反而放大了这个风险 —— 测试终于开始跑了,却跑不出任何信号 |
| **④** | **P1-4 在途修复的收益被高估** —— 工作区 patch 从不被自动加载 | **已跨版本验证**:DSH `0.2.0-rc.2` 的 dump 里 `tmem.patch.yml` 出现 **0** 次、`.dsh/plugins` **0** 次;`0.2.2-rc.5` 同样是 **0**。**两代 DSH 都没有工作区层** → 这是 tmem 对 dsh patch 层的理解偏差,不是版本差异 |
| **⑤** | P1-1 / P1-2 / P1-3 / P1-5 ~ P1-14、P2-1 ~ P2-10 | **全部是 tmem 自身**的实现、测试与文档问题,与 DSH 版本无关 |

### 8.3 排除版本后依然成立的**能力验证**结论(正面)

排除版本因素后,能力侧的结论一条都没变 —— 因为它们本来就不依赖版本:

| 能力 | 结论 | 证据 |
| :--- | :--- | :--- |
| 记忆引擎本身可用 | ✅ | F1 阳性对照:`l1_hints 2 / l1_adopted 2 / l1_adoption_rate 1`,agent 推理中直接引用注入的提示 |
| MEMORIES.md 三节可同时填满 | ✅ | F1-b:§1 L0_FULL 70% + §2 一条 L2 因果修复 + §3 两条共变对 |
| 工具正确进入 agent tool schema | ✅ | `tmem_search` / `tmem_expand` 各 2 次,出现在 `request/header` 的 25 个工具名中 |
| 闭环成立 | ✅ | `capture → sync → lookup(4/4) → pre_step 召回注入` |
| `replay` 能剪除噪声 | ✅ | 10 步 → **3 步关键路径**(READ → MUTATE → ENV_EXEC) |
| L2 因果抽取需要真实失败循环 | ✅ | F1-b 的 `node --test test/` glob 缺陷构成"观察到失败 → 修复 → 验证",被抽成 L2 条目 |
| 无实现性能回退 | ✅ | 工作区 expand 17.53 ms vs HEAD 基线 18.32 ms(在途改动反而更快,作用域零重叠) |

### 8.4 一句话重述(排除版本后)

> **tmem 的记忆引擎是好的,记忆机制是通的,甚至 L1 纵向学习都能达到 100% 采纳率。**
>
> 真正的问题只有两类:
> **(一)它看不见** —— transcript 是 `.zstd` 而它只认 `.jsonl`(自 Python 时代就没对过);
> 共变引擎不跟随 git 重命名,把移植前的历史路径判死,再叠加"排除文档对 + 丢弃大 commit"两个过滤器,最终 42 个 commit 的真实信号被清成 **0 对**。
> **(二)它被自己的测试骗了** —— e2e 断言恒真,延迟门在 CI 形状下 9/10 红,门禁对 README 的延迟数字完全无感。
>
> 这两类问题**都与 DSH 版本无关**,修它们不需要升级任何东西。

### 8.5 0.4.1 之后,这份优先级表怎么变

| 新优先级 | 缺陷 | 0.4.1 状态 |
| :--- | :--- | :--- |
| **①** | **P0-3 CI 门近乎必然红** | ❌ **未修复**,10/10 实测仍 9/10 红 → **仍居第一** |
| **②** | **P0-2 真实 trace 断链** | ⚠️ **半修复**:文件能发现了,内容仍解不出 → **降级为 P0-4**(多帧 zstd),**优先级不变** |
| **③** | **P0-1 e2e 恒真断言** | ✅ **已修复**,新断言有区分能力 → **降级为 P2**(仅剩"单帧 fixture 与真实多帧不符"这一条夹带问题) |
| **④** | **P1-4 在途修复收益被高估** | ❌ 未变,且**现在已不成立**——修复已进 `0.4.1` 并发布到 GitHub,工作区 patch 的写法本身是**对的** |
| **⑤** | 共变引擎 0 对 | ✅ **已修复**(2 对 → 29 对)→ **从缺陷列表移出** |
| **⑤′** | **P0-4 zstd 多帧只解首帧**(新发现) | 🔴 **新增,优先级仅次于 ①** —— 它让 P0-2 的"半修复"变成**静默失败** |

> **排除版本因素后的最终排序(0.4.1 视角)**:
> **① CI 红门(与 DSH 无关,每个 PR 都中招)→ ② zstd 多帧解压(新 P0)→ ③ 其余 tmem 自身问题(14 P1 + 10 P2)。
> 能力侧正面结论**一条都没变**。

---

## 9. 版本现状与发布缺口

### 9.1 当前版本(2026-11-21 更新)

| 包 | 安装版本 | npm `latest` | 状态 |
| :--- | :--- | :--- | :--- |
| `@deepseek-ai/dsh` | `0.2.2-rc.5` | `0.2.2-rc.5` | ✅ 已是最新,无更新版本可升 |
| `@deepseek-ai/dsh-tool-executor-native` | `0.2.2-rc.5` | `0.2.2-rc.5` | ✅ 锁步一致 |
| `@deepseek-ai/cordis` | `4.1.0` | — | ✅ 对齐 tmem 的 peerDep |
| 其余 dsh-* 核心子包 | 全部 `0.2.2-rc.5` | 锁步 | ✅ |
| `dsh-tmem` | 本地 **`0.4.1`**(npm link → `/archive/tmem`) | **`0.4.0`** | ⚠️ **npm 上还没有 0.4.1** |

健康度复验:`tmem --version` = **0.4.1**;`dsh --profile web --dump-config` 中 tmem 解析为 `file:///archive/tmem/tmem/dsh_plugin.mjs`;`npm test` = **50 pass / 1 fail**(唯一失败为 CI 延迟门,§10.4)。

### 9.2 发布缺口:**GitHub 已到 0.4.1,npm 还停在 0.4.0**

| 项 | 已发布 `0.4.0`(npm) | GitHub `0.4.1`(已同步到本地) |
| :--- | :--- | :--- |
| `installer.mjs` 写入的 patch 路径 | `./.dsh/plugins/tmem-dsh-plugin.mjs`(**会双拼的旧写法**) | `./plugins/tmem-dsh-plugin.mjs` ✅ |
| `cli.mjs` 的 realpath 入口判定 | `resolveRealPath` 出现 **0** 次 | ✅ 已修(`safeRealpath`) |
| `adapters/dsh.mjs` zstd 支持 | ❌ 完全没有 | ⚠️ **有,但只解首帧**(§10.3) |
| 共变引擎跟随 git 重命名 | ❌ | ✅ |
| `.dsh/tmem.patch.yml` | 包内**不含** | ✅ 已修 |

→ **从 npm 装到的用户,拿到的仍是有缺陷的 0.4.0。** `0.4.1` 目前只在 GitHub 仓库里。

### 9.3 要让修复真正生效,还差两步(需你授权)

1. ~~应用 P0 修复 + 版本号 bump~~ ✅ **已完成**(远端已提交 `eafb045 chore(release): bump version to 0.4.1`)
2. **`npm publish`** —— ⚠️ **本机没有发布凭证**(`npm whoami` → `ENEEDAUTH`),且发布到公共 registry 是**对外可见**的动作
3. **(可选,建议)** 修 P0-4 多帧 zstd 后再发 `0.4.2` —— 否则发出去的 0.4.1 仍会让自动捕获**静默产出空 trace**(§10.3)

> 按 AGENTS.md 的协议,本次战役本身的执行路径可用 `tmem capture --agent dsh` 沉淀。
> 但请注意 **P0-2/P0-4**:自动捕获会撞上 zstd 多帧断链,需显式 `--transcript` 才能成功。

---

## 10. 0.4.1 同步复验

> **触发**:2026-11-21,远端 `origin/main` 推送了新提交(`d2a9a96..eafb045`),声明已修复相关问题。
> **执行**:同步 → 逐条复验原 P0 → 追加新发现。范围仍为**只验证、出报告**。

### 10.1 同步动作

| 项 | 结果 |
| :--- | :--- |
| 远端新提交 | `7a4bc30` fix(install) → `db7fceb` fix(core) → `eafb045` chore(release) bump 到 `0.4.1` |
| 同步方式 | `git fetch --all` → `git merge --ff-only origin/main`(无 merge commit) |
| 同步后 HEAD | `eafb045` |
| 同步后版本 | `0.4.1`(`node -p "require('./package.json').version"`) |
| 全局安装 | `npm ls -g` → `dsh-tmem@0.4.1 -> /archive/tmem`(link 完好,`tmem --version` = 0.4.1) |
| 远端改动 | 11 个文件,+204 / −54 |

**关于工作区原有的 4 个未提交改动**(`tmem/cli.mjs`、`tmem/installer.mjs`、`.dsh/tmem.patch.yml`、`tests/intent_graph.test.mjs`):

逐文件比对后确认,它们与远端 `7a4bc30` **是同一批修复的两次实现**,远端版本**严格更强**:

| 文件 | 本地未提交版 | 远端 0.4.1 | 结论 |
| :--- | :--- | :--- | :--- |
| `tmem/cli.mjs` | `resolveRealPath()` + 注释 | `safeRealpath()` 等价实现 | 远端等价,本地无增量 |
| `tmem/installer.mjs` | 改路径 + 注释 | 改路径 | 远端等价,本地仅多注释 |
| `.dsh/tmem.patch.yml` | 改路径 + 注释 | 改路径 | 远端等价,本地仅多注释 |
| `tests/intent_graph.test.mjs` | 字符串包含断言 | **语义化断言**:解析 patch → resolve 目标 → 断言文件存在 → 另加 symlink CLI 启动测试 | 远端**严格更强** |

处置:本地改动已**完整备份**到 `/tmp/tmem-sync-2026-11-21/local-uncommitted.patch` 与 `/tmp/tmem-sync-2026-11-21/backup/`,再执行 `git checkout --` 丢弃,随后 fast-forward。
**备份完整保留,可随时恢复**;`git apply /tmp/tmem-sync-2026-11-21/local-uncommitted.patch` 即可还原(但会与 0.4.1 冲突,因远端已包含等价改动)。

> 三个 `??` 未跟踪文档(`docs/dsh-validation-plan.md`、`docs/dsh-validation-report.md`、`docs/plugin-install-troubleshooting.md`)**未被触碰**。

### 10.2 原 P0 逐条复验

#### ✅ P0-1 e2e 恒真断言 —— **真修好了**

0.4.1 在 `tests/dsh_e2e.test.mjs:363` 新增:

```js
assert.ok(dumpOut.includes(pathToFileURL(path.join(ws, ".dsh", "plugins", "tmem-dsh-plugin.mjs")).href));
```

**独立复核**(调度者亲自执行,构造损坏路径):

```bash
WS=/tmp/p0check2/ws; rm -rf /tmp/p0check2; mkdir -p "$WS/.dsh/plugins"
echo "// fake" > "$WS/.dsh/plugins/tmem-dsh-plugin.mjs"
printf -- "- insert:\n    - id: tmem\n      name: ./.dsh/plugins/tmem-dsh-plugin.mjs\n" > "$WS/.dsh/tmem.patch.yml"
cd "$WS" && dsh --profile headless --patch "$WS/.dsh/tmem.patch.yml" --dump-config
```

输出解析到的路径为 `file:///tmp/p0check2/ws/.dsh/.dsh/plugins/tmem-dsh-plugin.mjs`(**文件不存在**,双拼复现):

| 断言 | 损坏路径下的值 | 是否有区分能力 |
| :--- | :--- | :--- |
| 旧:`dumpOut.includes("tmem-dsh-plugin.mjs")` | `true` | ❌ 恒真 |
| 新:`dumpOut.includes(fileURL(<ws>/.dsh/plugins/...))` | `false` | ✅ **有效** |

→ 断言**首次具备区分能力**。P0-1 关闭。

#### ✅ 共变引擎 0 对 —— **真修好了**

0.4.1 重写了 `tmem/cochange.mjs`:移除 `isDocFile` 过滤、用 `git log -M --name-status` 建 renameMap 并沿 rename 链解析历史路径。

**实测对照**(同一仓库 `/archive/tmem`):

```bash
cd /archive/tmem && node -e "import('./tmem/cochange.mjs').then(m=>{
  const p=m.computeCochangeStats('/archive/tmem');
  console.log('对数 =',p.length,' 置信度>=0.5 =',p.filter(x=>x.confidence>=0.5).length);});"
```

| 版本 | 共变对数 | 置信度 ≥0.5 |
| :--- | :--- | :--- |
| 旧版 `d2a9a96` | **2** | — |
| 新版 `eafb045` | **29** | **29(全部)** |

样例:`README.md ↔ tmem/dsh_plugin.mjs`、`tmem/adapters/base.mjs ↔ tmem/slicer.mjs`、`tmem/cli.mjs ↔ tmem/dsh_plugin.mjs`。
→ 共变信号从"几乎为零"恢复到可用水平,**§8.5 中该条已从缺陷列表移出**。

#### ❌ P0-3 CI 延迟门 —— **未修复**

10 次完整 `npm test`(空闲机器,24 vCPU):

```
ℹ pass 50 ℹ fail 1  ← run 1..7
ℹ pass 51 ℹ fail 0  ← run 8
ℹ pass 50 ℹ fail 1  ← run 9, 10
```

**9/10 红**,失败点固定为 `tests/compaction_inverted.test.mjs:807` 的 `assert.ok(report.latency.avg_expand_latency_ms < 25)`。
单跑该文件则 5/5 全绿 —— 仍是**并发负载敏感门**,与 §3.6 结论一致。

0.4.1 对 `tmem/sqlite_compat.mjs` 的改动是 **WAL 模式 + busy 重试(3 次)**,**没有动"每次 `withDatabase` 都开/关一个 SQLite 连接"这个性能根因**(`sqlite_compat.mjs:79-96` 原实现)。因此 CI 红门原封不动。

### 10.3 🆕 新发现 P0-4:zstd 多帧只解首帧(0.4.1 修了一半)

**这是本次同步最重要的发现**:0.4.1 确实加上了 zstd 支持,但**只对"单帧压缩"的数据有效**;DSH 真实写出的文件是**拼接多帧容器**,于是
**自动捕获从"找不到文件"变成了"找到文件但内容为空"—— 静默失败**。

#### 根因

0.4.1 的 `tmem/adapters/dsh.mjs` 新增:

```js
function readTranscriptText(transcriptPath) {
  if (transcriptPath.endsWith(".zstd")) {
    if (typeof zlib.zstdDecompressSync === "function") {
      return zlib.zstdDecompressSync(readFileSync(transcriptPath)).toString("utf-8");  // ← 只解第一帧
    }
    return execFileSync("zstd", ["-dc", transcriptPath], { encoding: "utf-8" });        // ← 正确,但走不到
  }
  ...
}
```

Node 的 `zlib.zstdDecompressSync` **一次只解一帧**;`createZstdDecompress()` 流式 API 同样只解一帧(已实测)。
而 DSH 用的是**拼接帧容器**(每批 append 一个独立 zstd frame),它自己实现了 `scanZstdFrames()` 逐帧解码
(`@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`,含 `NodePrivateZstdFrameDecoder` / `PublicZstdFrameDecoder`)。
**由于 Node ≥22.15 存在 `zstdDecompressSync`,tmem 永远走不到那条正确的 CLI fallback 分支。**

#### 证据(真实数据,非合成)

真实文件:`/home/kent/.dsh/sessions/--tmp-tmem-validation-F1-ws2-1hFjWP--/session-0c9e346c-db72-426f-99e1-4d35e3f10f94/session.v4.jsonl.zstd`

| 量 | 值 |
| :--- | :--- |
| 文件字节 | 87,600 |
| `zstd -dc` 解出 | **291,433 字节 / 163 行** |
| Node `zstdDecompressSync` 解出 | **189 字节 / 1 行**(仅首帧 = session 头) |

**对照实验**(`docs/evidence/dsh-0.4.1-multiframe-check.mjs`,同一份内容,四种喂法):

| # | 输入 | tmem 0.4.1 解析结果 | 说明 |
| :--- | :--- | :--- | :--- |
| A | **真实 DSH 多帧 zstd** | `actions=0, intents=0` | ❌ 坏 |
| B | 同一文件 `zstd -dc` 明文 | `actions=28, intents=1` | ✅ parser 本身没问题,纯粹是解压层 |
| C | **合成单帧** zstd(`zstdCompressSync`) | `actions=28, intents=1` | ✅ **远端 e2e 测试只覆盖了这种形态** |
| D | **合成多帧**(DSH 形态) | `actions=0, intents=0` | ❌ **复现真实故障** |

**A vs C 就是这个 bug 的全貌**:远端新增的 e2e 用例用 `zlib.zstdCompressSync` 造 fixture,那是**单帧**;真实 DSH 文件是**多帧**。
**测试绿灯,真实故障** —— 与原 P0-1 同款模式的"合成数据自证"。

#### 端到端表现

```bash
tmem capture --agent dsh --workspace <ws> --force
```

| 版本 | 返回 |
| :--- | :--- |
| 0.4.0 | `{"status":"skipped","reason":"no_transcript_found"}` ← 找不到文件 |
| **0.4.1** | **`{"status":"skipped","reason":"no_effective_actions"}`** ← **找到了,但解出来是空** |

**后者更危险**:从日志看"会话本来就没有有效动作",而不是"解码失败"。用户不会知道自己的记忆系统正在收集空气。

#### 修复方向

对齐 DSH 的做法:扫描帧边界后**逐帧** `zstdDecompressSync` 再拼接;或直接调 `zstd -dc`;或用 DSH 已有的 `scanZstdFrames` 逻辑。
**同时必须把 e2e 的 fixture 从"单帧"改成"多帧拼接"**,否则下一个修复还会假绿。

### 10.4 当前健康度总览

| 项 | 结果 |
| :--- | :--- |
| `npm test` | **50 pass / 1 fail**(唯一失败 = CI 延迟门) |
| 单独跑延迟门文件 | 7 pass / 0 fail,5/5 稳定 |
| `tmem --version` | `0.4.1` |
| 全局包 | `dsh-tmem@0.4.1 -> /archive/tmem`(npm link) |
| `dsh --profile web --dump-config` | tmem 解析为 `file:///archive/tmem/tmem/dsh_plugin.mjs` ✅ |
| npm `latest` | **仍为 `0.4.0`** —— 0.4.1 只在 GitHub |
| 本机发布凭证 | ❌ `npm whoami` → `ENEEDAUTH` |

### 10.5 建议(按优先级)

1. **修 P0-4(多帧 zstd)** —— 这是当前唯一挡在"自动捕获可用"前面的东西。修完自动捕获即可端到端跑通。
2. **修 P0-3(CI 延迟门)** —— 根因是 `withDatabase` 每次开关连接;建议改连接池或放宽阈值,**在修之前 CI 门会持续随机变红**。
3. **把 e2e 的 zstd fixture 改成多帧拼接** —— 防止"修好了但没测到"。
4. **`npm publish 0.4.1`(或修完 P0-4 后发 0.4.2)** —— 需你授权 + 提供凭证;**发布是对外可见动作**。
5. 补 P1 / P2 共 24 条(见 §4)—— 均为 tmem 自身问题,与 DSH 版本无关。

### 10.6 复现命令清单

```bash
# 同步
cd /archive/tmem && git fetch --all && git log --oneline HEAD..origin/main && git merge --ff-only origin/main

# 版本与链接
node -p "require('/archive/tmem/package.json').version"
npm ls -g --depth=0 | grep tmem && tmem --version

# P0-1 断言敏感性(损坏路径)
WS=/tmp/p0check2/ws; rm -rf /tmp/p0check2; mkdir -p "$WS/.dsh/plugins"
echo "// fake" > "$WS/.dsh/plugins/tmem-dsh-plugin.mjs"
printf -- "- insert:\n    - id: tmem\n      name: ./.dsh/plugins/tmem-dsh-plugin.mjs\n" > "$WS/.dsh/tmem.patch.yml"
cd "$WS" && dsh --profile headless --patch "$WS/.dsh/tmem.patch.yml" --dump-config | grep -A1 "id: tmem"

# P0-4 多帧对照(仓库内已固化)
cd /archive/tmem && node docs/evidence/dsh-0.4.1-multiframe-check.mjs

# 共变引擎新旧对照
cd /archive/tmem && node -e "import('./tmem/cochange.mjs').then(m=>console.log(m.computeCochangeStats('/archive/tmem').length))"

# CI 门复现
cd /archive/tmem && for i in $(seq 1 10); do npm test 2>&1 | grep -E "^ℹ (pass|fail)" | tr '\n' ' '; echo; done
```

> **证据留存说明**:本次战役的原始证据目录 `/tmp/tmem-validation/*` 已随 `/tmp` 回收被清除;
> 本节的结论均由上面的命令**现场可复现**,且多帧对照脚本已固化进仓库 `docs/evidence/dsh-0.4.1-multiframe-check.mjs`。

---

---

## 11. 问题总结(0.4.1 视角 · 一页纸)

> **本节是全报告的收口**:截至 0.4.1,**还剩什么问题、什么已解决、什么不是问题**。
> 按用户指示:**只总结,不做修复、不做发布**。

### 11.1 一句话

> **记忆引擎本身是好的(能力侧结论一条没变),剩下的问题全在"外围"**:
> **一道必红的 CI 门** + **一个让自动捕获静默失效的 zstd 多帧缺陷** + **24 条 tmem 自身的实现/测试/文档问题**。
> **没有一条与 DSH 版本有关** —— 升到 `0.2.2-rc.5` 已经是天花板,再升也不会变好。

### 11.2 仍然开放的问题(24 条)

| 严重度 | 数量 | 内容 |
| :--- | :--- | :--- |
| **P0** | **2** | **P0-3** CI 延迟门近乎必然红(9/10)· **P0-4** zstd 多帧只解首帧,自动捕获静默产出空 trace |
| **P1** | **12** | capture 非幂等 · compaction 事件未接 CLI · `--json` 静默失效 · installer 的 home 层写入与"已装好"假象 · README/排障文档过时 · intent_graph 断言(**已修**) · 延迟门负载敏感 · `withDatabase` 每次开关连接 · `normalizeRelPath` 绝对路径泄漏 · 共变引擎重命名(**已修**) · `is_trivial` trace 被隐藏 |
| **P2** | **10** | README 延迟宣称被证伪 · 门禁不比对 README 数字 · 若干注释/模板/文档漂移 · 幂等守卫是子串启发式 · 基准报告非字节可复现 · 门禁只断言 L2 |

### 11.3 0.4.1 关闭的问题(3 条)

| 原编号 | 问题 | 0.4.1 的修法 | 复验 |
| :--- | :--- | :--- | :--- |
| **P0-1** | e2e 恒真断言 | 断言改为比对 `file://` 绝对 URL | ✅ 损坏路径下旧断言 `true` / 新断言 `false`,**首次有区分能力** |
| **P1-9** | intent_graph 字面量断言 | 改为解析 patch → resolve → `existsSync` + symlink CLI 启动测试 | ✅ 断言变严格 |
| **P1-13** | 共变引擎不跟随 git 重命名 | `git log -M --name-status` + renameMap 解析链 | ✅ **`/archive/tmem` 上 2 对 → 29 对**,置信度全 ≥0.5 |

### 11.4 被重新定义的问题(1 条)

**P0-2** 从"找不到 transcript 文件"升级为 **P0-4**"找到了但解不出内容":

| 版本 | `tmem capture --agent dsh` 的返回 | 真实含义 |
| :--- | :--- | :--- |
| 0.4.0 | `{"status":"skipped","reason":"no_transcript_found"}` | 文件没找到(明确) |
| **0.4.1** | **`{"status":"skipped","reason":"no_effective_actions"}`** | **文件找到了,但只解出第一帧 → 空内容** |

**后半句更危险**:日志读起来像"这个会话本来就没价值",用户不会知道记忆系统在收集空气。
根因是 Node 的 `zlib.zstdDecompressSync` **一次只解一帧**,而 DSH 写的是**拼接帧容器**(DSH 自己有 `scanZstdFrames` 逐帧解码)。
0.4.1 的 e2e fixture 用 `zstdCompressSync` 造的是**单帧**,所以**测试绿、真实故障**。

### 11.5 明确"不是问题"的项(避免被误当缺陷追)

| 项 | 真相 |
| :--- | :--- |
| headless profile 报 `MISSING_CREDENTIAL` | **DSH 0.2.2 的新设计**(一次性任务用启动时凭据),web profile 不受影响 → **不是 tmem 缺陷** |
| cordis 版本对齐 | 已对齐 `4.1.0`,符合 tmem 自己的 peerDep → **已消解** |
| `dsh-tool-executor-native` 缺失 | 是 DSH `0.2.0-rc.2` 太老 → **升级后消解** |
| **工作区 `.dsh/tmem.patch.yml` 不被自动加载** | **DSH 的 patch 层根本没有工作区层**(`bundle → profile → home → --patch`),跨两个 DSH 版本验证过 → **不是可修的 bug**,是集成方式需要重新设计 |
| MEMORIES.md §3 稀疏 | 共变引擎修好后已显著改善(29 对)→ **不再是问题** |

### 11.6 能力侧正面结论(**一条都没变**)

| 能力 | 结论 |
| :--- | :--- |
| 记忆引擎可用 | ✅ `l1_adoption_rate = 1`(agent 推理中直接引用注入的提示) |
| MEMORIES.md 三节可同时填满 | ✅ §1 L0_FULL + §2 L2 因果修复 + §3 共变对 |
| 工具正确进入 agent tool schema | ✅ `tmem_search` / `tmem_expand` |
| 闭环成立 | ✅ `capture → sync → lookup(4/4) → pre_step 召回注入` |
| `replay` 能剪除噪声 | ✅ 10 步 → 3 步关键路径 |
| 无实现性能回退 | ✅ 工作区 expand 17.53 ms vs HEAD 18.32 ms |

### 11.7 发布缺口(**唯一一件"对外"的事**)

| 位置 | 版本 | 状态 |
| :--- | :--- | :--- |
| GitHub `origin/main` | `0.4.1` | ✅ 已同步到本地,已复验 |
| npm `latest` | **`0.4.0`** | ❌ **未发布** —— npm 用户拿到的仍是有缺陷的版本 |
| 本机发布凭证 | — | ❌ `npm whoami` → `ENEEDAUTH` |

> 按你的指示:**未发布、未修复**。若日后要发,建议**先修 P0-4 再发 `0.4.2`** —— 否则发出去的 0.4.1 仍会让自动捕获静默产出空 trace。
