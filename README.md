# `tmem` — 面向编程 Agent 的无感执行轨迹记忆编译器

**中文文档** | [English](./README_EN.md)

`tmem`（**Trace Memory**）是一个**零 UI、无控制面、双层架构（项目级 `.tmem/` + 全局级 `~/.tmem/`）**的编程 Agent 执行轨迹记忆插件与 CLI 工具，原生支持 **`agy` (Antigravity)**、**`@deepseek-ai/dsh` (DeepSeek Harness)**，并兼容 **`codex`**、**`opencode`**、**`pi`**。

不同于传统记忆系统记录冗长、易过期的对话文本摘要，`tmem` 将 Agent 的会话日志（`transcript.jsonl` / `session.v4.jsonl`）视为未优化的执行轨迹：以 **Git 最终落盘状态（Ground Truth）**与**最终通过的验证命令（`exit_code == 0`）**为终点，执行**确定性后向程序切片（Backward Trace Slicing）**，自动剪除所有试错弯路、废弃修改与无关扫描，编译为带预检探针（Speculative Guards）的 **L0 / L1 / L2 三层结构化记忆手册（Playbook）**。

---

## 一、核心解决的问题

1. **过程噪声极高**：Agent 解决一个复杂 Bug 往往经历 30 步探索（25 步弯路 + 5 步真正生效），全量塞入上下文既浪费 Token 又容易误导后续推理。
2. **环境漂移与记忆腐烂（Stale Memory）**：写死文件路径与行号的经验（如静态 Skill）在代码重构或换机器后会失效甚至产生反作用。
3. **无感沉淀与零管理负担**：开发者不需要打开任何 Web 面板维护记忆库；记忆随日常开发、`git commit`、里程碑达成提示或 `/tmem` 命令静默流转。

---

## 二、核心架构设计

```mermaid
flowchart LR
    subgraph Capture["1. 轨迹采集与后向切片 (Slicer)"]
        T["Agent 会话日志\n(agy transcript.jsonl / dsh session.v4.jsonl)"] --> A["多 Agent 适配器\n(AgyAdapter / DshAdapter)"]
        G["Git 工作区真实变更\n(git status / git diff)"] --> S["后向依赖切片器\n(BackwardTraceSlicer)"]
        A --> S
        S -->|"剔除死胡同修改 & 敏感信息脱敏"| P["3层结构化记忆 (TracePlaybook)"]
    end

    subgraph Store["2. 双层存储架构 (Dual-Tier Store)"]
        P -->|"L0 + L1 + L2"| Proj["项目级记忆 (.tmem/traces/)\n随 Git 仓库版本化共享"]
        P -->|"蒸馏跨项目 L2 修复对"| Glob["全局级记忆 (~/.tmem/playbooks/)\n本机跨仓库通用经验"]
    end

    subgraph Recall["3. 投机探针与分级降级召回 (Guarded Recall)"]
        Proj & Glob --> Guard{"零 Token 预检探针 (<5ms)\nfile_exists / symbol_present"}
        Guard -->|"探针全部通过"| L0["注入 L0_FULL\n精确读写与验证最短路径"]
        Guard -->|"符号漂移 / 重构"| L1["自动降级 L1_TOPOLOGY\n文件协同修改契约 + 验证命令"]
        Guard -->|"匹配报错特征"| L2["注入 L2_INVARIANT\n[报错签名 -> 因果修复动作]"]
    end
```

### 1. 基于 Git Ground-Truth 的后向切片 ([`tmem/slicer.py`](file:///home/ubuntu/proj/tmem/slicer.py))
- **终点锚定**：结合 `git diff` / `git status` 提取会话结束时真正存活在磁盘上的修改文件集合，忽略中途创建后又被删除或回滚的实验性修改。
- **因果反推**：从最终 `exit_code == 0` 的测试/构建命令出发，反向保留对最终文件产生实质贡献的 `READ -> MUTATE -> ENV_EXEC(VERIFY)` 节点。
- **因果报错配对（L2 提取）**：自动捕获会话中从“命令报错（非零退出码 / Traceback）”到“修改代码或安装依赖后同命令验证通过”的最小闭环，提炼为 `[Error Signature] -> [Causal Repair] -> [Verification]`。
- **敏感信息自动脱敏 ([`scrub_secrets`](file:///home/ubuntu/proj/tmem/adapters/base.py))**：落盘前自动使用正则抹除 GitHub PAT (`github_pat_...`、`ghp_...`)、OpenAI/Anthropic/Google API Key 等密钥，杜绝隐私泄露。

### 2. 三层抽象与投机执行探针 ([`tmem/models.py`](file:///home/ubuntu/proj/tmem/models.py) & [`tmem/guards.py`](file:///home/ubuntu/proj/tmem/guards.py))
每条沉淀的 `TracePlaybook` 同时包含三层不同粒度的知识与毫秒级预检探针（`guards`）：

| 层级 | 名称 | 内容结构 | 召回条件与降级策略 |
| :--- | :--- | :--- | :--- |
| **L0** | **关键执行路径 (`L0_FULL`)** | `READ(file) -> MUTATE(file#symbol) -> ENV_EXEC(cmd)` | 仅当本地磁盘通过所有 `file_exists` 与 `symbol_present` 探针校验（耗时 `<5ms`，零 LLM 消耗）时注入，指导 Agent 跳过全仓盲搜直奔目标。 |
| **L1** | **架构协同契约 (`L1_TOPOLOGY`)** | 协同修改文件拓扑（Co-change contract）+ 必跑验证命令 | 当代码发生重构导致 L0 具体符号探针失效时，**自动屏蔽 L0 并降级注入 L1**，防止过时代码细节误导 Agent，同时保留架构级防漏改提醒。 |
| **L2** | **跨环境排障不变量 (`L2_INVARIANT`)** | `报错特征签名 -> 最小修复动作 -> 验证命令` | 当工具执行触发同类报错或跨项目检索命中报错签名时立即注入，具备极强的跨环境可移植性。 |

### 3. 项目级 + 全局级双层存储 ([`tmem/store.py`](file:///home/ubuntu/proj/tmem/store.py))
- **项目级记忆（`<repo>/.tmem/traces/*.json`）**：
  - 随 Git 仓库提交（单条记忆一个独立短 Hash JSON 文件，如 `tr_30fa71c60d.json`，多人协作 `git merge` 零冲突）。
  - 团队成员 `git clone` / `git pull` 后，本地 Agent 立即获得该项目的架构拓扑与历史排障经验。
- **全局级记忆（`~/.tmem/playbooks/*.json`）**：
  - 存储脱敏项目私有路径后的通用工具链、依赖冲突、编译器报错修复经验（L2 Invariants），跨仓库共享。

---

## 三、多 Agent 无感插件机制

执行 `python3 -m tmem.cli init --agents agy,dsh` 后，`tmem` 会在当前工作区与全局环境自动完成隐形挂载：

### 1. Antigravity (`agy`) 原生接入
- **插件目录**：[`.agents/plugins/tmem/`](file:///home/ubuntu/proj/.agents/plugins/tmem)
  - [`hooks.json`](file:///home/ubuntu/proj/.agents/plugins/tmem/hooks.json)：挂载 `PreInvocation`（新对话投机召回 + `/tmem` 命令拦截 + 里程碑提示）与 `PostToolUse`（追踪文件修改、测试通过与报错签名）。
  - [`rules/AGENTS.md`](file:///home/ubuntu/proj/.agents/plugins/tmem/rules/AGENTS.md)：指导 Agent 消费 `<tmem_speculative_memory>` 并在达成目标后主动询问是否沉淀记忆。
  - [`skills/tmem/SKILL.md`](file:///home/ubuntu/proj/.agents/plugins/tmem/skills/tmem/SKILL.md)：支持自然语言请求沉淀记忆。

### 2. DeepSeek Harness (`@deepseek-ai/dsh`) 原生接入
- **支持 `dsh plugin add` 官方协议一键安装**：
  ```bash
  dsh plugin add github:kentzjh-cmd/tmem
  ```
- **Cordis 运行时插件**：[`tmem/dsh_plugin.mjs`](file:///home/ubuntu/proj/tmem/dsh_plugin.mjs)（通过 [`package.json`](file:///home/ubuntu/proj/package.json) `dsh.bundle` 与 [`cordis.patch.yml`](file:///home/ubuntu/proj/cordis.patch.yml) 自动导出）
  - **动态工作区感知**：自动通过 `@deepseek-ai/dsh-working-directory` 服务或 `session.header.cwd` 解析当前会话所属项目根目录，无需硬编码路径。
  - **`agent/pre-step` 钩子**：在每轮推理前静默召回 `<tmem_speculative_memory>` 并注入消息上下文。
  - **`tools/post-execute` 钩子**：追踪 `read`、`write`、`edit`、`str_replace_editor`、`apply_patch` 及 `bash`/`pwsh` 的 `[exit code: N]`，在报错时即时通过 `additionalContexts` 注入 L2 修复提示，在 `修改 -> 测试通过` 时注入 `<tmem_milestone_reached>`。
  - **原生 `/tmem` 命令注册**：通过 `@deepseek-ai/dsh-commands` 的 `commands.register` 原生注册 `/tmem [project|global|both|status|list|forget|gc]` 命令。
- **会话日志解析 ([`tmem/adapters/dsh.py`](file:///home/ubuntu/proj/tmem/adapters/dsh.py))**：原生解析 `$DSH_HOME/sessions/<id>/session.v4.jsonl`（兼容 `v3` 与 `session.jsonl`），支持多文件 `apply_patch` 补丁拆解。

---

## 四、记忆沉淀的三种触发方式

1. **达成目标后 Agent 自主提示（Milestone Prompting）**
   当 `tmem` 监测到当前会话完成了 **“修改代码 (`MUTATE`) $\rightarrow$ 运行测试/构建验证通过 (`VERIFY exit 0`)”** 的里程碑闭环时，会自动提示 Agent 在回复末尾附带一行轻量询问：
   > 💡 *是否需要将本次有效执行路径沉淀为记忆？（可输入 `/tmem project` 存项目级、`/tmem global` 存全局级、或 `/tmem both` 同时保存）*
2. **`/tmem` 斜杠命令直达（支持指定层级與自定义意图）**
   - `/tmem` 或 `/tmem project [可选备注]`：切片当前会话并保存至项目级 `.tmem/traces/`。
   - `/tmem global [可选备注]`：切片并蒸馏保存至全局级 `~/.tmem/playbooks/`。
   - `/tmem both [可选备注]`：同时保存至项目级与全局级。
   - `/tmem status`：查看当前项目级与全局级记忆库统计。
   - `/tmem list [project|global|both]`：列出所有已沉淀轨迹及其当前探针生效层级（`L0_FULL` / `L1_TOPOLOGY` / `L2_INVARIANT`）。
   - `/tmem forget <trace_id>`：按 ID 或前缀一键删除指定记忆轨迹。
   - `/tmem gc`：运行预检探针代谢检查，自动标记失效的 L0 路径。
3. **Git 提交自动兜底（`.git/hooks/post-commit`）**
   每次执行 `git commit` 时，后台静默触发 `tmem capture --scope auto --quiet`，自动完成增量轨迹切片与落盘。

---

## 五、快速开始与 CLI 手册

### 1. 初始化项目
```bash
# 在当前 Git 仓库初始化 .tmem/、~/.tmem/，并安装 agy + dsh 隐形插件与 git post-commit 钩子
python3 -m tmem.cli init --agents agy,dsh
```

### 2. 常用 CLI 命令
```bash
# 查看项目级 (.tmem) 与全局级 (~/.tmem) 记忆统计
python3 -m tmem.cli status

# 运行公开轨迹数据集（SWE-bench / ATIF / DSH）基准评测与数据验证
python3 -m tmem.cli evaluate

# 手动从最新会话日志切片并沉淀记忆（支持 --agent agy|dsh|swe_bench|atif|codex|opencode|pi 与 --scope project|global|both|auto）
python3 -m tmem.cli capture --agent dsh --scope both

# 按问题意图、关联文件或报错签名检索记忆（自动执行 <5ms 探针校验与 L0/L1/L2 分级）
python3 -m tmem.cli lookup "ZeroDivisionError" --file src/billing.py

# 运行探针代谢垃圾回收（检查代码重构后哪些 L0 探针已漂移）
python3 -m tmem.cli gc
```

### 3. 运行端到端（E2E）测试与基准验证套件
```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

---

## 六、公开轨迹数据集验证与框架有效性评测 (Benchmark & Validation)

`tmem` 内置了面向公开编程智能体轨迹（SWE-bench、ATIF、OpenHands、DSH）的标准数据解析适配器与端到端量化评测体系。

### 1. 公开轨迹数据集接入
- **SWE-bench Experiments ([`SWE-bench/experiments`](https://github.com/SWE-bench/experiments)) & SWE-agent (`.traj`)**：
  原生解析 SWE-bench 官方评测产出的 `.traj` 交互轨迹，自动映射 `open_file`、`edit_file`、`python tests/runtests.py` 等环境调用。
- **ATIF (Agent Trajectory Interchange Format)**：
  原生兼容 Harbor framework / SWE-bench Live 推行的通用轨迹交换标准 `trajectory.json`。
- **DeepSeek Harness (`session.v4.jsonl`)**：
  解析真机多轮多工具调用流与 `apply_patch` 差异。

### 2. 评测指标与量化评测结果

在 `benchmarks/data/` 典型工业级轨迹（覆盖 Django、SymPy、Flask 以及 DSH 实际工程交互）上的基准评测结果：

| 测试实例 / 轨迹来源 | 轨迹标准 | 原始交互步数 | L0 关键路径步数 | 轨迹压缩率 (CR) | 探索噪音消除率 (NER) | L1 拓扑文件 | L2 因果修复对 | 召回排名 | 编译切片耗时 | 检索耗时 |
|---|---|---|---|---|---|---|---|---|---|---|
| `django__django-11099` | `SWE-bench (.traj)` | 8 | 3 | **62.5%** | **100.0%** (4 $\to$ 0) | 1 file | 1 对 | **#1** | 0.19 ms | 2.5 ms |
| `sympy__sympy-20590` | `ATIF (trajectory.json)` | 6 | 4 | **33.3%** | **100.0%** (3 $\to$ 0) | 1 file | 0 对 | **#1** | 0.18 ms | 2.4 ms |
| `flask__flask-5014` | `SWE-bench (.traj)` | 6 | 3 | **50.0%** | **100.0%** (3 $\to$ 0) | 1 file | 1 对 | **#1** | 0.19 ms | 2.5 ms |
| `dsh_e2e_session` | `dsh (session.v4.jsonl)` | 9 | 4 | **55.6%** | **100.0%** (3 $\to$ 0) | 1 file | 1 对 | **#1** | 0.49 ms | 2.6 ms |
| **平均值 / 综合表现** | **Multi-Format** | **7.25** | **3.5** | **50.35%** | **100.0%** | **1.0 file** | **共 3 对** | **Top-1: 100% (MRR: 1.0)** | **0.26 ms** | **2.50 ms** |

### 3. 为什么目前的框架是极其有效的？
1. **彻底消除探索噪音（100% NER）**：
   Agent 在初次解决问题时往往会执行大量探索性调用（如 `find`、`grep`、`cat`、`pwd`、`git status` 以及中间失败的试错命令）。`tmem` 基于 Git Ground-Truth 的后向数据流切片算法，只保留对最终正确状态产生实质因果影响的 `READ -> MUTATE -> ENV_EXEC`，将噪声消除率提升至 **100%**。
2. **渐进式优雅降级（Graceful Tier Degradation）**：
   传统基于文本汇总的记忆在代码重构后容易诱导 Agent 产生幻觉。`tmem` 采用三层编译模型：
   - 当仓库完全契合时，通过 **L0** 毫秒级重放完整路径；
   - 当代码局部修改导致探针失败时，自动降级为 **L1 协同修改拓扑**（告诉 Agent 哪些文件需要联动修改）与 **L2 因果修复规则**（遇到同类报错直接提供验证过的修复动作），杜绝过时记忆误导。
3. **零 Token 开销与亚毫秒级纯本地运行**：
   切片编译平均耗时 **0.26 ms**，级联检索平均耗时 **2.50 ms**，完全无需调用大模型 API 总结，对宿主 Agent 交互完全透明且零延迟感知。

---

## 七、目录结构

```text
tmem/
├── pyproject.toml                 # Python 包定义与 `tmem` CLI 入口配置
├── README.md                      # 中文架构与使用文档
├── README_EN.md                   # 英文文档
├── AGENTS.md                      # Agent 记忆消费与里程碑提示协议规则
├── benchmarks/                    # 公开轨迹基准数据集与验证用例
│   └── data/
│       ├── swe_bench_django_11099.traj.json
│       ├── swe_bench_flask_5014.traj.json
│       ├── swe_bench_sympy_20590.atif.json
│       └── dsh_e2e_session.jsonl
├── tmem/
│   ├── models.py                  # ActionNode IR、GuardProbe 探针与 TracePlaybook 三层数据模型
│   ├── slicer.py                  # Git Ground-Truth 后向依赖切片器 (BackwardTraceSlicer)
│   ├── guards.py                  # <5ms 零 Token 预检探针校验与 L0->L1->L2 降级格式化器
│   ├── store.py                   # 项目级 (.tmem/) + 全局级 (~/.tmem/) 双层存储与级联检索
│   ├── evaluate.py                # 公开轨迹基准评测引擎与量化报告生成器
│   ├── cli.py                     # CLI 入口、/tmem 命令解析、里程碑检测与插件安装器
│   ├── dsh_plugin.mjs             # @deepseek-ai/dsh 原生 Cordis 插件源码
│   └── adapters/
│       ├── base.py                # 统一会话协议、报错签名提取、符号提取与密钥脱敏
│       ├── agy.py                 # Antigravity (agy) transcript.jsonl 原生适配器
│       ├── dsh.py                 # DeepSeek Harness (dsh) session.v4.jsonl & apply_patch 适配器
│       └── swe_bench.py           # SWE-bench (.traj) 与 ATIF (trajectory.json) 适配器
└── tests/
    ├── test_tmem_e2e.py           # 核心后向切片、双层存储、探针降级与 agy 钩子 E2E 测试
    ├── test_dsh_e2e.py            # @deepseek-ai/dsh v4 解析与真机 Cordis 运行时 E2E 测试
    └── test_evaluation_benchmark.py # SWE-bench / ATIF 轨迹基准评测自动化测试套件
```
