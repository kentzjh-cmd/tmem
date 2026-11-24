# B1 reprep2 free —— 会话输出（命中率的评分输入）

我查阅了工作区里的项目记忆载体（`AGENTS.md` 指定的散文载体 `MEMORY.md`；`.tmem/` 结构化库在本工作区不存在，故忽略）。以下三个答案都出自 `MEMORY.md` 中「headless 导入的表格里价格单元格为空时会被写成 nan…」那条决策记录（MEMORY.md:24-38）。

## 1) 依次修改的文件与顺序

记录里明确写的是「改动文件（按修复顺序）」：

**`src/ledger/money.py` → `src/ledger/import_rows.py` → `src/ledger/settlement.py`**

- **money.py（先落共享守卫）**：决策要点原文是「Root cause is fully mapped. Now let me implement the fix, starting with a shared guard in `money.py`」——先把「价格是否可用」的统一校验集中到 money 层。
- **import_rows.py（根因边界）**：`src/ledger/import_rows.py:13`，`parse_price()` 改走 `ensure_usable_price()`；`parse_rows()` / `rows_to_tuples()` 一并传入行描述；`parse_price_checked()` 改为委托该守卫（不再自己判 NaN）。
- **settlement.py（兜底边界）**：`src/ledger/settlement.py:25,65,91`，`settle_invoice()` / `settle_line()` 先校验单价再换算；`settle_invoice_checked()` 的异常由 `ValueError` 改为 `ValidationError`。

配套还涉及 `src/ledger/invoicing.py`、`tests/test_import_rows.py`、`tests/test_settlement.py`（补测试），以及 `MEMORY.md`（沉淀结论）。

根因链条记忆中也留了原文：**headless 导出把空的数值单元格写成字面文本 `nan`**（`import_rows.py` 的 docstring 早已写明），导入时 `float('nan')` 直接混进单价，直到结算 `int(...)` 才炸成 `ValueError: cannot convert NaN to integer`。

## 2) 验证命令

主验证命令（全量套件）：

```
PYTHONPATH=src python3 -m pytest -q 2>&1
```

组合形式记录为：`PYTHONPATH=src python3 -m pytest -q 2>&1 && PYTHONPATH=src python3 -m pytest "$f" -q 2>&1`（即全量通过后，再对受影响测试文件单独跑一遍）。另外还用过收集层面的检查：

```
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1
PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1
PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

## 3) 新增的统一校验入口函数名

**`ensure_usable_price()`**，定义在 `src/ledger/money.py`，是 money 层的共享守卫；`import_rows.parse_price()` 改走它、`settlement` 侧先校验再换算。缺失价格统一抛项目自己的异常 `ledger.errors.ValidationError`（记忆中出现的签名：`ValidationError: missing price for hosting` / `ValidationError: missing price for line`）。

---

⚠️ 一点需要提醒的不一致：当前工作区的**代码树还停留在修复前状态**——`src/ledger/money.py` 里没有 `ensure_usable_price`，`import_rows.py` 的 `parse_price_checked()` 仍抛 `ValueError`，`settlement.py` 的 `settle_invoice_checked()` 也仍是 `ValueError`。全仓库检索 `ensure_usable_price` 只命中 `MEMORY.md`。也就是说：**记忆里记录的是那次修复的决策，但对应代码改动不在这份工作副本中**（可能是未提交/已回退/换了分支）。如果你需要代码也处于修复后状态，告诉我，我可以把这套改动落到当前树上并跑一遍测试验证。
