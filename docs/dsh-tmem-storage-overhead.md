# tmem 的存储资源开销（实测）

> **文档定位**：这是插件效果报告（[dsh-independent-e2e-report.md](dsh-independent-e2e-report.md)）的一条**独立维度**。主报告度量的是**上下文 token 成本**（tmem 省 token、散文省磁盘）；本文度量的是**磁盘存储成本**（tmem 省 token、但费磁盘）。两个方向必须分开陈述，否则容易得出"tmem 更省"的错误印象。
>
> **数据来源**：三套真实实验根（非 fixture、非构造样本），逐字节测量。复现脚本：[`research/storage_overhead/measure_storage_overhead.py`](../research/storage_overhead/measure_storage_overhead.py)。

---

## 0. 结论摘要

1. **插件的存储开销主项不是「记忆」，而是派生索引**：每条捕获轨迹约 **991 KB** 磁盘，其中 **77.7%** 是 SQLite 倒排索引缓存；真正的记忆本体（trace + episode）只有约 **221 KB/条**。
2. **索引是纯派生的，删掉不丢信息**：`tmem/store.mjs:244-325` 在数据库与磁盘不一致时，会从 `traces/*.json` 与 `episodes/*.json` 全量重建（含倒排 postings）。
3. **成本结构与 token 成本结构相反**：同一批事实，tmem 渲染出的 `MEMORIES.md` 为 **3.8 KB**（散文载体 `MEMORY.md` 为 6.1 KB，**0.6×**），但支撑它的 `.tmem` 磁盘足迹为 **2934 KB**（**478×** 散文；剔除可重建缓存后仍 **102×**）。
4. **有界性要靠手工**：`tmem gc` 默认只**降级**不删除，真正回收必须显式 `--prune`；`--max-episodes-mb`（默认 200）与 `--max-episode-days`（默认 30）只是阈值，不自动执行。

---

## 1. 口径

| 项 | 口径 |
| :--- | :--- |
| 「一条轨迹」 | `.tmem/raw/tr_*.raw.json` 的一份原始留档 = 一次被捕获的会话轨迹。Round-3 fixture 共 9 条。 |
| 「磁盘足迹」 | `.tmem/` 目录下**全部**文件的字节数（含缓存）。 |
| 「入库足迹」 | 会被 `git add` 提交的字节数（按 `.tmem/.gitignore` 的排除规则）。 |
| 「散文载体」 | 载体消融实验 B 臂的 `MEMORY.md`——与 A 臂 tmem 载体**同一批事实**的散文渲染。 |
| 增长证据 | Round-3 fixture（18 场真实会话跑完后的状态）。**消融各臂不可用于测增长**——装置把种子库只读挂载以保证各臂事实完全一致。 |

---

## 2. 插件到底往项目里写了什么

### 2.1 每项目固定开销

| 路径 | 内容 | 字节 |
| :--- | :--- | ---: |
| `.dsh/plugins/tmem-dsh-plugin.mjs` | 插件主体 | 21 873 |
| `.dsh/skills/tmem/SKILL.md` | 技能说明 | 414 |
| `.dsh/tmem.patch.yml` | 注册点 | 132 |

固定合计 ≈ **21.9 KB/项目**（22 419 B）。npm 包本体（`dsh-tmem@0.4.1`，36 MB）是工具侧，不进用户仓库。

### 2.2 每项目 `.tmem/` 数据层

| 路径 | 内容 | 单条均值 | 入库？ |
| :--- | :--- | ---: | :---: |
| `.tmem/traces/tr_*.json` | 轨迹 playbook（检索主体） | 9.0 KB | ✅ |
| `.tmem/raw/tr_*.raw.json` | 会话原始事件留档 | 105.1 KB | ✅ |
| `.tmem/episodes/ep_*.json` | 会话 episode | 87.7 KB | ❌ |
| `.tmem/.cache/index.db` | SQLite 倒排索引 + FTS | **770 KB/条（分摊）** | ❌ |
| `.tmem/telemetry.jsonl` | 运行期遥测 | 很小 | ❌ |
| `.tmem/runtime_state.json` | 运行态 | 很小 | ❌ |
| `.tmem/env_facts.json` | 环境事实 | 很小 | ✅ |

排除规则由插件自己写（`tmem/store.mjs:210-229`）：

```
.cache/  runtime_state.json  telemetry.jsonl  episodes/
```

> **一处已核实的不一致**：tmem 仓库自身的 `.tmem/.gitignore` 只有前 3 行，缺 `episodes/`（该文件写于插件加入这条排除项之前，store 只在项目初始化时补齐缺失行）。目前该仓的 `.tmem/` 里没有 episodes 目录，**无实际影响**；但它说明「已存在项目不会自动获得新版本的排除规则」。

---

## 3. 实测：单条捕获轨迹的边际成本

Round-3 fixture，18 场真实会话跑完后：

```
.cache       n=  3  total    6932.0 KB   77.4%
raw          n=  9  total     945.6 KB   10.6%
episodes     n=11  total     964.2 KB   10.8%
traces       n=  9  total      80.9 KB    0.9%
root_files   n=  4  total      29.9 KB    0.3%
TOTAL        n= 36  total    8952.6 KB

每条轨迹：raw 105.1 KB + trace 9.0 KB + episode 87.7 KB
  记忆本体      221.2 KB
  缓存分摊      770.2 KB
  磁盘合计      991.4 KB
  入库(git)     114.1 KB
  缓存占磁盘    77.7%
```

### 3.1 索引自身比它索引的数据更贵

`index.db` 的页占用（SQLite `dbstat`）：

| 表 / 索引 | 字节 |
| :--- | ---: |
| `inverted_postings`（数据） | 1 896.0 KB |
| `sqlite_autoindex_inverted_postings_1` | 1 748.0 KB |
| `idx_postings_doc` | 1 040.0 KB |
| `episodes` | 944.0 KB |
| `idx_postings_term` | 896.0 KB |
| `playbooks` | 76.0 KB |

32 061 条 postings，数据表 1 896 KB → **约 61 B/行**；而**索引自身 3 684 KB**（自建索引 1 748 KB + `idx_postings_doc` 1 040 KB + `idx_postings_term` 896 KB），即索引开销约为 postings 数据的 **1.9 倍**。把整个缓存（6 932 KB，含 episodes 表、FTS 与全部二级索引）折算到每条 posting，约 **221 B**。

### 3.2 与「源数据」的关系

Round-3 的 dsh 会话转录合计 **1 966 KB**（17 场，中位 113.5 KB/场，最大 234.8 KB）。tmem 的 `raw/` 留档 946 KB / 9 条 ≈ **105 KB/条**——与原会话**同量级**。也就是说 `raw/` 是**切片而非摘要**，它保存的是会话事件的原始副本。（本节数字由测量脚本的 `[1b]` 段给出。）

---

## 4. 三个结构性发现

### 4.1 缓存是派生的，删掉不丢信息

`store.mjs:244-325` 的 `_withSyncedProjectDb`：

- 遍历 `.tmem/traces/*.json` 与 `.tmem/episodes/*.json` 的**磁盘**文件；
- 删除数据库中磁盘已不存在的行；
- 若两侧集合不一致（缺失、行数不同或 id 不匹配），**从磁盘 JSON 全量重灌** playbook 与 episode（含倒排 postings）。

因此 `index.db` 只是命中率缓存：删掉它不丢任何信息，只损失一次重建时间。

### 4.2 成本结构与 token 成本结构相反

同一批事实（载体消融 A1 臂 vs B 臂；**与 §3 的 Round-3 fixture 是不同的库**，此处的绝对值不可与 §3 相加）：

| | 体积 | 相对散文 |
| :--- | ---: | ---: |
| 散文载体 `MEMORY.md`（B2） | 6.1 KB | 1.0× |
| tmem 渲染 `MEMORIES.md`（A1，**交付物**） | 3.8 KB | **0.6×** |
| 支撑它的 `.tmem`（磁盘） | 2 934.3 KB（3 004 717 B） | **478.2×** |
| 同上，剔除可重建缓存 | 626.3 KB（641 325 B） | **102.1×** |

**tmem 让"被读到的记忆"比散文更小（省上下文 token、命中更快），但让"存着的记忆"大两个数量级（费磁盘）。** 两件事不矛盾：前者是读路径的优化，后者是写路径的代价。

### 4.3 有界性：默认只降级、不回收

`tmem/cli.mjs:329-332` 的默认参数：`maxEpisodesMb: 200`、`maxEpisodeDays: 30`、`prune: false`。

`store.mjs:919-975` 的 `runMetabolismGc(pruneStale)` 语义：

1. 守卫失效的 playbook 先被标记 `stale` 并下调 confidence（可降级为 `degraded_l1_l2`）；
2. **只有 `pruneStale` 为真（即显式 `--prune`）**，且满足 `l2_causal_repairs` 为空 **且**（`guard_fail_streak ≥ 3` 或 `confidence ≤ 0.1`）时，才 `unlink` 轨迹 JSON、删除 raw 留档、并从 `playbooks`/`exact_triggers`/`playbooks_fts`/`trace_links`/`inverted_postings` 同步删除。

**结论：默认不会自动回收；200 MB / 30 天是阈值而非触发器。**

---

## 5. 实操建议

1. **`.tmem/raw/` 值得重新决策**——它是唯一「体积大（105 KB/条）且默认进版本库」的层。若不希望仓库随会话线性膨胀，把它加入项目 `.gitignore`（信息仍在本地，只是不再入库）。
2. **定期执行 `tmem gc --prune`**——否则只有降级没有回收，磁盘只增不减。
3. **`.tmem/.cache/` 随删**——已被排除在版本库外，且可重建；不必备份。
4. **关注索引重建代价**：删缓存后首次访问会重建索引（代价随 trace 数增长），高峰期删除可能造成一次性卡顿。

---

## 6. 复现

```bash
# 一键重算本文全部数字（脚本分 [1] 单轨迹成本 / [1b] 转录基准 / [2] 两种载体 / [3] 固定开销）
python3 research/storage_overhead/measure_storage_overhead.py

# 单轨迹成本
python3 -c "
from pathlib import Path;import statistics
r=Path('/home/kent/e2e/r3/ledger-svc/.tmem')
for s in ('raw','traces','episodes','.cache'):
    a=[p.stat().st_size for p in (r/s).iterdir() if p.is_file()]
    print(f'{s:10s} n={len(a):3d} 合计 {sum(a)/1024:8.1f} KB 单条 {statistics.mean(a)/1024:7.1f} KB')"

# 索引页占用
python3 -c "
import sqlite3;c=sqlite3.connect('file:/home/kent/e2e/r3/ledger-svc/.tmem/.cache/index.db?mode=ro',uri=True)
print(c.execute('select name,sum(pgsize) from dbstat group by name order by 2 desc limit 6').fetchall())"

# 关键源码
sed -n '244,325p' tmem/store.mjs   # 索引从磁盘重建
sed -n '919,975p' tmem/store.mjs   # GC / --prune 语义
sed -n '205,232p' tmem/store.mjs   # .tmem/.gitignore 写入
sed -n '325,360p' tmem/cli.mjs     # gc 的默认阈值
```

测量输出留档：`/home/kent/e2e/_harness/storage-overhead-measurement.txt`。

---

## 7. 局限

- **增长数据只有 Round-3 fixture 一个样本**（18 场会话 → 9 条轨迹 → 8.95 MB）。消融各臂因种子库只读挂载，无法测增长。
- **单轨迹成本依赖会话形态**：本项目 `ledger-svc` 的会话中位 113 KB；长会话、多次工具调用、触碰文件多时 `raw/` 与 episode 都会线性变大。**本文的 KB 数不是常数。**
- **未测**：并发多项目、索引重建耗时、`gc --prune` 的实际回收率、数百场规模下的增长曲线、以及 `.tmem/raw/` 与 dsh 自身会话文件的重复度是否随时间收敛。
- 本文只度量**存储**，不度量 I/O 时延；索引体积带来的检索时延未在本次测量范围内。

---

## 8. 与主报告的关系

| 维度 | tmem（插件臂） | 散文载体（B 臂） | 出处 |
| :--- | :--- | :--- | :--- |
| 交付给 agent 的记忆大小 | `MEMORIES.md` 3.8 KB | `MEMORY.md` 6.1 KB | 本文 §4.2 |
| 存储足迹 | 2 934 KB | 6.1 KB | 本文 §4.2 |
| 注入口径的上下文成本 | 1 388 token（注入切片） | 需先检索再读 | 主报告 §3.14 |
| 允许自由查阅的总知识获取成本 | 18 017 token（3.55× 于散文臂） | 5 073 token | 主报告 §3.14 |
| 效果（`zero` 变体命中率） | 1.000 | 0.000 | 主报告 §3.14 / 消融报告 §4.3 |

**一句话**：tmem 用**磁盘**换**上下文 token**与**零检索可得性**——它不是"更轻"的方案，而是把成本从上下文挪到了存储上的方案；是否划算取决于项目对二者的敏感度。
