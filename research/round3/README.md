# Round 3 主数据集判定产物（归档）

本目录是 **Round 3（用修复后的插件重生成的主数据集）** 的机器判定留档，对应
[`docs/dsh-independent-e2e-report.md`](../../docs/dsh-independent-e2e-report.md) §3.13。

| 文件 | 内容 |
| :--- | :--- |
| `metrics-r3-first-pass-dirty-tree.json` / `.md` | **第一次判定**（2026-11-24 00:08）：6 / 9 通过，G9 判 FAIL——`repo_tracked_dirty = [' M docs/dsh-memory-carrier-ablation.md']`，即**研究者本人在会话运行窗口内正在撰写的研究文档**，与任何会话无关 |
| `metrics-r3-recheck-clean-tree.json` / `.md` | **干净工作树上的同脚本同阈值复算**（2026-11-24 20:53）：**7 / 9 通过**，G9 判 PASS（`repo_tracked_dirty = []`，`repo_untracked = 0`） |
| `metrics-h6-r3.json` / `.md` | 披露式追加测量 H6′ 的判定：两个门槛均 PASS（可得性 A_zero 0.333 vs B/C 0.000；成本 1 874 ≤ 0.30 × 25 408） |

两次判定的差异**只在 G9**，其余八道门槛的取值逐项相同。G9 检查的是"仓库 tracked 无意外改动"——它对**研究者在运行期间是否碰过仓库**敏感，因此报告把"会话是否污染了仓库"（由 B 臂逐场断言、watcher 与 `analyze_arm_contamination.py` 独立证明）与"G9 判定"分开陈述。

复现命令（需要 Round 3 的原始 transcript，见 `/home/kent/e2e/_harness/raw-r3/`）：

```bash
cd /home/kent/e2e/_harness && \
EXP_ROOT=/home/kent/e2e/r3 E2E_RAW=/home/kent/e2e/_harness/raw-r3 \
E2E_METRICS_JSON=metrics-r3-recheck.json E2E_METRICS_MD=metrics-r3-recheck.md \
E2E_H6_JSON=metrics-h6-r3-recheck.json E2E_H6_MD=metrics-h6-r3-recheck.md \
bash run_scoring.sh
```

> 注意：`EXP_ROOT` 与 `E2E_RAW` 必须显式给，否则脚本会读到 Round 2 的数据——这是上一轮已记录过的坑。
