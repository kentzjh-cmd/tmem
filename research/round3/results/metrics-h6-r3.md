# H6' memory-exclusive probe (Round 2, disclosed addition)

| Arm | Variant | hit rate | injected | inj. tokens | read calls | read tokens |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| A_plugin_memory | zero | 0.333 | True | 1874 | 0 | 0 |
| A_plugin_memory | free | 0.333 | True | 1878 | 4 | 46544 |
| B_no_plugin | zero | 0.0 | False | 0 | 0 | 0 |
| B_no_plugin | free | 0.333 | False | 0 | 21 | 25408 |
| C_plugin_no_session | zero | 0.0 | False | 0 | 0 | 178 |
| C_plugin_no_session | free | 0.333 | False | 0 | 8 | 70941 |

## Gates

- **H6'-1_availability**: 记忆专有事实的可得性（A 零读取臂 vs B/C 零读取臂） — value={"A_zero": 0.333, "B_zero": 0.0, "C_zero": 0.0} threshold=`A_zero >= B_zero + 0.30 且 A_zero >= C_zero + 0.30` → **True**
- **H6'-2_cost**: 获取成本（A 零读取臂 vs A 允许读取臂） — value={"A_zero_injected_tokens": 1874, "A_free_read_tokens": 46544, "A_free_read_calls": 4, "B_free_read_tokens": 25408, "B_free_read_calls": 21} threshold=`A_zero 的知识获取 token <= B_free 的 0.30 倍` → **True**
