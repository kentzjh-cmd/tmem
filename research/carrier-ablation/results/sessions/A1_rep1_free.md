# A1 reprep1 free —— 会话输出（命中率的评分输入）

已从项目结构化记忆库（`.tmem/`，episode `ep_e98083fe22` / trace `tr_01aed48efa`，session `session-4bd4516f-…`）取回当时的完整执行路径。全程未运行任何命令。

## 1) 依次修改的文件与顺序

**根因链**（当时定位到的，不是单点）：导出端把空数值单元格写成字面文本 `nan` → `import_rows.py` 的 `parse_price()` 直接 `float(raw)`，`float('nan')` 不报错，NaN 静默穿过 `Decimal.quantize()` → 直到结算里 `int(converted.quantize(...) * 100)` 才炸成 `ValueError: cannot convert NaN to integer`。

修改顺序（源文件由内向外，先立守卫再改调用方）：

|顺序 | 文件 | 改动 |
|---|---|---|
| 1 | `src/ledger/money.py` | 新增共享守卫 `ensure_usable_price()`（第 55 行），并在此文件补 `import math` / `from .errors import ValidationError` |
| 2 | `src/ledger/import_rows.py` | 导入边界（根因）：`parse_price(raw, description)` 改走 `ensure_usable_price()`；`parse_rows()` / `rows_to_tuples()` 把行描述传进去；`parse_price_checked()` 改为委托，不再自己抛 `ValueError` |
| 3 | `src/ledger/settlement.py` | 结算边界（兜底）：`settle_invoice()` / `settle_line()` 先校验单价再换算；`settle_invoice_checked()` 由 `ValueError` 改为 `ValidationError` |
| 4 | `src/ledger/invoicing.py` | 中途在 `build_invoice` 里试加过守卫，但改变了既有测试的语义（项目约定是**结算侧**拒绝缺失价格），遂回退 —— 最终 diff 里没有这个文件 |
| 5 | `tests/test_import_rows.py`（新建）+ `tests/test_settlement.py` | 新建15 个用例；结算侧加 `test_headless_import_with_empty_price_cell_is_rejected`、`test_settle_invoice_checked_rejects_missing_price_with_validation_error` 等回归用例 |
| 6 | `MEMORY.md` | 写入根因链、边界表与新基线 |

最终 `git status` 只有：`src/ledger/import_rows.py`、`src/ledger/money.py`、`src/ledger/settlement.py`、`tests/test_settlement.py` 被修改，`tests/test_import_rows.py` 新增。

## 2) 验证用的命令

- 全量套件：`PYTHONPATH=src python3 -m pytest -q` → `2 failed, 39 passed`（剩的 2 个是既有的、与本修复无关的 `test_pagination.py::test_pagination_does_not_lose_invoices` 和 `test_rates.py::test_get_cached_rate_falls_back_to_rate_table`，属原基线）
- 按文件隔离：`for f in tests/test_import_rows.py tests/test_settlement.py tests/test_rates.py tests/test_pagination.py tests/test_invoicing.py tests/test_idempotency.py; do PYTHONPATH=src python3 -m pytest "$f" -q; done` → `test_import_rows.py` 15 passed、`test_settlement.py` 14 passed
- 稳定性连跑：`for i in 1 2 3; do PYTHONPATH=src python3 -m pytest -q; done` → 三次结果一致
- 收集数核对：`PYTHONPATH=src python3 -m pytest --collect-only -q`，以及 `PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q`、`PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q`（41 / 15 / 14 collected）
- 另外用内联 `PYTHONPATH=src python3 - <<'PY'` 脚本逐个断言 `parse_price / parse_rows / parse_price_checked / build_invoice / build_invoice_from_rows / settle_invoice / settle_line / settle_invoice_checked` 都抛 `ledger.errors.ValidationError`（含 `""`、`"   "`、`None`、`"abc"`、`"inf"`、`"-inf"`、`"nan"`、`"NaN"` 各变体）

记忆库里记录的 playbook verify 串是：
`PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 && PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 && PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1`

## 3) 新增的“统一校验价格是否可用”入口

**`ensure_usable_price(value, description: str = "line") -> float`**，定义在 **`src/ledger/money.py:55`**，非法时抛 `ledger.errors.ValidationError`：

- `None` / 空白字符串 → `missing price for {description}`
- 非数字文本 → `invalid price for {description}: {value!r}`
- `math.isnan` → `missing price for {description}`
- `math.isinf` → `non-finite price for {description}: {value!r}`

`import_rows.py`（导入边界）和 `settlement.py`（结算边界）都统一改走这个入口，错误信息里带上行描述（如 `missing price for hosting`）。
