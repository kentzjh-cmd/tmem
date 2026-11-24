# B1 reprep1 zero —— 会话输出（命中率的评分输入）

按要求，我没有读任何文件、也没有运行任何命令，以下三个答案全部来自当前上下文里的工程决策记录。

---

## 1) 修复 NaN 价格这个坑时，改动文件与顺序

记录里的「改动文件（按修复顺序）」明确写的是：

**`src/ledger/money.py` → `src/ledger/import_rows.py` → `src/ledger/settlement.py`**

对应的三层职责划分：

| 边界 | 文件 | 改动内容 |
|---|---|---|
| 公共守卫（源头） | `src/ledger/money.py` | 新增统一的价格可用性校验入口；`parse_price()` 改走 `ensure_usable_price()` |
| 导入边界（根因） | `src/ledger/import_rows.py:13` | `parse_price()` 改走 `ensure_usable_price()`；`parse_rows()` / `rows_to_tuples()` 传入行描述；`parse_price_checked()` 改为委托 |
| 结算边界（兜底） | `src/ledger/settlement.py:25,65,91` | `settle_invoice()` / `settle_line()` 先校验单价再换算；`settle_invoice_checked()` 由抛 `ValueError` 改为抛 `ValidationError` |

同批还涉及（非核心修复顺序的配套文件）：`tests/test_import_rows.py`、`tests/test_settlement.py`（补测试）、`src/ledger/invoicing.py`（相关路径排查）、`MEMORY.md`（记录排查结论）。

关键点：根因在导入端——`parse_price()` 此前会把空单元格解析成 `nan` 并直接放行；修复后导入边界即用项目自己的校验异常 `ledger.errors.ValidationError`（消息形如 `missing price for hosting` / `missing price for line`）**明确拒绝**缺失价格，结算端再做一层兜底，避免再出现 `ValueError: cannot convert NaN to integer`。

## 2) 验证命令

记录在「验证命令」里的是全量 + 分文件的组合：

```bash
PYTHONPATH=src python3 -m pytest -q 2>&1 && PYTHONPATH=src python3 -m pytest "$f" -q 2>&1
```

其中单文件验证形如 `PYTHONPATH=src python3 -m pytest "$t" -q 2>&1` / `"$f" -q 2>&1`（`$t`/`$f` 为对应测试文件）。

另外记录了一条「出错/验证命令」用于全量基线：

```bash
PYTHONPATH=src python3 -m pytest -q 2>&1
```

以及一次 collect-only 的收集健全性检查（防止用例根本没被收集而假绿）：

```bash
PYTHONPATH=src python3 -m pytest --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_import_rows.py --collect-only -q 2>&1 && \
PYTHONPATH=src python3 -m pytest tests/test_settlement.py --collect-only -q 2>&1
```

## 3) 新增的「统一校验价格是否可用」入口函数名

**`ensure_usable_price()`** —— 定义在 `src/ledger/money.py`。

它是那层公共守卫：导入端 `parse_price()` / `parse_price_checked()` 都改为走它，结算端在换算前也调用它做兜底。校验失败时抛的是项目自己的异常 `ledger.errors.ValidationError`，消息携带行描述（如 `missing price for line`），而不是原来的 `ValueError: cannot convert NaN to integer`。

---

补充一条上下文里已记录的相关背景（与上面三问同源）：本项目所有金额一律以**整数最小单位**存储，因为上游对账系统只接受整数分——这条是**不可变更的外部硬约束**，不是内部风格偏好，精度约定为 CNY/USD/EUR = 2 位、JPY = 0 位；相应地导出端会把空数值单元格写成字面文本 `nan`（`import_rows.py` 的 docstring 早已写明），这正是本次 NaN 坑的来源。
