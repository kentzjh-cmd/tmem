# 载体消融课题 · 机器判定结果

> 预注册时间：2026-11-23T22:53:27+08:00（阈值与提示词执行前冻结）｜ 装置：`/home/kent/e2e/_harness/ablation`

## 逐臂结果（free 变体 = 允许查阅项目记忆）

| 臂 | 载体/获取 | 有效样本 | 知识获取 token（均值, 95%CI） | 注入 token | 读取 token | 读取次数 | 命中率（均值, 95%CI） |
| :--- | :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| A1 | tmem / 自动注入 | 3 | 18016.6667 (10184.7322–25848.6011) | 1388 | 16628.6667 | 0.6667 | 0.889 (0.4114–1.3666) |
| A2 | tmem / 按需检索 | 3 | 14250.3333 (8659.256–19841.4107) | 0 | 14250.3333 | 1.3333 | 1.0 (1.0–1.0) |
| B1 | 散文 MD / 全量注入 | 3 | 7112.3333 (3812.3396–10412.3271) | 2181.3333 | 4931 | 5 | 1.0 (1.0–1.0) |
| B2 | 散文 MD / 按需读取 | 3 | 5073 (1847.0882–8298.9118) | 0 | 5073 | 5.6667 | 1.0 (1.0–1.0) |
| C | 无记忆 | 2 | 8775 (1568.0567–15981.9433) | 0 | 8775 | 13.5 | 0.0 (0.0–0.0) |

## 描述性统计（不构成门槛判定）

```
{
  "injection_only_ratio": 0.2736,
  "free_variant_total_ratio": 3.5515,
  "A1_free_mean_injected_tokens": 1388,
  "A1_free_mean_knowledge_tokens": 18016.6667,
  "B2_free_mean_knowledge_tokens": 5073,
  "read_output_tokens_by_category": {
    "A1": {
      "carrier": 16169,
      "code": 307,
      "other": 149.3
    },
    "A2": {
      "carrier": 13161.7,
      "code": 614,
      "other": 470.3
    },
    "B1": {
      "carrier": 2168,
      "code": 2291.3,
      "other": 467.3
    },
    "B2": {
      "carrier": 2168,
      "code": 2428,
      "other": 472.3
    },
    "C": {
      "carrier": 0,
      "code": 6532,
      "other": 2233
    }
  },
  "note": "carrier = 读取记忆载体（MEMORY.md / MEMORIES.md / .tmem）；code = 读取源码与测试；other = 其余工具输出（如命令回显）。知识获取 token 的正式口径把三者都计入，因此 free 变体的绝对值含了解题成本。"
}
```


## 门槛判定

| 门槛 | 判定 | 值 | 阈值 |
| :--- | :---: | :--- | :--- |
| CA-5 载体等价性（前置门） | ✅ PASS | `{"episodes": 4, "facts": [{"id": "Q1_file_order", "ask": "Q1_file_order", "groups": [["money.py"], ["import_rows.py"], ["settlement.py"]], "ordered": true, "…` | 所有关键点 in_tmem_carrier AND in_md_carrier |
| CA-1 成本优势（主判据） | ❌ FAIL | `{"A1_free_mean_knowledge_tokens": 18016.6667, "B2_free_mean_knowledge_tokens": 5073, "ratio": 3.5515}` | mean(A1_free.knowledge_tokens) <= 0.30 * mean(B2_free.knowledge_tokens) |
| CA-1b 成本优势（注入式 vs 散文全量注入） | ❌ FAIL | `{"A1_free_mean_knowledge_tokens": 18016.6667, "B1_free_mean_knowledge_tokens": 7112.3333, "ratio": 2.5332}` | mean(A1_free.knowledge_tokens) <= 0.30 * mean(B1_free.knowledge_tokens) |
| CA-4 获取方式效应可分离 | ❌ FAIL | `{"knowledge_tokens": {"A1": 18016.6667, "A2": 14250.3333, "B1": 7112.3333, "B2": 5073}, "hit_rate": {"A1": 0.889, "A2": 1.0, "B1": 1.0, "B2": 1.0}}` | A1_free.knowledge_tokens <= A2_free.knowledge_tokens AND B1_free.knowledge_tokens <= B2_free.knowledge_tokens AND A1_free.hit_rate >= A2_free.hit_rate AND B1_free.hit_rate >= B2_free.hit_rate |
| CA-E1 效果优势（记忆专有事实） | ✅ PASS | `{"variant": "zero", "A1_zero_hit_rate": 1.0, "C_zero_hit_rate": 0.0, "B2_zero_hit_rate": 0.0, "free_variant_hit_rate_descriptive": {"A1": 0.889, "A2": 1.0, "…` | mean(A1_zero.hit_rate) > mean(C_zero.hit_rate) AND mean(A1_zero.hit_rate) >= mean(B2_zero.hit_rate) |
| CA-2 同签名复发率 | — | `None` | 未预注册具体阈值 |
| CA-3 L1 共变提示采纳精确率 | — | `None` | 未预注册具体阈值 |
| CA-6 压缩后回收 | — | `None` | 未预注册具体阈值 |
| CA-7 CA-7 | — | `None` | None |
| CA-8 CA-8 | — | `None` | None |
| CA-9 CA-9 | — | `None` | None |

## 作废样本（机械判定，不允许人工挑选）

- **C rep3 free**：out_of_arm_read:/home/kent (glob)；out_of_arm_read:/home/kent/abl2 (glob)；out_of_arm_read:/home/kent (grep)；out_of_arm_read:/home/kent (bash)；out_of_arm_read:/home/kent/abl2 (bash)；out_of_arm_read:/archive (bash)；out_of_arm_read:/archive/pentest_work_flow (bash)；out_of_arm_read:/archive/pentest_work_flow/dsh_secops_plugin (bash)；out_of_arm_read:/archive (grep)
