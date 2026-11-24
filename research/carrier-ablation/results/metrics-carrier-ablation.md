# 载体消融课题 · 机器判定结果

> 预注册时间：2026-11-23T22:53:27+08:00（阈值与提示词执行前冻结）｜ 装置：`/home/kent/e2e/_harness/ablation`

## 逐臂结果（free 变体 = 允许查阅项目记忆）

| 臂 | 载体/获取 | 有效样本 | 知识获取 token（均值, 95%CI） | 注入 token | 读取 token | 读取次数 | 命中率（均值, 95%CI） |
| :--- | :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| A1 | tmem / 自动注入 | 3 | 21812.3333 (-3730.0691–47354.7357) | 1387 | 20425.3333 | 1.3333 | 1.0 (1.0–1.0) |
| A2 | tmem / 按需检索 | 3 | 21640.6667 (-2906.2112–46187.5445) | 0 | 21640.6667 | 0 | 0.889 (0.4114–1.3666) |
| B1 | 散文 MD / 全量注入 | 3 | 17810.3333 (8932.6266–26688.04) | 12356.6667 | 5453.6667 | 5.3333 | 1.0 (1.0–1.0) |
| B2 | 散文 MD / 按需读取 | 3 | 5065 (4823.9362–5306.0638) | 0 | 5065 | 4.3333 | 0.889 (0.4114–1.3666) |
| C | 无记忆 | 3 | 52889 (40910.4094–64867.5906) | 0 | 52889 | 12.3333 | 0.7777 (-0.179–1.7343) |

## 描述性统计（不构成门槛判定）

```
{
  "injection_only_ratio": 0.2738,
  "free_variant_total_ratio": 4.3065,
  "A1_free_mean_injected_tokens": 1387,
  "A1_free_mean_knowledge_tokens": 21812.3333,
  "B2_free_mean_knowledge_tokens": 5065,
  "read_output_tokens_by_category": {
    "A1": {
      "carrier": 19377.3,
      "code": 805.3,
      "other": 239
    },
    "A2": {
      "carrier": 19051,
      "code": 0,
      "other": 2586.3
    },
    "B1": {
      "carrier": 2182,
      "code": 2836.3,
      "other": 433
    },
    "B2": {
      "carrier": 2182,
      "code": 2481,
      "other": 389.7
    },
    "C": {
      "carrier": 10243,
      "code": 2014.7,
      "other": 40289
    }
  },
  "note": "carrier = 读取记忆载体（MEMORY.md / MEMORIES.md / .tmem）；code = 读取源码与测试；other = 其余工具输出（如命令回显）。知识获取 token 的正式口径把三者都计入，因此 free 变体的绝对值含了解题成本。"
}
```


## 门槛判定

| 门槛 | 判定 | 值 | 阈值 |
| :--- | :---: | :--- | :--- |
| CA-5 载体等价性（前置门） | ✅ PASS | `{"episodes": 4, "facts": [{"id": "Q1_file_order", "ask": "Q1_file_order", "groups": [["money.py"], ["import_rows.py"], ["settlement.py"]], "ordered": true, "…` | 所有关键点 in_tmem_carrier AND in_md_carrier |
| CA-1 成本优势（主判据） | ❌ FAIL | `{"A1_free_mean_knowledge_tokens": 21812.3333, "B2_free_mean_knowledge_tokens": 5065, "ratio": 4.3065}` | mean(A1_free.knowledge_tokens) <= 0.30 * mean(B2_free.knowledge_tokens) |
| CA-1b 成本优势（注入式 vs 散文全量注入） | ❌ FAIL | `{"A1_free_mean_knowledge_tokens": 21812.3333, "B1_free_mean_knowledge_tokens": 17810.3333, "ratio": 1.2247}` | mean(A1_free.knowledge_tokens) <= 0.30 * mean(B1_free.knowledge_tokens) |
| CA-4 获取方式效应可分离 | ❌ FAIL | `{"knowledge_tokens": {"A1": 21812.3333, "A2": 21640.6667, "B1": 17810.3333, "B2": 5065}, "hit_rate": {"A1": 1.0, "A2": 0.889, "B1": 1.0, "B2": 0.889}}` | A1_free.knowledge_tokens <= A2_free.knowledge_tokens AND B1_free.knowledge_tokens <= B2_free.knowledge_tokens AND A1_free.hit_rate >= A2_free.hit_rate AND B1_free.hit_rate >= B2_free.hit_rate |
| CA-E1 效果优势（记忆专有事实） | ✅ PASS | `{"variant": "zero", "A1_zero_hit_rate": 1.0, "C_zero_hit_rate": 0.0, "B2_zero_hit_rate": 0.0, "free_variant_hit_rate_descriptive": {"A1": 1.0, "A2": 0.889, "…` | mean(A1_zero.hit_rate) > mean(C_zero.hit_rate) AND mean(A1_zero.hit_rate) >= mean(B2_zero.hit_rate) |
| CA-2 同签名复发率 | — | `None` | 未预注册具体阈值 |
| CA-3 L1 共变提示采纳精确率 | — | `None` | 未预注册具体阈值 |
| CA-6 压缩后回收 | — | `None` | 未预注册具体阈值 |
| CA-7 CA-7 | — | `None` | None |
| CA-8 CA-8 | — | `None` | None |
| CA-9 CA-9 | — | `None` | None |

## 作废样本（机械判定，不允许人工挑选）

- 无
