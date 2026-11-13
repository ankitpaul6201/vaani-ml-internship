# Empirical Evaluation Tables

### Table 1: Provider Limits and Operating Parameters

| Limit | Value you found | How you found it | Confidence (high, medium, low) |
| :--- | :--- | :--- | :--- |
| **Requests per minute** | 30 req/min (free) / 1,000 req/min (tier 1) | Response header `x-ratelimit-limit-requests` | High |
| **Tokens per minute** | 7,000 tokens/min | Response header `x-ratelimit-limit-tokens` | High |
| **Requests per day** | 14,400 req/day | Provider documentation & tier specification | High |
| **Tokens per day** | 200,000 tokens/day | Direct 429 error payload `tokens per day (TPD) limit reached` (D19) | High |
| **Requests in flight at once** | 2 requests | Live concurrency probe (cliff at N > 2) | High |
| **What it returns when a limit is hit** | HTTP 429 Too Many Requests | Direct probe capture: `error.code = rate_limit_exceeded` | High |
| **Time until it recovers** | 60s (RPM/TPM) / next midnight UTC (TPD) | Response header `retry-after` and reset headers | High |

### Table 2a: Latency Percentiles Across Scenarios

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Steady | stand-in | 25 | 241.2 | 6.2 | 833.0 / 1102.8 / 1139.3 | 885.0 / 1161.6 / 1199.3 |
| Burst | stand-in | 50 | 52.2 | 57.5 | 20025.6 / 20410.7 / 20527.5 | 20025.6 / 20410.7 / 20527.5 |
| Sustained overload | stand-in | 60 | 60.0 | 60.0 | 3.5 / 10.2 / 893.7 | 3.5 / 10.2 / 952.5 |
| Per-minute limit halved | stand-in | 15 | 140.1 | 6.4 | 3.3 / 46.2 / 119.9 | 3.3 / 46.2 / 119.9 |
| Daily budget exhausted | stand-in | 15 | 141.1 | 6.4 | 3.4 / 920.5 / 1011.9 | 3.4 / 974.5 / 1065.9 |

### Table 2b: Outcomes and Resource Costs Across Scenarios

| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Steady | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 1357 | $1.0722 |
| Burst | 16.0% | 0.0% | 84.0% | 0.00 | 0 | 224 | $0.1773 |
| Sustained overload | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 49 | $0.0384 |
| Per-minute limit halved | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 0 | $0.0000 |
| Daily budget exhausted | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 298 | $0.2352 |

### Table 3: Retrieval settings (at least three)

| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Setting A (Small chunks 200w, k=3, no verifier) | 16 | 81.2% | 0.0% | 18.8% | 1256.8 | 5863.6 / 13963.0 | 5937.6 / 14041.0 |
| Setting B (Medium chunks 400w, k=5, dense+BM25, verifier) | 16 | 81.2% | 0.0% | 18.8% | 1256.8 | 11541.6 / 13782.5 | 11618.6 / 13860.5 |
| Setting C (Cost-optimized 300w, dynamic k=3, verifier) | 16 | 81.2% | 0.0% | 18.8% | 1256.8 | 11410.7 / 13897.9 | 11490.7 / 13975.9 |

### Table 3b: Verifier Ablation (Identical Setting B Retrieval, 16 Questions)

| Ablation Condition | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Refused wrongly % |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Verifier OFF (Setting B retrieval, raw output released) | 16 | 81.2% | 0.0% | 18.8% | 0.0% |
| Verifier ON (Setting B retrieval, verified citations + redraft) | 16 | 81.2% | 0.0% | 18.8% | 0.0% |

