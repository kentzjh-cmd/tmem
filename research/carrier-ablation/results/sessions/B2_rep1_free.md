# B2 reprep1 free —— 会话输出（命中率的评分输入）

已查阅工作区的记忆载体（`MEMORY.md`；本工作区不存在 `.tmem/`，按 `AGENTS.md` 约定忽略）。以下是三个问题的答案：

## 1) 修复顺序：依次改了哪些文件

记忆里「改动文件（按修复顺序）」明确记录为：

**`src/ledger/money.py` → `src/ledger/import_rows.py` → `src/ledger/settlement.py`**

各文件的角色（记忆中的决策要点）：

| 顺序 | 文件 | 落点 |
|---|---|---|
| 1 | `src/ledger/money.py` | 先落共享的价格守卫（统一校验入口），后续模块都复用它 |
| 2 | `src/ledger/import_rows.py`（导入边界 = 根因） | `parse_price()` 改走 `ensure_usable_price()`；`parse_rows()`/`rows_to_tuples()` 把行描述传进去用于报错定位；`parse_price_checked()` 改为委托而非自己判 NaN |
| 3 | `src/ledger/settlement.py`（结算边界 = 兜底） | `settle_invoice()`/`settle_line()` 在换算前先校验单价；`settle_invoice_checked()` 的 `ValueError` 改为项目的 `ValidationError` |

随后补测试与写记忆：记忆的「涉及文件」清单还包含 `src/ledger/invoicing.py`、`tests/test_import_rows.py`、`tests/test_settlement.py`、`MEMORY.md`（排查结论与新基线回写）。

根因背景：导出端把空数值单元格写成字面文本 `nan`（`import_rows.py` 的 docstring 早已写明），导入时 `float("nan")` 一路传到结算的 `int(...)`，才炸成 `ValueError: cannot convert NaN to integer`。

## 2) 验证命令

- 主验证：`PYTHONPATH=src python3 -m pytest -q 2>&1`
- 单文件回归：`PYTHONPATH=src python3 -m pytest "$f" -q 2>&1`（`$f` 为目标测试文件）
- 记忆里另外记了一条先做收集确认的序列：
  `PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 && PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 && PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1`

（与 `README.md` 的 `PYTHONPATH=src python -m pytest` 一致，只是记忆里用了 `python3` 和 `-q`。）

## 3) 新增的统一校验入口函数名

**`ensure_usable_price()`** —— 定义在 `src/ledger/money.py`，是「统一校验价格是否可用」的唯一入口；`import_rows.parse_price()` 和 `settlement`侧的校验都改为走它。

一点观察（供参考）：当前工作区快照的 `src/ledger/money.py` 里还没有 `ensure_usable_price`，`tests/test_import_rows.py` 也不存在，而 `tests/test_settlement.py:34` 的 `test_settle_invoice_rejects_missing_price` 已经在期望 `ValidationError`——说明这份快照停在**修复前**的基线状态，记忆里记录的是另一个会话中已完成的那次修复。
