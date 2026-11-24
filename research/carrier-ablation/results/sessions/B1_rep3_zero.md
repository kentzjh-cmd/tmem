# B1 reprep3 zero —— 会话输出（命中率的评分输入）

以下答案全部来自本次会话上下文中已有的决策记录，未读取任何文件、未执行任何命令。

## 1) 修改的文件与顺序

按修复顺序（共享校验入口 → 导入边界 → 结算边界）：

1. **`src/ledger/money.py`** — 先加**共享的价格校验护栏**（新增 `ensure_usable_price()`），把"价格是否可用"的判断收敛到一处。
2. **`src/ledger/import_rows.py`** — 根因所在：`parse_price()` 改走 `ensure_usable_price()`；`parse_rows()` / `rows_to_tuples()` 把行描述传下去，好让报错能指明是哪一行；`parse_price_checked()` 改为委托新的校验入口。
3. **`src/ledger/settlement.py`** — 兜底：`settle_invoice()` / `settle_line()` 先校验单价再做整数分换算；`settle_invoice_checked()` 的异常类型由 `ValueError` 改为项目自己的 `ledger.errors.ValidationError`。

配套补测试：`tests/test_import_rows.py`、`tests/test_settlement.py`。（`src/ledger/invoicing.py` 在涉及文件列表中，属于排查时读过/关联过的模块，不在改动文件序列里。）

## 2) 验证命令

主验证（全量）：

```bash
PYTHONPATH=src python3 -m pytest -q 2>&1
```

分文件/收集校验：

```bash
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

以及"全量通过 + 单文件回归"组合的写法：`PYTHONPATH=src python3 -m pytest -q 2>&1 && PYTHONPATH=src python3 -m pytest "$f" -q 2>&1`（`$f` 指具体测试文件）。

## 3) 统一校验入口函数名

**`ensure_usable_price()`**，位于 `src/ledger/money.py`。它是"价格是否可用"的统一入口：`import_rows.parse_price()` 和 `settlement` 的结算路径都改走它，缺失/不可用价格统一抛 `ledger.errors.ValidationError`（如 `missing price for hosting` / `missing price for line`），不再让 `nan` 漏到换算层变成 `ValueError: cannot convert NaN to integer`。
