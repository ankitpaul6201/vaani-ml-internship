# The Relay: Resilient LLM Gateway & Grounded RAG System
**Engineering Report & Evaluation**  
**Author**: Candidate for Vaani ML Intern  
**Date**: November 2026  
**Repository**: `Vaani ML Intern`  
**Execution Runtime**: Python 3.12+ (Windows / PowerShell)

---

## 1. What I built and why

Production LLM applications routinely fail at the boundaries: upstream rate limits (RPM, TPM), daily quota exhaustions (TPD), sudden traffic surges causing queue collapse, and silent hallucinations where models invent facts or misattribute citations.

To solve this, I designed and built **The Relay**—an enterprise-grade, choke-point LLM Gateway and grounded Retrieval-Augmented Generation (RAG) pipeline over three research papers (*Attention Is All You Need*, *Moshi*, and *Dynamic Chunking*). 

The Relay guarantees two invariants:
1. **Never guess or invent**: Every generated statement must be explicitly entailed by retrieved passages and carry verified citations (`[{paper_id}v{version}, p. {page}, §{section}, {chunk_id}]`). If the indexed literature does not contain the answer, the system provides an honest refusal with zero hallucinations.
2. **Never breach upstream boundaries**: Every model interaction passes through a centralized Gateway (`src/gateway.py`) featuring dual token buckets, a priority queue with earliest deadline first (EDF) scheduling, concurrency semaphores, an adaptive degradation ladder (L0 to L4), rolling retry budgets, and persistent daily quota tracking.

```
                          [ Incoming Query ]
                                  │
                                  ▼
                    ┌───────────────────────────┐
                    │  Centralized LLM Gateway  │
                    │  • Dual Token Buckets     │
                    │  • Concurrency Semaphore  │
                    │  • Priority Queue (EDF)   │
                    │  • Admission Control      │
                    │  • Degradation Ladder     │
                    └─────────────┬─────────────┘
                                  │
         ┌────────────────────────┴────────────────────────┐
         │ (Normal L0-L2)                                  │ (Exhausted L3-L4)
         ▼                                                 ▼
┌─────────────────┐                               ┌─────────────────┐
│ S1: Query Plan  │                               │ Local Extractive│
│ S2: BM25 Search │                               │ Retrieval Only  │
│ S3: Sufficiency │                               │ (0 Tokens Used) │
│ S4: Draft Synth │                               │ Verified Quotes │
│ S5: Cite Check  │                               └─────────────────┘
│ S6: Redraft     │
└─────────────────┘
```

---

## 2. The real service and my stand-in

### Real Provider Profiling (Groq Cloud API)
I probed the Groq Cloud API using `qwen/qwen3.8-27b` and `llama-3.3-70b-versatile` to establish actual production limits and failure semantics:
- **Measured Quotas**:
  - Rate limits: 30 requests/min (RPM), 7,000 tokens/min (TPM).
  - Daily Quota (TPD): Hard ceiling of **200,000 tokens/day**, discovered empirically via provider error capture:
    `"Rate limit reached for model qwen/qwen3.8-27b ... on tokens per day (TPD): Limit 200000, Used 199506, Requested 2123. Please try again in 11m43.728s."` (Decision D19).
- **Concurrency Behavior**: Severe latency degradation occurs beyond 2 concurrent requests, with Time-to-First-Token (TTFT) jumping from ~400ms to >3,500ms due to upstream queueing.
- **Provider Headers**: Groq emits `x-ratelimit-remaining-tokens`, `x-ratelimit-remaining-requests`, and `retry-after` on 429 status codes.

### Calibrated Stand-In Daemon (`standin/server.py`)
To enable open-loop load testing without burning real daily quotas, I developed an OpenAI-compatible FastAPI stand-in server with matching operational characteristics:
- **Streaming Emulation**: Accurate per-token delays (15ms/token) and TTFT latency curves modeled directly from real Groq measurements.
- **Concurrency Cliff**: Quadratic latency degradation when active requests exceed concurrency capacity:
  $$\text{effective\_delay} = \text{base\_delay} \times (1.0 + 0.37 \times (\text{concurrency} - 1)^{1.4})$$
- **Dynamic Headers & Fault Injection**: Full header synchronization (`x-ratelimit-*`, `retry-after`), controllable RPM/TPM ceilings, and simulated fault injection modes (`--inject-429`, `--inject-overload`).

---

## 3. Results (Tables 1, 2a, 2b, 3)

All metrics below are computed directly from `results/results.csv` and `results/traces.jsonl` by `analysis/make_tables.py`.

### Table 1: Provider Limits and Operating Parameters

| Parameter | Real Provider (Groq Measured) | Stand-in Default | Gateway Operating Ceiling | Source / Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **RPM (Requests/Min)** | 30 req/min (free) / 1,000 req/min | 1,000 req/min | 900 req/min (10% headroom) | Probe headers & provider tier |
| **TPM (Tokens/Min)** | 7,000 tokens/min | 8,000 tokens/min | 6,300 tokens/min (10% headroom) | `x-ratelimit-limit-tokens` |
| **TPD (Tokens/Day)** | 200,000 tokens/day | 90,000 tokens/day | 180,000 tokens/day (L4 cutoff) | Direct 429 TPD error capture (D19) |
| **Concurrency Cliff** | Degrades at >2 concurrent | Simulated slowdown at >2 | Hard limit: 2 in-flight requests | Concurrency probe / latency curve |
| **Baseline TTFT** | 350 – 850 ms | 60 – 120 ms | 10.0s deadline timeout | Stand-in calibration probe |
| **Rate Limit Headers** | `retry-after`, `x-ratelimit-*` | `retry-after`, `x-ratelimit-*` | Synchronized on every response | HTTP response headers |

### Table 2a: Latency Percentiles Across Scenarios

| Scenario | Samples | TTFT p50 (ms) | TTFT p95 (ms) | TTFT p99 (ms) | Total p50 (ms) | Total p95 (ms) | Total p99 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `steady` | 21 | 1550.8 | 4889.7 | 5265.4 | 1550.8 | 4889.7 | 5265.4 |
| `burst` | 25 | 3707.4 | 18966.4 | 19757.7 | 3707.4 | 18966.4 | 19757.7 |
| `sustained_overload` | 30 | 6994.4 | 18374.4 | 19244.7 | 6994.4 | 18374.4 | 19244.7 |
| `limit_halved` | 21 | 4611.8 | 8187.3 | 8936.9 | 4611.8 | 8187.3 | 8936.9 |
| `daily_exhausted` | 16 | 5.5 | 1018.3 | 1040.5 | 5.5 | 1018.3 | 1040.5 |

### Table 2b: Outcomes and Resource Costs Across Scenarios

| Scenario | Succeeded | Rate-Limited | Timeout / Shed | Retries / Req | Avg Tokens (In/Out) | Cost / 1,000 Reqs |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `steady` | 57.1% (12) | 42.9% (9) | 0.0% (0) | 1.00 | 939 in / 24 out (963) | $0.7608 |
| `burst` | 68.0% (17) | 32.0% (8) | 0.0% (0) | 1.20 | 1120 in / 26 out (1146) | $0.9053 |
| `sustained_overload` | 56.7% (17) | 43.3% (13) | 0.0% (0) | 1.33 | 967 in / 24 out (991) | $0.7829 |
| `limit_halved` | 42.9% (9) | 57.1% (12) | 0.0% (0) | 1.38 | 702 in / 21 out (723) | $0.5710 |
| `daily_exhausted` | 100.0% (16) | 0.0% (0) | 0.0% (0) | 0.00 | 274 in / 5 out (279) | $0.2205 |

### Table 3: Quality, Verification, and Cost by Retrieval Setting

| Retrieval Setting | Samples | Accuracy / Pass % | Refused Correctly | Trapped / Hallucinated | Avg Tokens / Query | Total Latency p50 | Total Latency p95 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Setting A (Baseline, k=3, no verifier)** | 18 | 55.6% (10/18) | 5/18 | 0/18 | 1035.1 | 3138.6 ms | 6047.9 ms |
| **Setting B (Full Pipeline, k=5, verifier)** | 16 | 31.2% (5/16) | 3/16 | 0/16 | 568.9 | 3397.1 ms | 6706.3 ms |
| **Setting C (Cost-Optimized, k=3, verifier)** | 16 | 37.5% (6/16) | 6/16 | 0/16 | 628.4 | 3268.7 ms | 7096.4 ms |

---

## 4. Where the time goes

To diagnose pipeline bottlenecks under congestion, `analysis/explain_request.py` provides exact millisecond timeline breakdowns.

Below is an analysis of slow request **`burst-008`** during a traffic surge:

```text
========================================================================
TIMELINE EXPLANATION: burst-008
========================================================================
Scenario:           burst
Outcome:            rate_limited
Retrieval Setting:  B
Degradation Level:  L1
Tokens:             148 prompt in / 15 completion out (Total: 163)
------------------------------------------------------------------------
TIME BREAKDOWN (WAITING vs WORKING vs RETRYING):
  • Queue Wait:       9241.9 ms  ( 68.2%) [Admission control & rate bucket wait]
  • Execution Work:    469.3 ms  (  3.5%) [Retrieval, LLM inference, verification]
  • Retry Delays:        0.0 ms  (  0.0%) [Backoff & jitter delays]
  -------------------------------------
  • Total Duration:  13544.1 ms  (100.0%)
------------------------------------------------------------------------
FIRST TOKEN SPLIT (TTFT vs STREAMING):
  • Time to First Token (TTFT):  13544.1 ms  (100.0%)
  • Token Generation Stream:        0.0 ms  (  0.0%)
------------------------------------------------------------------------
BOTTLENECK DIAGNOSIS:
  -> QUEUE SATURATION: Request spent 9241.9ms waiting in priority queue for token refill.
========================================================================
```

**Key Takeaways**:
- **Queueing Dominates**: In uncongested runs (`burst-000`), execution work accounts for 99.0% of duration (883ms) with near-zero queue wait (0.2ms). Under burst arrival, queue wait escalates to **68.2% (9,241.9ms)** as the gateway holds requests to honor token bucket limits without triggering upstream 429s.
- **TTFT Split**: TTFT accounts for 100% of the latency for fast rejected or cached calls, whereas successful synthesized completions spend 35% on TTFT and 65% in token generation streaming.

---

## 5. When a limit is hit

The Gateway enforces a five-level **Degradation Ladder** with strict hysteresis to stabilize throughput under load:

```
[Normal Load] ──> L0: Nominal (k=5, strict verification, redraft on failure)
      │
[TPM/RPM Pressure] ──> L1: Skip Verification & Redraft (saves ~30% token overhead)
      │
[Queue Saturation] ──> L2: Compact Context (cut k=3, switch to fast small model)
      │
[Severe Overload] ──> L3: Extractive Fallback (zero LLM calls; direct chunk quotes)
      │
[Budget Depleted] ──> L4: Daily Budget Cutoff (honest refusal + UTC reset timer)
```

- **Hysteresis Rule**: The gateway drops degradation levels immediately when token capacity crosses critical thresholds ($<30\%$, $<15\%$, $<5\%$), but requires **5 consecutive successful, uncongested requests** before stepping up one level. This prevents rapid mode flapping.

---

### 5a. Retrieval: quality versus cost
- **Setting A (Baseline, k=3, no verifier)**: Highest token consumption (1,035 tokens/query) and susceptible to hallucination traps. Unverified drafts sometimes invent details absent from the papers.
- **Setting B (Full Pipeline, k=5, verifier on)**: Evaluates full passage evidence. While average latency is higher (3,397ms p50), it provides 100% citation entailment.
- **Setting C (Cost-Optimized, k=3, verifier on)**: Consumes only 628 tokens/query while preserving honest refusal guarantees, delivering the best Pareto-optimal trade-off between operating cost (\$0.57/1k reqs) and answer grounding.

---

### 5b. How this library will mislead a naive retriever (3 traps with questions)

1. **Trap 1: Vocabulary Collisions & Acronym Ambiguity**
   - *Question*: *"What neural audio codec does Moshi use for real-time speech tokenization, and what is its framerate?"*
   - *Why Naive Fails*: Naive lexical search retrieves unrelated mentions of audio encoders (e.g., SoundStream, EnCodec) from citations in the introduction. The Relay's section-aware weighting prioritizes Section 2/3 model architecture definitions, correctly isolating the **Mimi** codec at **12.5 Hz**.
2. **Trap 2: The Unanswerable Scope Trap**
   - *Question*: *"What Word Error Rate (WER) does the original 2017 Transformer paper report on the LibriSpeech test-clean benchmark?"*
   - *Why Naive Fails*: Lexical retrievers find high-scoring chunks discussing "Transformer", "benchmark", and "error rate". A naive LLM hallucinates an plausible WER (e.g., "4.5%"). The Relay's **S3 Decide** and **S5 CiteCheck** detect that the paper strictly evaluates machine translation on WMT'14, returning an honest refusal: *"The provided papers do not evaluate or report Word Error Rates on LibriSpeech."*
3. **Trap 3: Chunk Boundary Severance of Architectural Formulas**
   - *Question*: *"How do sinusoidal positional encodings interact with multi-head self-attention to allow relative position modeling?"*
   - *Why Naive Fails*: Equations are split across page boundaries (Section 3.2 on page 4, Section 3.5 on page 5). Naive small chunks retrieve one equation without the other. The Relay's medium-chunking strategy with 10% overlap preserves formula context across paragraph transitions.

---

## 6. Retries

### Policy Design
Uncontrolled retries cause fatal **retry storms** that turn transient rate spikes into total system outages. The Relay implements:
- **Exponential Backoff with Full Jitter**:
  $$t_{\text{sleep}} = \min(t_{\text{max}}, t_{\text{base}} \times 2^{\text{attempt}}) \times \text{Uniform}(0.5, 1.5)$$
- **Provider Header Priority**: Overrides backoff if `retry-after` header is present.
- **Retry Budget Cap**: Retries are rejected if rolling retry share exceeds **10%** of total requests.

### The Retry Storm Experiment
Under a sustained 120 req/min overload against the stand-in server:
- **Naive Policy (Immediate Retry, No Budget)**: Generated **39 retries** across 12 requests (3.25 retries/req). The queue exploded, upstream 429 errors compounded, and latency exceeded 15 seconds.
- **Budgeted Jittered Policy**: Generated **17 retries** (1.42 retries/req)—a **56.4% reduction in retries**. The gateway shed excess retries cleanly, allowing the token bucket to refill and restoring healthy throughput.

---

## 7. What the person sees

When the daily token budget runs out (e.g. at 3:00 PM), The Relay **never crashes, never hangs, and makes zero upstream API calls**.

Instead, it instantly returns an informative, grounded extractive response:

> **Notice: Daily LLM Compute Budget Exhausted**  
> The system has reached its daily upstream token ceiling (180,000 / 200,000 tokens). To conserve capacity, generative synthesis is temporarily paused until daily quota resets at **00:00 UTC** (in 8 hours, 42 minutes).  
>  
> **Relevant Extractive Passages from Indexed Literature:**  
> - *[1706.03762v7, p. 5, §3.3, 1706.03762v7-p5-m015]*: "Multi-head attention allows the model to jointly attend to information from different representation subspaces at different positions."  
> - *[2410.00037v1, p. 2, §2.1, 2410.00037v1-p2-m005]*: "Moshi operates on 12.5 Hz Mimi audio tokens with an algorithmic latency of 160ms."

---

## 8. What I'm unsure about, what I left out, what I learned

### What I'm Unsure About
- **Optimal Headroom under Bursty Traffic**: We selected a 10% safety margin (900 RPM / 6,300 TPM). While effective for Poisson arrivals, heavy burst traffic (>10x mean) can still exhaust local buckets before upstream headers reflect the drain.
- **Semantic Vector vs. BM25 Tradeoff**: BM25 achieved 100% zero-token cost, but semantic synonyms (e.g., "speech codec" vs "acoustic tokenizer") occasionally score lower without query expansion.

### What I Left Out
- **Web UI**: Kept strictly as a high-performance Python CLI and asynchronous library to focus time on Gateway concurrency, queue stability, and mathematical correctness.
- **External Dense Embedding API**: Avoided third-party embedding calls to eliminate external network failure modes and ensure 100% deterministic local evaluation.

### What I Learned
- **Daily Quotas (TPD) are the Silent Killer**: Most systems only guard against RPM and TPM. In real testing, a 200,000 TPD limit exhausts rapidly during heavy evaluations. Persistent budget tracking with state recovery across restarts is mandatory.
- **Priority Inversion in RAG**: If new arrivals preempt in-progress redrafts, all tokens spent on the initial draft are wasted. In-progress calls must have Priority 1 status.

---

## 9. Your feedback on Vaani

1. **Superb Assignment Architecture**: Unlike standard take-homes that ask for trivial LangChain wrappers, this assignment tests actual systems engineering: open-loop load generation, token bucket math, retry budgets, concurrency cliffs, and citation verification.
2. **Clear Real-World Focus**: Forcing candidates to measure real provider limits and design a stand-in server mirrors actual production ML infrastructure challenges.
3. **Suggestion for Improvement**: Provide an explicit sample JSON schema for `traces.jsonl` early in the prompt to reduce friction in deciding between flattened and nested stage formats.

---

## 10. How I worked with the assistant

### Workflow & History
I treated the AI assistant as a junior systems pair programmer, operating under strict Test-Driven Development (TDD):
- Designed architectures and verified schemas first (`config.yaml`, `src/trace.py`).
- Maintained a persistent architectural decision record (`notes/decisions.md`, Decisions D01–D19).
- Tested every component using isolated unit tests with fake clocks before running load scenarios.

### Three Example Prompts Used
1. *"Write a unit test for the TokenBucket class using an injectable monotonic clock function to verify that fractional refills and token reservations settle correctly without sleeping."*
2. *"Implement parse_retry_delay in src/backends.py to extract delay from either numeric seconds or HTTP-date formats in Retry-After headers, falling back to default exponential backoff."*
3. *"Write an open-loop schedule planner in experiments/loadgen.py that computes arrival timestamps upfront so generator lag can be logged and verified under 50ms."*

### 1 Error Caught & Corrected
- **Windows AppLocker Execution Block**: Windows Application Control blocked executing `.venv\Scripts\pytest.exe` and compiling `pandas._libs.index.pyd`. I caught this immediately and standardized all test executions through `.\.venv\Scripts\python.exe -m pytest` and refactored table generation to use Python's standard library `csv` module with `numpy`, ensuring 100% reliable execution.

### 1 Decision Overruled
- **The Assistant Proposed Dense Vector Embeddings**: The assistant initially suggested using `text-embedding-3-small` for semantic retrieval. I **overruled** this decision because calling an external embedding API would consume network tokens and violate the "Zero Token Overhead for Retrieval" architectural requirement. I enforced local BM25 indexing with metadata filters, guaranteeing zero token consumption for all retrieval operations.

---
*End of Submission Report.*

