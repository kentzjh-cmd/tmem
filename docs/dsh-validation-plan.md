# tmem × DSH 验证战役 · 调度计划

> **调度者**:mcode(MiniMax Code)。mcode 不亲自跑验证,而是负责**拆分 → 派发 → 证据聚合 → 裁决 → 沉淀**。
> **被验证对象**:`/archive/tmem`(npm 包 `dsh-tmem@0.4.0`)在 DeepSeek Harness(`@deepseek-ai/dsh@0.2.0-rc.2`)上的真实可用性。
> **计划制定时间**:2026-11-20 23:5x(本轮所有"基线"均为当轮实测,非引用历史结论)。

---

## 0. 一句话定义

把"tmem 在 DSH 上是否真的能用"这个模糊命题,拆成 **5 条互不干扰的验证轨道**(Track A–E),
每条轨道由一个**子代理**独立执行并产出结构化证据;mcode 只做**编排、冲突消解、证据聚合与最终裁决**,
最后产出一份可追溯的验证矩阵。

### 为什么用"调度者"模式而不是直接跑

1. **证据面太宽**:安装层、运行时钩子、基准性能、未提交改动审查,四类工作**互不依赖**,串行做浪费时间。
2. **有污染风险**:live DSH 会话会写 `~/.dsh/`、`.tmem/`、会话日志。隔离不好会互相踩。
3. **结论需要交叉验证**:单次运行的通过/失败都可能骗人(本轮已发现 1 个"隔离跑通过、并行跑失败"的例子)。

---

## 1. 已实测基线(本轮确认,直接作为验收起点)

| # | 检查项 | 实测结果 | 证据 |
| :--- | :--- | :--- | :--- |
| B1 | `npm test` 全量 | **48 pass / 2 fail** | `node --test tests/*.test.mjs`,1.87s |
| B2 | 失败①:Live dsh compact+prune 集成 | `MODULE_NOT_FOUND: @deepseek-ai/dsh-tool-executor-native` | `tests/compaction_inverted.test.mjs:225` |
| B3 | 失败②:Milestone 5 压缩召回 | `avg_expand_latency_ms < 25` 断言失败 | `tests/compaction_inverted.test.mjs:807` |
| B4 | B3 单独跑(机器空闲) | **通过**,`avg_expand_latency_ms = 17.82`,`avg_search = 10.01` | `eval_compact_recall.mjs` 单独调用 |
| B5 | dsh 0.2.0-rc.2 是否含 executor | **完全不含**——`node_modules/@deepseek-ai/` 下无任何 `dsh-tool-executor*` 包 | `ls …/@deepseek-ai \| grep -i exec` → 空 |
| B6 | `node scripts/verify_benchmarks.mjs` | **PASS**(基准门 + 已存报告 + README 同步全绿) | 命令输出 |
| B7 | `tmem --version`(symlink 启动) | `tmem 0.4.0`,exit 0 —— **缺陷二修复已生效** | 走 `~/.nvm/…/bin/tmem` symlink |
| B8 | home 层 patch 解析 | `tmem → file:///archive/tmem/tmem/dsh_plugin.mjs` | `dsh --profile web --dump-config` |
| B9 | `zstd` 可用性 | `/usr/bin/zstd` 存在(会话日志解压证据链可用) | `command -v zstd` |
| B10 | provider 覆盖层 | minimax-cn / MiniMax-M3.1-Flash 配在 **home 层**,对所有 profile 生效 | `~/.dsh/cordis.patch.yml` |
| B11 | MEMORIES.md 记忆存量 | `project_traces: 0` —— **尚无任何历史 trace**,L0/L1 提示层为空 | `tmem status` |

### 由基线直接得出的 3 个关键判断

- **J1**:B2 **不是 tmem 的代码缺陷**,是 **DSH 版本错配**——测试写给了一个会带 `dsh-tool-executor-native` 的 DSH 版本,
  而本机装的 0.2.0-rc.2 根本不带任何 executor 包。测试的 skip 守卫只检查"dsh 是否安装",没检查"executor 是否存在",
  于是**在不该运行的环境里硬失败**。→ 这是本次战役**最高优先级**的发现,因为它意味着
  **仓库里唯一的"真·DSH 集成测试"在本机从未真正跑过**。
- **J2**:B3 vs B4 证明 Milestone 5 的延迟门是**负载敏感的非确定性断言**(并行跑 ≥25ms,空闲跑 17.82ms),
  不是算法回退。→ 需要的是**稳健化该断言**,而不是调优实现。
- **J3**:B7+B8 说明**三个静默安装缺陷中,缺陷一(路径基准)与缺陷二(symlink 入口)已修复**,
  缺陷三(home 层缺失)也已由 `~/.dsh/cordis.patch.yml` 补齐。**但这些修复尚未被任何一条真实 DSH 会话验证过**。

---

## 2. 验证目标(DoD)

分三层,每层独立判定。**任意一层失败即战役失败**,不允许用另一层的好成绩抵扣。

| 层 | 名称 | 判定标准 |
| :--- | :--- | :--- |
| **L1** | 安装与激活 | tmem 插件能在**全新工作区**与**全局 home 层**两种路径下被 DSH 正确解析、导入、激活;`tmem` CLI 在 symlink 下可用;失败时**不得静默** |
| **L2** | 运行时行为 | 真实 DSH 会话中:工具进入 agent tool schema → 执行 → 产生 trace/episode → `MEMORIES.md` 再生 → 下轮召回命中 |
| **L3** | 质量与性能 | 基准门全绿、README 同步;延迟断言在并行与串行下均稳定;2 条已知失败完成定性(代码/环境/不稳定)并给出处置建议 |

---

## 3. 判定口径(verdict taxonomy)

所有轨道用**同一套**结论标签,聚合时不得混用:

| 标签 | 含义 | 后续动作 |
| :--- | :--- | :--- |
| `PASS` | 判据全部满足,证据可复现 | 记录 |
| `FAIL(code)` | tmem 自身缺陷 | 进修复轨道 |
| `FAIL(env)` | 环境/版本错配,tmem 无责 | 出处置建议(装哪个版本 / 装什么包) |
| `FLAKY` | 同一输入多次运行结论不一致 | 进修复轨道(断言稳健化) |
| `BLOCKED` | 前置条件不具备,无法判定 | 记录阻塞原因 + 解锁动作 |

> **硬性规则**:`FAIL(code)` / `FLAKY` 必须附**可复现命令**;`PASS` 必须附**可复现命令**。
> 没有复现步骤的结论一律降级为 `BLOCKED`。

---

## 4. 调度模型

### 4.1 轨道 → 子代理角色映射

| 轨道 | 主题 | 角色 | 写权限 | 依赖 |
| :--- | :--- | :--- | :--- | :--- |
| **A** | 安装与激活回归 | `worker` | 仅 scratch 目录 | — |
| **B** | 真实 DSH 会话 E2E | `worker` | 仅 scratch 目录 | A(软依赖) |
| **C** | 钩子与生命周期闭环 | `worker` | 仅 scratch 目录 | — |
| **D1** | 基准门与报告一致性 | `verifier` | **只读** | — |
| **D2** | 延迟断言稳健性刻画 | `verifier` | **只读** | 独占机器(见 4.3) |
| **E** | 未提交改动审查 | `verifier` | **只读** | — |
| **F** | 证据聚合与裁决 | **mcode 本体** | 写报告 | A–E |
| **G** | 修复(条件触发) | `worker` | 仓库文件,**独占** | F |

### 4.2 执行顺序与并发

```
Round 1 (并发 4 路):  A  ·  B  ·  C  ·  D1        ← 子代理并发上限为 4,E 排队
        ↓ (等待全部回收)
Round 1.5:            E  (verifier,只读)          ← 槽位释放后补跑
        ↓
Round 2 (独占串行):    D2        ← 必须在机器安静时跑,否则测出来的又是负载噪声
        ↓
Round 2.5 (独占串行):  F1        ← 记忆闭环纵向验证,需要"第 2 场"会话,必须独占
        ↓
Round 3 (mcode):      F  聚合 → docs/dsh-validation-report.md
        ↓
Round 4 (条件):        G  修复 → 复测 → 二次裁决   ← Q2 已确认"只验证",故默认不触发
        ↓
Round 5:               tmem capture --agent dsh   (沉淀本战役执行路径)
```

> **为什么 D2 不能和别的轨道并发**:B3 的失败已经被证明是负载敏感型(§1 B3 vs B4)。
> 若在并发窗口测延迟,会把"机器忙"误判成"实现回退"——这正是 B3 的成因,不能重演。

### 4.3 隔离矩阵

| 资源 | 隔离方式 | 说明 |
| :--- | :--- | :--- |
| **工作区** | 每轨道独立 `mktemp -d` | 插件 store 根按会话 cwd 解析,故 `.tmem/` 天然落在各自 scratch |
| **DSH_HOME** | Track A 用 scratch `DSH_HOME`;**B/C 用真实 DSH_HOME** | ⚠️ provider 配置在 home 层,隔离 home 会丢 provider → B/C 只隔离工作区,保留真实 home |
| **`~/.dsh/cordis.patch.yml`** | 全程**只读** | 禁止任何轨道改写真实 home patch |
| **仓库文件** | A–E **只读**;仅 G 可写 | 避免并发写冲突 |
| **证据产物** | `/tmp/tmem-validation/<track>/` | ⚠️ 实际执行时**改用 /tmp 而非 `.tmem/validation/`** —— 因为"不得改动仓库任何文件"这条红线优先级更高,`/tmp` 天然不脏 `git status` |

### 4.4 子代理 prompt 契约

子代理**没有父会话上下文**,每条 prompt 必须自包含,包含以下 7 段:

1. 目标(一句话,含被测仓库绝对路径 `/archive/tmem`)
2. 已验证基线(直接引用 §1 对应行,不要求子代理重新发现)
3. 允许的操作 / 禁止的操作(禁改仓库文件、禁改真实 `~/.dsh`、禁 `git commit`)
4. 逐步命令(可直接复制执行,含 scratch 目录与清理)
5. 通过判据(逐条可勾选)
6. 证据产物落盘路径
7. 返回格式契约(见下)

**返回格式契约**(所有轨道统一):

```
TRACK: <A|B|C|D1|D2|E>
VERDICT: PASS | FAIL(code) | FAIL(env) | FLAKY | BLOCKED
FINDINGS: <数量>
EVIDENCE:
  - <可复现命令> → <实际观察到的输出>
REPRO: <判定该结论的最小复现命令>
ARTIFACTS: <落盘的证据文件绝对路径>
RECOMMENDATION: <一句话处置建议>
```

---

## 5. 轨道详解

### Track A — 安装与激活回归(`worker`)

**目标**:证明三个已修缺陷在**新工作区**里不会复发,且失败时**可见**(不静默)。

**步骤**

1. `SCRATCH=$(mktemp -d)`;`WS=$SCRATCH/ws`;`mkdir -p $WS`;`cd $WS && git init -q`
2. `DSH_HOME=$SCRATCH/dsh_home tmem init --workspace "$WS"`
   → installer 会写 `<ws>/.dsh/tmem.patch.yml`、`<ws>/.dsh/plugins/tmem-dsh-plugin.mjs`、
   `<ws>/.dsh/skills/tmem/SKILL.md`、以及 scratch home 的 `cordis.patch.yml`
3. **语义校验(而非字面量)**——这是 `docs/plugin-install-troubleshooting.md` §4 的教训:

   ```bash
   node -e '
     const fs=require("fs"),p=require("path");
     const f=p.join(process.argv[1],".dsh","tmem.patch.yml");
     const m=fs.readFileSync(f,"utf8").match(/name:\s*(\S+)/);
     const abs=p.resolve(p.dirname(f),m[1]);
     console.log("resolved:",abs,"exists:",fs.existsSync(abs));
     process.exit(fs.existsSync(abs)?0:1);
   ' "$WS"
   ```
   → 期望 `exists: true`,且解析结果形如 `<ws>/.dsh/plugins/tmem-dsh-plugin.mjs`(**不得**出现 `.dsh/.dsh`)
4. 同法校验仓库自带 `.dsh/tmem.patch.yml`(基准目录是 `/archive/tmem/.dsh/`)
5. 校验真实 home 层条目:`~/.dsh/cordis.patch.yml` 的 tmem `name:` 是**绝对路径**且文件存在
6. **激活非静默**:`dsh --profile headless --dump-config | grep -A2 tmem` → 解析为 `file://` 绝对路径;
   并跑一次真实启动,确认 stderr 的 "did not activate" 列表里**没有** tmem
7. **CLI symlink 回归**:`tmem --version`(走 PATH 上的 symlink)→ 期望 `tmem 0.4.0` + exit 0;
   再用 `node --preserve-symlinks-main` 跑一遍确认两端都被 realpath 归一
8. **反向验证**:手工把 patch 改回 `./.dsh/plugins/...` 再跑一次 dump-config,
   确认能**复现** `.dsh/.dsh` 双拼 → 证明这个修复点真的被测到,而不是恒真断言

**通过判据**:3/4/5/6/7 全绿,且 8 能复现失败(反向对照是防"恒真测试"的关键)。

**风险**:`tmem init` 会调用 `npm root -g`(只读,安全);step 8 会写 scratch 的 patch 文件,收尾前清理即可。

---

### Track B — 真实 DSH 会话 E2E(`worker`)

**目标**:在**真实 DSH agent 会话**中跑通 tmem 全链路。这是整个战役的证据核心。

**步骤**

1. `SCRATCH=$(mktemp -d)`;`WS=$SCRATCH/ws`;在 `$WS` 造一个小 JS 工程(如 `src/cache.js` + 一个有 bug 的函数),
   让 agent 有真实的"读→改→跑→再改"工作可做
2. **不隔离 DSH_HOME**(见 §4.3),直接用真实 home(其中已注册 tmem 插件 + minimax-cn provider)
3. 在 `$WS` 内启动:
   ```bash
   cd "$WS" && dsh --profile headless "<多轮任务:修改 src/cache.js 并禁止把时间戳写进缓存 key>"
   ```
   任务需触发**至少一次工具调用 + 一次失败重试 + 一次跨轮修改**,以产出有价值的 slice
4. **工具进入 tool schema** 证据:
   ```bash
   zstd -dc ~/.dsh/sessions/<workspace>/*/session.v4.jsonl.zstd | grep -c tmem_search
   ```
   → 期望 `tmem_search` 与 `tmem_expand` 均出现
5. **产物证据**:会话结束后在 `$WS` 下检查
   - `.tmem/traces/*.json` 数量 > 0
   - `.tmem/episodes/` 有内容
   - `MEMORIES.md` 被再生(内容含 trace 摘要,且不再是"no project execution traces recorded yet")
6. **遥测证据**:`tmem status --workspace "$WS"` → `l2_injections > 0` 或 `l1_hints > 0`
7. **召回闭环**:在 `$WS` 跑 `tmem lookup --workspace "$WS" "<本次任务关键词>"` → 期望命中本次 trace/episode
8. **非静默确认**:该次 dsh 运行 stderr 中 "did not activate" 不含 tmem

**通过判据**:4/5/6/7/8 全绿。

**风险与预案**:live 会话消耗 minimax-cn 额度 → 单次控制在中等复杂度任务;
若 headless profile 在真实 home 下因 provider 缺失失败,改用 `--patch` 覆盖层注入 tmem 绝对路径
(`docs/plugin-install-troubleshooting.md` §5.3),**不**改真实 home。

---

### Track C — 钩子与生命周期闭环(`worker`)

**目标**:不依赖 LLM,用**合成 hook 事件**把 L2 链路单独钉死。

**已知 hook 事件名**(源码 `tmem/hooks/dsh.mjs`):
`command` · `pre_step` / `pre_invocation` · `post_tool_use` / `post_execute`;
会话事件:`compaction_committed` · `compaction_prune`。

**步骤**

1. scratch 工作区 `$WS`;`export DSH_HOME=$SCRATCH/dsh_home`(C 不需要 provider,故可完全隔离 home)
2. `echo '<payload json>' | tmem hook dsh command --workspace "$WS" --args "status"` → 期望返回 `kind: success` 且含 `[tmem status]`
3. 投递 `post_tool_use` 事件(payload 含 shell 工具与一次失败输出)→ 期望产出 hook 侧状态写入
4. 投递 `compaction_prune` / `compaction_committed` 事件 → 期望产出 episode / pruned 归档
5. **捕获闭环**:`tmem capture --agent dsh --workspace "$WS" --transcript <合成 transcript> --force` → 期望 `status: compiled`
   (注意 `--force`:默认有 default-deny 门,平凡轨迹会被判 routine/skipped_trivial)
6. **再生闭环**:`tmem sync --workspace "$WS"` → 期望 `MEMORIES.md` 重写且含新 trace
7. **召回闭环**:`tmem lookup --workspace "$WS" "<关键词>"` → 命中刚捕获的 trace
8. **不变式**:重复 step 5 两遍,确认不会产生重复 trace(幂等性)

**通过判据**:2/5/6/7 全绿,8 无重复。

**价值**:B 依赖真实 LLM,C 不依赖 —— **若 B 因额度或 provider 失败,C 仍能独立证明 L2 链路是通的**。

---

### Track D1 — 基准门与报告一致性(`verifier`,只读)

**目标**:确认 L3 的"质量"侧没有被未提交改动破坏。

**步骤**

1. `node scripts/verify_benchmarks.mjs` → 期望 PASS(基准门 + `benchmarks/runs/*.json` + README/README_EN 同步)
2. 逐个跑 6 个评测入口,记录输出与已存报告的差异:
   `eval_compact_recall` · `eval_dsh_e2e` · `eval_slicing_fidelity` · `eval_plugin_scale` · `eval_retrieval` · `eval_llm_judge`
   (`eval_llm_judge` 的 live 模式需 `OPENAI_API_KEY` —— 本机未配 → 记为 `BLOCKED` 或跑离线模式,不要擅自配凭据)
3. `npm run check`(语法门)、`node tmem/cli.mjs evaluate`(CI 的 smoke test)
4. 复核 `benchmarks/gold/gold_annotations_50.json` 与已存报告的一致性
5. 对照 §1 B6 的当前 PASS,判断未提交改动是否影响任何门

**通过判据**:1/3 全绿;2 给出逐项差异表;4 无未解释差异。

---

### Track D2 — 延迟断言稳健性刻画(`verifier`,独占机器)

**目标**:把 B3 彻底定性为 `FLAKY` 还是 `FAIL(code)`,并给出断言稳健化方案。

**步骤**(必须在其他轨道全部结束后、机器空闲时执行)

1. **串行基线**:独立进程连续跑 Milestone 5 基准 5 次,记录 `avg_search_latency_ms` / `avg_expand_latency_ms` 分布
2. **并行复现**:用 `node --test tests/compaction_inverted.test.mjs` 复现失败;再试 `--test-concurrency=1`
   → 对比并行/串行差异,量化"机器负载"对门限的贡献
3. **同机对比**:确认实现层是否有绝对回退(拿 `git stash` 后的基线跑同一基准比较)
4. **结论三选一**:
   - `FLAKY` → 方案:预热 + 取多次中位数 + 放宽为相对断言(对比基线倍率而非绝对 25ms)
   - `FAIL(code)` → 方案:定位 expand 路径热点并优化
   - `FAIL(env)` → 方案:降级为 CI 非阻断告警
5. **顺带定性 J1**:核实 `@deepseek-ai/dsh-tool-executor-native` 属于哪个 DSH 版本引入
   (`npm view @deepseek-ai/dsh versions` + 检查候选版本依赖),给出 3 选 1 处置:
   (a) 升级 DSH 到带该包的版本 · (b) 给测试加 executor 存在性守卫(缺失则 skip,与已有 dsh 守卫同构) · (c) 改用现存的 executor 实现

**通过判据**:给出可复现的量化数据 + 明确结论 + 处置方案。

---

### Track E — 未提交改动审查(`verifier`,只读)

**目标**:在跑完验证之前,先确认"被验证的代码本身"是对的。

**被审对象**(工作区未提交改动,4 文件 + 1 新增):

| 文件 | 变更 |
| :--- | :--- |
| `tmem/installer.mjs` | 生成 patch 的相对路径基准修正 + 注释解析规则 |
| `tmem/cli.mjs` | 入口判定改为 realpath 归一比较 |
| `.dsh/tmem.patch.yml` | 同步修正路径 |
| `tests/intent_graph.test.mjs` | 断言由字面量改为正+反双断言 |
| `docs/plugin-install-troubleshooting.md`(新增) | 三个静默失败点的根因与排障手册 |

**审查要点**

1. **缺陷一修复**:installer 写出的相对路径是否对**所有** patch 消费者成立(工作区 patch 基准 = patch 文件所在目录);
   注释是否准确描述 dsh 的真实解析规则
2. **缺陷二修复**:`resolveRealPath` 在 `process.argv[1]` 不存在 / 是目录 / symlink 环时是否安全;
   `--preserve-symlinks-main` 下是否真如文档所称"两边都会被归一"
3. **缺陷三修复**:home 层写绝对路径是否幂等且不覆盖用户已有 patch;`DSH_HOME` 未设时是否落到 `~/.dsh`
4. **测试稳健性**:现有断言仍是**字符串字面量**的正反双断言(而非 §5 Track A step 3 的语义解析断言)
   → 按文档自己的教训,这是**尚未闭环的硬化项**,应作为发现项提出
5. **文档准确性**:排障文档里 "3 个缺陷" 的小标题但正文列了 4 个缺陷(缺陷四=测试把 bug 固化成契约)→ 编号口径需核对
6. **作用域副作用**:`tmem init` 现在会向 `DSH_HOME` 写 patch —— 在隔离环境/多用户机器上的影响

**输出**:按严重度排序的发现清单,**不修改任何文件**。

---

### Track F1 — 记忆闭环纵向验证(`worker`,独占)

> **为什么补这条轨道**:`tmem status` 显示仓库自身 `project_traces: 0`、MEMORIES.md §1/§2 为空。
> L3 质量层有 56 条真实轨迹语料兜底,L2 写入层由 Track B/C 生产真实 trace,
> 但 **L1 共变学习 / L2 不变式抽取 / 置信度代谢(L0→L1 降级)本质上需要同一项目 ≥2 场会话**才有东西可学。
> 单场战役验证不了这三样 —— 这是"第一轮没有真实轨迹"的真正缺口。
> F1 用**两场**会话把纵向能力闭环。

**F1-a 给仓库 store 造出第一条真实 trace**

1. 取 Track B 产出的**真实 DSH 会话 transcript**(在 `/tmp/tmem-validation/B/`),作为输入:
   `tmem capture --agent dsh --transcript <该 transcript> --workspace /archive/tmem`
2. 确认 `/archive/tmem/.tmem/traces/` 出现 trace、`MEMORIES.md` §1 从
   "No project execution traces recorded yet" 变为含条目
3. `tmem sync --workspace /archive/tmem` → 刷新 living index
4. **量化索引陈旧度**:sync 前后对比 MEMORIES.md §3 的共变拓扑条数
   (基线观测:§3 只有 2 条,而真实 git 历史里 `README.md ↔ README_EN.md` 共变 **20x**、
   `tmem/slicer ↔ tmem/store`(Python 时代 `slicer.py ↔ store.py`)**13x** —— 这些强信号当前**不在**索引里)
   → 判定:sync 是否补齐?补齐比例多少?
5. 校验 `tmem inspect <trace_id> --level index|playbook|raw|diff` 四级渐进披露都能取到内容
6. 校验 `tmem replay <trace_id>` 终端回放可执行、`tmem diff <trace_id>` 出 patch

**F1-b 验证 L1 共变提示真的浮现并被采纳(第 2 场会话)**

7. 在 scratch 工作区跑**第 2 场**真实 DSH 会话(预算内),任务**刻意触碰与 F1-a 相同的文件**
   (`tmem/installer.mjs`、`tmem/cli.mjs` —— 本次战役改过的两个文件),
   例如:"修改 tmem/cli.mjs 的版本号输出,再跑 npm test"
8. 跑 `tmem status` → 期望 `l1_hints > 0` 且 `l1_adopted > 0`
   (即:系统从真实共变拓扑推出"改 cli.mjs 时 installer.mjs 也该一起看"的提示,且 agent 真的采纳了)
9. 若 `l1_hints > 0` 但 `l1_adopted = 0`:区分是"提示未注入"(hook 问题)还是"注入了但 agent 没理"(模型行为),
   分别归因

**通过判据**

- F1-a:仓库 store 从 0 条 trace 变为 ≥1 条;MEMORIES.md §1 不再是空壳;四级 inspect 全部有内容
- F1-a:§3 拓扑在 sync 后**显著增加**(至少把 20x / 13x 这两条强信号补进来,或给出"为何补不进来"的定论)
- F1-b:`l1_hints > 0` 且能归因 `l1_adopted` 的取值

**边界(Q2 = 只验证,出报告)**

- 本轨道**只写记忆数据**(`/archive/tmem/.tmem/` + 重新生成的 `MEMORIES.md`),**不改任何 `.mjs` / `.yml` / 测试**
- `MEMORIES.md` 是被 git 跟踪的文件 → 会使 `git status` 变脏,这一点在报告中**明确列出**,由用户决定是否保留
- 不执行任何 `git commit`

---

### Track F — 聚合与裁决(mcode 本体)

1. 收齐 A–E 的结构化返回
2. 产出验证矩阵(每条判据 × L1/L2/L3 × verdict)
3. **冲突消解**:同一失败点被多轨道报告时,以证据可复现性最高者为准
4. 归类统计:`FAIL(code)` / `FAIL(env)` / `FLAKY` / `BLOCKED` 各几条,各自的处置动作与优先级
5. 写 `docs/dsh-validation-report.md`
6. 决定是否触发 Track G

### Track G — 修复(条件触发,独占仓库写权限)

**候选修复清单(按优先级)**

| P | 问题 | 轨道 | 动作 |
| :--- | :--- | :--- | :--- |
| P0 | Live dsh 集成测试在 0.2.0-rc.2 上硬失败 | D2/E | 给测试加 executor 存在性守卫,缺失即 skip(与既有 dsh 守卫同构) |
| P0 | 断言仍是字面量,恒真风险 | E | 改为语义解析断言(复用 Track A step 3 的 resolver) |
| P1 | Milestone 5 延迟门不稳定 | D2 | 预热 + 中位数 + 相对断言 |
| P1 | 排障文档编号口径(3 vs 4 个缺陷) | E | 统一为 4 个 |
| P2 | `.tmem/validation/` 未被 gitignore | F | 加一行 |

**约束**:每条修复对应一个独立 commit(或一组),修完**必须复跑** A/D1/D2 确认转绿。

---

## 6. 红线(子代理必须遵守)

1. ❌ 不修改 `/archive/tmem` 下的任何文件(A–E 全部只读;仅 G 可写)
2. ❌ 不修改真实 `~/.dsh/cordis.patch.yml`、`~/.dsh/profiles/**`
3. ❌ 不执行 `git commit` / `git push` / `git stash`(会毁掉在途改动)
4. ❌ 不擅自安装全局 npm 包、升级 `@deepseek-ai/dsh`、配置 API 凭据
5. ❌ 不删除 `/archive/tmem/.tmem/` 现有内容
6. ✅ 所有 scratch 目录用 `mktemp -d` 建,收尾清理
7. ✅ 所有结论必须附**可复现命令**

---

## 7. 里程碑与记忆沉淀

- **战役结束**:mcode 汇报验证矩阵 → 询问是否沉淀
- **沉淀方式**:`tmem capture --agent dsh` → 生成 `.tmem/traces/` 记录本次战役的**有效执行路径**
  (这正是 tmem 自己的 dogfood 场景:用 tmem 记录"验证 tmem"的路径)
- **报告归档**:`docs/dsh-validation-report.md`
- **修复归档**:每个修复一个 commit,commit message 引用验证矩阵中的判据编号

> ⚠️ 当前 `MEMORIES.md` 的 `project_traces: 0` —— 本战役是**该仓库第一条真实 trace** 的来源,
> 战役结束后的 capture 将直接填上 L0 提示层的空缺。

---

## 8. 已确认的执行参数(2026-11-20 用户确认)

| # | 参数 | 确认值 | 对计划的影响 |
| :--- | :--- | :--- | :--- |
| Q1 | live DSH 会话预算 | **1–2 场中等复杂度真实会话** | Track B 跑 1 场多轮会话(读→改→跑→再改),必要时补第 2 场覆盖 compaction 路径;控制 minimax-cn 消耗 |
| Q2 | 战役范围 | **只验证,出报告** | **Track G(修复)不触发**。§5 Track G 的候选修复清单降级为报告中的"建议清单",不改任何代码 |
| Q3 | DSH 版本 | **允许升级到带 executor 的版本** | 已执行 `npm i -g @deepseek-ai/dsh@0.2.2-rc.5`;升级后所有轨道以 0.2.2-rc.5 为基准环境 |

### Q3 升级的事实依据与回滚

**依据(升级前查证)**

| 事实 | 值 |
| :--- | :--- |
| `dsh-tool-executor-native` 最早版本 | `0.2.2-alpha.2` —— 0.2.0/0.2.1 系列**根本没有**这个包 |
| `dsh@0.2.0-rc.2`(升级前)自带 cordis | `~4.0.4` |
| `dsh@0.2.2-rc.5`(npm `latest`)自带 cordis | `~4.1.0` |
| tmem 的 peerDependency | `"@deepseek-ai/cordis": "~4.1.0"` |
| `dsh@0.2.2-rc.5` engines | node `>=24.9.0`(本机 v24.19.0 ✓) |

→ 结论:升级前安装的 0.2.0-rc.2 **相对本仓库自身声明是过时的**(cordis 大版本不匹配),
`0.2.2-rc.5` 才是 tmem 的目标环境。升级属于**修正环境错配**,不是冒险。

**回滚锚点**

| 项 | 回滚方式 |
| :--- | :--- |
| dsh 版本 | `npm i -g @deepseek-ai/dsh@0.2.0-rc.2` |
| home patch | `cp /tmp/dsh-home-cordis.patch.yml.bak ~/.dsh/cordis.patch.yml` |
| profiles | `tar -xzf /tmp/dsh-profiles-snapshot.tar.gz -C ~` |
| 全局包快照 | `/tmp/dsh-global-rollback-snapshot.txt` |

**升级后必须立即验证(未通过则回滚)**:① `dsh --version` 正常 ② `dsh --profile web --dump-config`
中 tmem 仍解析为 `file://` 绝对路径 ③ minimax-cn provider 与 MiniMax-M3.1-Flash 仍可用
④ home patch 中的插件引用在新版本下仍能解析。

---

*本计划由 mcode 生成于 2026-11-20。所有基线数据均为当轮实测,可用 §1 的命令原样复现。*

---

## 9. 执行记录(2026-11-20 战役实跑)

| 轮次 | 轨道 | 裁决 | 实际发生的事 |
| :--- | :--- | :--- | :--- |
| 前置 | DSH 升级 | ✅ | `0.2.0-rc.2 → 0.2.2-rc.5`;executor 到位、cordis 对齐 4.1.0;web profile 凭据行为未受影响(headless 有意禁用) |
| Round 1 | A / B / C / D1 / E | `PASS` / `FAIL(code)` / `FAIL(code)` / `PASS` / `FAIL(code)` | 5 路并发(上限 4,E 排队补跑);2 场真实 DSH 会话 |
| Round 1.5 | E(补跑) | `FAIL(code)` | 15 条发现,P0 恒真断言 |
| Round 2 | D2 | `FLAKY` | 等机器空闲后测量;4 vCPU(CI 形状)9/10 红 |
| Round 2.5 | F1 | `PASS` | clone 仓库内完成纵向闭环;`l1_adoption_rate = 1` |
| Round 3 | 聚合 | — | [`docs/dsh-validation-report.md`](./dsh-validation-report.md) |
| **Round 4** | **0.4.1 同步复验** | **P0 修好 2 / 半修 1 / 未修 1** | 远端推送 `eafb045`(`0.4.1`);fast-forward 同步;复验 P0-1 ✅、共变 ✅(2→29 对)、P0-3 ❌(9/10 仍红);**新发现 P0-4:zstd 多帧只解首帧**。详见报告 §10 |
| **Round 5** | **独立场景 E2E(已落地)** | **7/9 通过** | 用户放行"允许在本地仓库修复发现的 issue 并继续验证"。新建**与本仓库无关**的合成真实项目(Python 发票台账 `ledger-svc`)+ 手写真值 oracle + **11 场真实 `dsh headless` 会话** + 9 条硬门槛(G1–G9)。结果见 [`docs/dsh-independent-e2e-report.md`](./dsh-independent-e2e-report.md) |
| **Round 6** | **缺陷修复 + Round 2 复验** | **进行中** | Round 5 发现 N1(P1,中文 lookup 全失效)、N2(注入遥测盲区)、N3(归因跨会话泄漏),复验中又发现 N4(跨工作区 transcript 误取)。三项均在本地修复并加回归测试(`tests/attribution_scope.test.mjs`,4 例红/绿对照),再用**同一套预注册协议**重跑完整实验(Round 2)。详见独立报告 §2.7 |
| **Round 7** | **载体消融研究课题** | **已立项,待执行** | 用户要求:"后期测试中作为一个研究课题,必然需要明确的消融与量化,比如 tmem 的记忆注入对比散文式 MD 记忆与无记忆带来的 token 减少这类指标"。课题文档:[`docs/dsh-memory-carrier-ablation.md`](./dsh-memory-carrier-ablation.md) —— 2×2 因子(载体 × 获取方式)+ 无记忆基线,强制**内容等价**控制,指标分成本/效果/结构化独有三类,**三因子(载体 × 获取方式 × 上下文压缩)**,**定性 + 定量并重**(压缩专项配 Q1–Q7 行为编码表 + Cohen's κ),预注册门槛 CA-1..CA-9,每臂 ≥3 次重复;追查中发现待确认缺陷 `injected_trace_ids` 压缩后不重注入(假设 H-F / 消融臂 C4) |

**Round 5/6 说明(2026-11-21 → 2026-11-22)**:
Round 5 起初按用户要求"**只规划、不落地**",仅产出方案文档 [`docs/dsh-independent-e2e-plan.md`](./dsh-independent-e2e-plan.md)。
随后用户放行**落地 + 允许本地修复**,Round 5 才真正执行:11 场真实模型会话、2024s、总判定 7/9。
Round 6 承接 Round 5 的缺陷发现:本地修复 N1/N2/N3/N4 → 全量测试 53 → 57 全绿 → 同协议重跑(Round 2)以区分"修复的效果"与"模型非确定性"。
两轮的判定口径差异(G3 归因聚合范围、精度分母排除插件内部路径)已在独立报告 §2.7 **显式披露**,并同时给出三种聚合范围的结果,阈值不变。


**Round 4 说明(2026-11-21)**:远端仓库发布了 `0.4.1`,本战役因此追加一轮同步复验。
范围仍为**只验证、出报告** —— 同步了远端代码(版本号 bump 是远端做的),**未应用任何本地代码修复**,**未发布 npm**。
本地原有的 4 个未提交改动经逐文件比对确认是远端 `7a4bc30` 的等价早期实现,**已备份**(`/tmp/tmem-sync-2026-11-21/`)后丢弃,随后 fast-forward。

**实际用到的会话数**:2 场(Track B 一场 + Track F1-b 一场),落在用户批准的"1–2 场"预算内。

**计划与实际的偏差(诚实记录)**

| 计划 | 实际 | 原因 |
| :--- | :--- | :--- |
| Round 1 并发 5 路 | 4 路 + E 排队 | 子代理并发上限为 4 |
| D2 在 Round 2 与 F1 并行 | D2 串行独占,F1 另起 | D2 的延迟测量要求空闲窗口,F1 的 live 会话会污染它 |
| 证据落 `.tmem/validation/` | 落 `/tmp/tmem-validation/` | "仓库零改动"红线优先 |
| Track G(修复) | **未触发** | Q2 确认"只验证、出报告" |

**核心结论**:tmem 的记忆引擎**是好的**;断链在集成层的 transcript 格式不匹配(代码侧)
与共变引擎看不见重命名后的历史路径(数据侧)。详见报告 §5。

---
