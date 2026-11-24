# A1 reprep1 zero —— 会话输出（命中率的评分输入）

根据我上下文里已有的执行记忆（trace `tr_01aed48efa`），三个问题的答案如下（**未读文件、未执行任何命令**，全部来自上下文中的记录）：

## 1) 依次修改的文件与顺序

根因是分段的（导入层写入了 `nan` → 结算层拿 `nan` 转 int 才炸），所以修复是三段连锁改动：

| 顺序 | 文件 | 改动的符号 | 作用 |
|---|---|---|---|
| 1 | `src/ledger/money.py` | `ensure_usable_price`（新增）、`format_money` | money 层加统一的价格可用性闸门，拦住 NaN/不可用价格 |
| 2 | `src/ledger/import_rows.py` | `parse_price`、`parse_price_checked`（新增）、`parse_rows` | 导入层空价格单元格不再写成 `nan`，改走 checked 路径 |
| 3 | `src/ledger/settlement.py` | `settle_invoice` | 结算层改用校验后的价格，缺失价格用项目自己的校验异常明确拒绝 |

另外还有一步不在代码链上的收尾：**`MEMORY.md`**（把根因链和验证方式记进项目记忆载体）。所以顺序是 `money.py → import_rows.py → settlement.py → MEMORY.md`。

## 2) 验证命令

最终收口用的是 pytest 收集 + 分文件回归的三段串联：

```bash
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

过程中实际跑测试用的是同一形式的全量 + 单文件：

```bash
PYTHONPATH=src python3 -m pytest -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest "$f" -q 2>&1
```

新增的回归用例包括 `tests/test_settlement.py::test_headless_import_with_empty_price_cell_is_rejected` 和 `::test_settle_invoice_checked_rejects_missing_price_with_validation_error`。

## 3) 新增的“统一校验价格是否可用”入口函数

**`ensure_usable_price`** —— 位于 `src/ledger/money.py`，签名记为 `ensure_usable_price(value, description)`。

它是唯一的统一入口：money 层定义语义（价格是否可用的判定 + 抛项目自己的校验异常），`parse_price_checked` 只是 import_rows 里对它的包装调用点，`settle_invoice` 再往上调用。

> 说明：记忆标注该 trace 的 L0 具体路径有轻微漂移（`ensure_usable_price` / `parse_price` 的签名与个别测试文件路径已不在原位），所以**函数名与修改顺序是可靠的**，但若要确认当前的确切签名和位置，需要我实际读一下 `src/ledger/money.py`。需要的话我可以去核对。
