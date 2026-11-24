# A2 reprep2 zero —— 会话输出（命中率的评分输入）

以下结论来自我上下文里的项目记忆库（tmem 索引与执行轨迹），未读取任何文件、未执行任何命令。

## 1) 修改的文件与顺序

按实际编辑顺序：

| 顺序 | 文件 | 改动 |
|---|---|---|
| 1 | `src/ledger/money.py` | 新增 `import math`、`from .errors import ValidationError`，新增统一守卫 `ensure_usable_price()`，并微调 `format_money` |
| 2 | `src/ledger/import_rows.py` | `parse_price()` / `parse_price_checked()` 改走 `ensure_usable_price()`；`parse_rows()`、`rows_to_tuples()` 传入行描述 |
| 3 | `src/ledger/invoicing.py` | **中途试过**在 `build_invoice()` 里加建单期校验（seq 114/122），后因破坏既有测试语义被**回退**（seq 166/171），最终该文件保持未改 |
| 4 | `src/ledger/settlement.py` | `settle_invoice()` / `settle_line()` 换算前先校验单价；`settle_invoice_checked()` 由 `ValueError` 改为 `ValidationError` |
| 5 | `tests/test_import_rows.py` | 新建该测试文件（14 个回归用例） |
| 6 | `tests/test_settlement.py` | 补端到端用例 `test_headless_import_with_empty_price_cell_is_rejected` 等 |
| 7 | `MEMORY.md` | 写入根因链、改动说明与新的失败基线 |

净变更（`git diff --stat`）为 `import_rows.py` + `money.py` + `settlement.py` + `tests/test_settlement.py`，外加新建的 `tests/test_import_rows.py`。

根因链是分段的，不是单点：**导出端把空单元格写成字面文本 `nan`** → 导入边界 `parse_price()` 直接 `float(raw)` 未校验 → 金额以整数最小单位存储，`Decimal('NaN').quantize(...)` 静默传播 → `int(NaN)` 才炸成 `ValueError: cannot convert NaN to integer`。

## 2) 验证命令

主验证：

```bash
PYTHONPATH=src python3 -m pytest -q 2>&1 | tail -8
```

结果：`2 failed, 39 passed`。剩下的 2 个失败是**与本次修复无关的既有缺陷**（`tests/test_pagination.py::test_pagination_does_not_lose_invoices`、`tests/test_rates.py::test_get_cached_rate_falls_back_to_rate_table`）；本次目标失败（`test_settle_invoice_rejects_missing_price` 的 `ValueError`）已消除。

配套还跑了：

```bash
# 稳定性 + 按文件隔离
for i in 1 2 3; do PYTHONPATH=src python3 -m pytest -q 2>&1 | tail -1; done
for f in tests/test_import_rows.py tests/test_settlement.py tests/test_rates.py \
         tests/test_pagination.py tests/test_invoicing.py tests/test_idempotency.py; do
  echo "--- $f"; PYTHONPATH=src python3 -m pytest "$f" -q 2>&1 | tail -1
done

# 收集用例计数（记忆里登记的验证命令）
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

计数结果：全量 41 collected，`test_import_rows.py` 15 个，`test_settlement.py` 14 个。另用一次性 Python 脚本验证了三层（`parse_price` / `parse_rows` / `parse_price_checked` / `build_invoice` / `build_invoice_from_rows` / `settle_invoice` / `settle_line` / `settle_invoice_checked`）都统一抛 `ledger.errors.ValidationError`，且 `''`、`'   '`、`None`、`'nan'`、`'NaN'`、`'inf'`、`'-inf'`、`'abc'` 全部被拒，`0` 与正常价格仍放行。

## 3) 统一校验入口函数**`ensure_usable_price`**，定义在 `src/ledger/money.py`：

```python
def ensure_usable_price(value, description: str = "line") -> float:
```

它拒绝缺失/非数值/非有限值，统一抛 `ledger.errors.ValidationError`（消息形如 `missing price for hosting`、`invalid price for line: 'abc'`、`non-finite price for line: 'inf'`）。导入边界和结算边界都复用它，即「一道口子、两处调用」的统一入口。
