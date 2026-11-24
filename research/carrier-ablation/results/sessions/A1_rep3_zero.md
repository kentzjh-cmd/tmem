# A1 reprep3 zero —— 会话输出（命中率的评分输入）

我直接依据当前上下文里的历史执行轨迹（trace `tr_01aed48efa`）作答，没有读取文件、没有运行命令。

## 1) 修改的文件与顺序

因果修复链是**三步、自底向上**：

```
1. src/ledger/money.py      —— ensure_usable_price（新增）、format_money
2. src/ledger/import_rows.py —— parse_price、parse_price_checked、parse_rows
3. src/ledger/settlement.py  —— settle_invoice
```

即：先在**最底层的 money 模块**建立统一的「价格可用性」校验点，再让**导入层**（`parse_price_checked` → `parse_rows`）走这个校验，最后让**结算层**（`settle_invoice`）用带校验的入口把缺失价格明确拒绝掉，而不是让它带着 `nan` 流到结算才炸成 `ValueError: cannot convert NaN to integer`。

轨迹另记 `read 8 file(s) -> edited 6 file(s)`，除上述 3 个源文件外，还落笔了测试与记忆：`tests/test_import_rows.py`（新增）、`tests/test_settlement.py`（新增 `test_headless_import_with_empty_price_cell_is_rejected`、`test_settle_invoice_checked_rejects_missing_price_with_validation_error`），以及把排查结论写进 `MEMORY.md`。

## 2) 验证命令

全部用 `PYTHONPATH=src` 跑 pytest，分三层：

```bash
# 全量
PYTHONPATH=src python3 -m pytest -q 2>&1

# 逐文件回归
PYTHONPATH=src python3 -m pytest "$f" -q 2>&1

# 收集校验（确认新测试能被正确收集、无导入错误）
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 \
  && PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

轨迹记录结果为 `repaired 1 error(s) -> verified`。

## 3) 统一的「校验价格是否可用」入口函数

**`ensure_usable_price`**，位于 `src/ledger/money.py`。

它是唯一的判定入口：导入层的 `parse_price_checked` 负责把它接进 `parse_rows` 的解析流程，结算层的 `settle_invoice` 则用带校验的入口调用，缺失/不可用价格在这一层就被项目自己的校验异常挡下，不再以 `nan` 的形式流入后续的整数化转换。

一点说明：轨迹的 drift 备注里出现过 `ensure_usable_price(value, description)` 的签名漂移记录，说明该函数的具体参数形态在后续可能有微调；上面第 1 点的三文件顺序和第 3 点的函数名是轨迹中稳定记录下来的结论。
