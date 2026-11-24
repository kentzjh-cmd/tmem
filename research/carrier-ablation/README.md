# 载体消融课题 · 装置快照（research/carrier-ablation）

本目录是**产出报告数字的那一份装置代码的版本化快照**（与执行环境
`/home/kent/e2e/_harness/ablation/` 中的文件逐字相同）。报告见
[`docs/dsh-memory-carrier-ablation-report.md`](../../docs/dsh-memory-carrier-ablation-report.md)，
协议见 [`docs/dsh-memory-carrier-ablation.md`](../../docs/dsh-memory-carrier-ablation.md)。

## 文件清单

| 文件 | 作用 | 阶段 |
| :--- | :--- | :--- |
| `oracle_carrier_ablation.json` | **预注册 v1.1**（原始冻结版，阈值与提示词） | Phase-1 |
| `oracle_carrier_ablation_v1.2.json` | **预注册 v1.2**（Phase-1b：四条装置修订，阈值逐字未变） | Phase-1b |
| `build_arms.py` | 建 5 臂 × 3 重复工作区、隔离 DSH_HOME、A2 机制消融补丁、PATH shim | Phase-1 |
| `render_prose_carrier.py` | 从 tmem 载体的 episode 记录**机械渲染**散文载体并校验等价性（CA-5） | 两阶段 |
| `md_carrier_plugin.mjs` | B1 臂的散文注入器（不含 tmem 代码、不注册工具）；**每用户轮注入一次**（v1.2 起与 tmem 节奏对齐） | v1.2 |
| `md_carrier_plugin.test.mjs` | 注入器零成本回归测试（9 项，含 Phase-1 缺陷的回归用例） | v1.2 |
| `mount_check.py` | Phase-1 的五臂挂载自检（宿主环境） | Phase-1 |
| `run_ablation.py` | Phase-1 执行器（宿主环境、共享 DSH_HOME） | Phase-1 |
| `Dockerfile.abl` | 隔离镜像 `tmem-abl:iso`（ubuntu:22.04 + git/python3/pytest，uid 1000 非 root） | Phase-1b |
| `run_phase1b.py` | Phase-1b 执行器：每会话独立容器 + 独立 DSH_HOME + 工作区卫生 + 关键点扫描 + 429 重试 | Phase-1b |
| `score_carrier_ablation.py` | 机器判定器（v1.2：含**越界读取自动作废** A7、`--oracle` / `--out-json` / `--out-md`） | v1.2 |
| `verify_report_numbers.py` | **报告数值一致性校验器**：把报告里印出的每个数字从机器判定里重新推导一遍，并断言逐字出现；顺带检查 v1.2 相对 v1.1 无阈值漂移 | v1.2 |

## 路径说明（重要）

脚本里带**绝对路径**，指向执行环境：

- `HARNESS = /home/kent/e2e/_harness`（证据与装置的规范位置）
- `ABL = /home/kent/e2e/_harness/ablation`（装置目录）
- Phase-1b 的执行根目录 `/home/kent/abl2`（**刻意放在 `/home/kent/e2e` 之外**，
  这样容器能用挂载白名单把 `/home/kent/e2e` 整体遮蔽）

在别的机器上复用时，改这三处常量即可；判定器支持
`--exp-root` / `--oracle` / `--out-json` / `--out-md` 参数，不必改代码。

## 仓库内随附的判定产物（`results/`）

为了让"预注册 → 装置 → 判定"三者同处一仓、可被第三方独立复核，`results/` 随附：

| 文件 | 内容 |
| :--- | :--- |
| `metrics-carrier-ablation-v1.2.json` / `.md` | **Phase-1b（报告口径）** 的机器判定：30 场逐场明细、逐臂聚合与 95%CI、5 个门槛判定、描述性统计、每场的越界审计 |
| `metrics-carrier-ablation.json` / `.md` | **Phase-1（已隔离存）** 的同口径判定，用于 §4.5 的两阶段对照 |
| `isolation_probe.txt` / `isolation_checks.json` | 容器内隔离探针输出：敏感路径（`/home/kent/e2e`、`/home/kent/.dsh`、`/archive/tmem/docs`、其它臂）在会话视图里**不存在** |
| `mount_check.json` | 五臂挂载自检（A1/A2 有 tmem 工具无注入、B1 有散文注入无 tmem 工具、B2/C 全清） |
| `sessions/<臂>_rep<n>_<变体>.md` | 30 场会话的**最终回答文本**——命中率的评分输入（判定脚本就是对它打分） |

原始 transcript（zstd 帧式会话日志，用于 token 计量与工具调用审计）体积较大，保留在执行环境 `/home/kent/abl2/homes/<会话>/sessions/`，路径由 `results/metrics-carrier-ablation-v1.2.json` 的 `sessions[].isolation` 字段与每场 `raw/*/scene.json` 的 `docker_argv` 字段指明。

> 第三方复核的最小路径：读 `oracle_carrier_ablation_v1.2.json` 的阈值 → 对 `results/sessions/*.md` 用同一套关键点做命中判定 → 与 `results/metrics-carrier-ablation-v1.2.json` 的 `gates` 对照。

## 证据位置

| 内容 | 路径 |
| :--- | :--- |
| 机器判定（Phase-1b，报告口径） | `/home/kent/e2e/_harness/abl-out/metrics-carrier-ablation-v1.2.json` / `.md` |
| 机器判定（Phase-1，已隔离存） | `/home/kent/e2e/abl/metrics-carrier-ablation.json` / `.md` |
| 容器内隔离探针 | `/home/kent/e2e/_harness/abl-out/isolation_probe.txt`、`isolation_checks.json` |
| 五臂挂载自检 | `/home/kent/e2e/_harness/abl-out/mount_check.json` |
| 每场会话原始记录（含容器挂载白名单） | `/home/kent/abl2/raw/<臂>/rep<n>/<变体>/scene.json` |
| Phase-1b 原始会话 transcript | `/home/kent/abl2/homes/<会话>/sessions/...` |
| Phase-1 原始会话 transcript | `/home/kent/e2e/abl/homes/{plain,md}/sessions/...` |

## 零成本自检

```bash
node research/carrier-ablation/md_carrier_plugin.test.mjs       # 期望 11/11
python3 research/carrier-ablation/verify_report_numbers.py      # 期望 PASS（报告数值 vs 机器判定）
```
