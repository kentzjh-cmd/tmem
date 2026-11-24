# B2 reprep3 free —— 会话输出（命中率的评分输入）

我查阅了项目记忆载体（工作区里只有散文载体 `MEMORY.md`，没有 `.tmem/` 结构化库），对应记录在 MEMORY.md:24-38 那条『导入表格价格是 nan』的条目中。全程没有执行任何命令，只读了记忆文件和源码。

## 1) 依次修改的文件与顺序

记忆里明确记录：**`src/ledger/money.py` → `src/ledger/import_rows.py` → `src/ledger/settlement.py`**（MEMORY.md:29 的"改动文件（按修复顺序）"）。

配套涉及文件（MEMORY.md:30）：`tests/test_import_rows.py`、`tests/test_settlement.py`、`src/ledger/invoicing.py`、`src/ledger/money.py`、`src/ledger/import_rows.py`、`src/ledger/settlement.py`，以及最后回写记忆的 `MEMORY.md`。

分工是三层边界：
- **money.py（共享守卫）**：先加统一的价格可用性校验，被后面的两层复用。
- **import_rows.py（根因边界）**：`parse_price()` 改走统一校验；`parse_rows()`/`rows_to_tuples()` 传入行描述；`parse_price_checked()` 改为委托该校验（MEMORY.md:37）。
- **settlement.py（兜底边界）**：`settle_invoice()`/`settle_line()` 先校验单价再换算；`settle_invoice_checked()` 的异常由 `ValueError` 改为 `ValidationError`（MEMORY.md:38）。

## 2) 验证命令

主验证命令（MEMORY.md:31）：

```
PYTHONPATH=src python3 -m pytest -q 2>&1 && PYTHONPATH=src python3 -m pytest "$f" -q 2>&1
```

其中第二条是针对单个测试文件的定向重跑（`$f` 指向具体测试文件）。另外记忆里还记了一条收集阶段的核对命令（MEMORY.md:32）：

```
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

## 3) 新增的统一校验入口函数

**`ensure_usable_price()`**，位于 `src/ledger/money.py`。

它就是那个"统一校验价格是否可用"的单一入口：导入侧 `parse_price()` / `parse_price_checked()` 走它，结算侧 `settle_invoice()`/`settle_line()` 也先调它再换算金额；命中缺失价格（导出端把空单元格写成字面文本 `nan`）时抛项目自己的 `ledger.errors.ValidationError`，消息形如 `missing price for hosting` / `missing price for line`，从而把原先的 `ValueError: cannot convert NaN to integer` 换成明确拒绝。

⚠️ 一点提示：当前工作区的 `src/ledger/money.py`（49 行）里**还没有** `ensure_usable_price()`，`import_rows.py` 也仍是修复前的 `float(raw)` 实现——说明现在这份检出处于修复前的基线状态，修复记录只存在于项目记忆里。如果需要把该修复重新落到代码上，我可以按记忆中的顺序（money.py → import_rows.py → settlement.py）重新实现并跑同样的验证命令。
