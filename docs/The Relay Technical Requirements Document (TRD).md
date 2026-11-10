# The Relay: Technical Requirements Document (TRD)

**Version:** 1.0 draft **Related documents:** PRD, SRS, SDLC plan, Output To Be Achieved, architecture and design doc

This document says how The Relay is built: stack, components, interfaces, algorithms, configuration, testing and technical decisions. All numeric thresholds are placeholders (**TBD**) until you replace them with your own measurements.

## 1. Technical scope

The system has six parts:

1. **Ingestion and retrieval** over three pinned arXiv papers.
2. **Pipeline** (five steps with one branch and two bounded loops).
3. **LLM gateway** that owns every model call.
4. **Backends**: real Groq, and a stand-in server you write, selected by one config value.
5. **Load generator** with open-loop scenarios.
6. **Tracing and analysis** that produce every report table from the raw file.

## 2. Technology stack

| Area | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11 or newer | Async support, libraries, fastest to build |
| Concurrency | `asyncio` | Many in-flight calls without threads |
| HTTP client | `httpx` (async, streaming) | Streaming responses and timeouts |
| Stand-in server | `fastapi` and `uvicorn` | Server-sent events, easy admin endpoints |
| PDF extraction | `pymupdf` (or `pdfplumber`) | Page-aware text |
| Lexical retrieval | `rank_bm25` | Free, local, no tokens |
| Embeddings (optional) | `sentence-transformers` and `faiss-cpu` | Local semantic search |
| Config | YAML via `pyyaml` | One file for every threshold |
| Analysis | `pandas`, `numpy`, `matplotlib` | Tables and charts from the raw file |
| Tests | `pytest`, `pytest-asyncio` | Gateway logic with a fake clock |
| Lint and format | `ruff` | Fast, one tool |

Pin every version in `requirements.txt`. Do not add libraries without a reason recorded in the decision log.

## 3. Components and responsibilities

| Component | Responsibility | Must not |
| --- | --- | --- |
| `ingest` | Parse PDFs, chunk, tag, index | Call the model |
| `retriever` | Return top pieces with citation metadata for a query and a setting | Spend provider tokens |
| `pipeline` | Run S1 to S5, branches and loops, emit traces | Call a backend directly or retry on its own |
| `gateway` | Admission, queue, buckets, concurrency, retries, breaker, ladder, budget | Know about papers or prompts |
| `backends` | Send one call to Groq or the stand-in and report timings and headers | Make policy decisions |
| `citecheck` | Deterministic verification of citations | Use a model |
| `standin` | Imitate the provider with controllable behaviour | Share code paths with real calls beyond the API shape |
| `loadgen` | Create open-loop arrivals and record outcomes | Change pipeline behaviour |
| `analysis` | Read raw files and print tables | Use any data not in the files |

## 4. Interfaces

### 4.1 Backend API contract

Both backends expose an OpenAI-style chat completions interface so only `base_url` changes.

- `POST {base_url}/chat/completions`
- Request fields used: `model`, `messages`, `max_tokens`, `temperature`, `stream`
- Streaming response: server-sent events with incremental deltas and a final usage record, if the provider supplies one
- Non-streaming response: includes `usage` with prompt and completion token counts
- Error contract: HTTP 429 with a `retry-after` header and rate-limit headers where available, 5xx for server errors

Verify the exact header names and the 429 body against real responses in phase 3, then make the stand-in return the same shape.

### 4.2 Internal interfaces (sketch)

```python
class Backend(Protocol):
    async def stream_chat(self, req: ModelRequest) -> AsyncIterator[Chunk]: ...

class Gateway:
    async def call(self, req: ModelRequest, ctx: RequestContext,
                   priority: int) -> ModelResult: ...
    def admit(self, ctx: RequestContext) -> Admission: ...   # accept, queue(eta), shed
    def level(self) -> int: ...                              # current ladder level 0..4

class Retriever(Protocol):
    def search(self, query: str, setting: Setting) -> list[Passage]: ...

class RequestContext:
    request_id: str
    deadline: float          # monotonic seconds
    trace: Trace
```

The pipeline imports `Gateway` and `Retriever` only. It never imports a backend.

### 4.3 Stand-in admin API

| Endpoint | Purpose |
| --- | --- |
| `POST /admin/config` | Change latency parameters, failure rates, limits and slowdown without restarting |
| `GET /admin/stats` | In-flight count, requests served, limits remaining |
| `POST /admin/reset` | Reset counters and daily budget between runs |

## 5. Data schemas

### Chunk

| Field | Type | Notes |
| --- | --- | --- |
| `chunk_id` | string | Stable, for example paper, section, page, index |
| `paper_id`, `version` | string, int | Must match the pinned versions |
| `section`, `page` | string, int | Needed for citations |
| `kind` | enum | prose, table, equation, caption, references |
| `text`, `token_count` | string, int |  |

### Trace (JSON lines, one record per request)

`request_id`, `scenario`, `source`, `start_time`, `deadline`, `commit_hash`, `retrieval_setting`, `degradation_level`, `outcome`, list of stage records (`name`, `t_enqueue`, `t_start`, `t_first_byte`, `t_end`, `attempt`, `status`), `queue_wait_ms`, `work_ms`, `retry_ms`, `tokens_in`, `tokens_out`, `first_token_ms`, `total_ms`.

### results.csv

Exactly the columns the brief fixes: `request_id, scenario, source, start_time, first_token_ms, total_ms, status, retries, tokens_in, tokens_out, retrieval_setting, answer_grade`. It is generated from the trace file, never edited by hand.

## 6. Core algorithms

### 6.1 Token buckets

Two buckets, one for requests per minute and one for tokens per minute, with capacity set slightly below the measured limit (margin **TBD**). A call reserves `prompt_tokens + max_tokens` before sending and settles to the real usage afterwards. If the real response headers show less remaining capacity than the local estimate, the local estimate is overridden.

### 6.2 Priority queue and scheduling

| Priority | Work |
| --- | --- |
| 1 (highest) | S4 and S5 calls for requests that have already spent tokens |
| 2 | S1 for new requests |
| 3 | Optional work: model-based verification, evaluation runs |

Within a priority, earliest deadline first. A call whose deadline has passed is dropped, and its request is marked given up.

### 6.3 Admission control

On arrival, estimate wait as queued token demand divided by the sustainable token rate. If estimated wait plus estimated service time exceeds the request's remaining patience: queue with an honest ETA if the wait is recoverable, otherwise shed with a busy reply and a suggested retry time. The thresholds are **TBD** and come from measurement.

### 6.4 Concurrency control

A cap on in-flight calls, adjusted additively upward when latency is healthy and multiplicatively downward when time to first byte or timeouts rise (AIMD). Starting cap **TBD** from the probe.

### 6.5 Retry policy

| Failure | Action |
| --- | --- |
| 429 | Wait at least `retry-after` plus random jitter, requeue, count against the retry budget |
| 5xx or connection error | Up to 2 retries, exponential backoff with full jitter |
| Timeout before first byte | Cancel, retry once if the deadline and budget allow |
| Stall mid-stream | Cancel, retry once or fall back to an extract |
| Other 4xx | No retry |
| Daily budget exhausted | No retry, go to ladder level 4 |

Retry budget: retries may not exceed a fixed share of recent calls (share **TBD**, start at 10%).

### 6.6 Circuit breaker

States: closed, open, half-open. Opens after N failures or slow calls in a window (N and window **TBD**), stays open for a cooling period, then lets a single probe through. A successful probe closes it.

### 6.7 Degradation controller

| Level | Trigger (TBD) | Behaviour |
| --- | --- | --- |
| 0 | Healthy | Full setting, model verification, large draft model |
| 1 | Queue wait high or token bucket low | Skip model verification, smaller retrieval |
| 2 | Token bucket very low or upstream slow | Smaller draft model, shorter output, 3 pieces |
| 3 | Daily budget above 95%, or breaker open | Extract only |
| 4 | Daily budget exhausted | Extract only, no model calls, explanatory message |

Use hysteresis (different thresholds to enter and leave a level) to stop flapping. Every transition is logged with the reason.

### 6.8 Daily budget

Counters for requests and tokens used today, persisted to disk after each settle, reset on the provider's schedule (verify how and when it resets).

## 7. Configuration

All thresholds live in one file so they can be changed in the live conversation without code edits.

```yaml
backend:
  mode: standin            # groq | standin
  base_url: http://localhost:8000/v1
models:
  small: TBD               # routing and verification
  large: TBD               # drafting
limits:                    # from your probe, with margin
  rpm: TBD
  tpm: TBD
  rpd: TBD
  tpd: TBD
  max_in_flight: TBD
patience_s: TBD
retry:
  max_attempts: 2
  budget_share: 0.10
ladder:
  l1_queue_wait_ms: TBD
  l2_bucket_pct: TBD
  l3_daily_pct: 95
retrieval:
  setting: B
```

The API key is read from an environment variable and never appears in this file.

## 8. Retrieval specification

- Extract text per page, record section headings, tag each chunk by kind.
- Three settings: A (small pieces, many fetched), B (medium pieces, fewer fetched), C (medium pieces, over-fetch then re-rank to keep fewer). Exact sizes **TBD** from experiments.
- Retrieval must cost zero provider tokens. Use local lexical, local embedding, or both.
- S3 uses a score threshold to pick answer, widen once, or refuse. The threshold is tuned on your question set.
- Down-weight the references section. Keep it addressable.

## 9. Stand-in server specification

| Behaviour | Technical approach |
| --- | --- |
| Slow | Time to first token drawn from a distribution fitted to the probe (log-normal is a starting point), then a per-token delay with jitter |
| Slower under load | Latency multiplier that grows with in-flight requests, for example `1 + a * (inflight / capacity) ** b`, with `a`, `b` and `capacity` fitted or chosen and stated |
| Fail | Configurable rates of 5xx, connection reset and hangs |
| Slow success | A configurable share of calls taking a multiple of the median but succeeding |
| Refuse on limits | The same bucket logic as the real limits, returning 429 with `retry-after` and rate-limit headers |
| Stream | Server-sent events at the measured token rate |
| Output | Canned or templated text with varying length so token accounting is realistic |

Known differences from the real service (to be stated in the write-up): a single machine, uncorrelated random failures, no other customers' load, fixed limits, no content-dependent latency.

## 10. Observability

- Every stage writes timestamps using a monotonic clock. Wall-clock time appears only in `start_time`.
- Every real response logs its rate-limit headers.
- The gateway samples queue length at a fixed interval so peak queue length is measurable.
- A script prints the timeline for any request id, splitting waiting, working and retrying.
- Every trace stores the code commit hash, so a result can be tied to the exact version.

## 11. Performance and capacity requirements

All values **TBD** and set from measurement:

| Requirement | Placeholder |
| --- | --- |
| Gateway overhead per call | Small compared with model latency, measured |
| Load generator accuracy | Actual send time within a stated tolerance of scheduled time |
| Stand-in fidelity | p50 and p95 within a stated percentage of the real service for the same request mix |
| Steady-state behaviour | No retry storm, queue length bounded |
| Overload behaviour | Shedding and ladder activate before deadlines collapse |

## 12. Testing strategy

| Level | What | How |
| --- | --- | --- |
| Unit | Buckets, queue ordering, retry rules, ladder transitions, citation checker | Fake clock, fake backend |
| Integration | Pipeline against the stand-in | Scripted scenarios |
| Contract | Stand-in response shape equals the real one | Compare captured real responses |
| Load | Five scenarios | Open-loop generator |
| Evaluation | At least 15 questions, three retrieval settings | Graded by rubric |
| Reproducibility | Tables from the raw file | Independent recomputation of a sample of numbers |

## 13. Security and compliance

- Keep the key in an environment variable. `.env` is git-ignored and a secret scan runs before commits.
- Use only your own free account. Do not use extra accounts or tricks to get around limits.
- Stay within the provider's terms.
- Do not log prompts that contain secrets. Never include the key in traces or the submission.

## 14. Environments and running

| Environment | Use |
| --- | --- |
| Local machine | Primary. Build, probe, quality runs, load runs |
| Notebook (optional) | Heavy stand-in load, if local CPU contention appears. Clone the repo and run a script |

Three processes: stand-in server, pipeline plus gateway, load generator. Record the machine, the network and the time of each real run.

## 15. Technical decisions log

| ID | Decision | Alternative | Why | Cost |
| --- | --- | --- | --- | --- |
| T1 | One gateway owns all model calls | Per-step retries | Avoids retry multiplication and lets one place enforce limits | Gateway is a bottleneck and a single point to get right |
| T2 | Buffer the draft until verified | Stream to the person immediately | No unverified text is ever shown | Higher time to first token |
| T3 | Deterministic citation checker always on | Model-only verification | Zero tokens and cannot be rate limited | Cannot judge meaning |
| T4 | Open-loop load generation | Closed-loop | Overload shows up instead of being hidden | More complex generator |
| T5 | Local retrieval | Model-based retrieval | No provider tokens spent | Possibly lower semantic quality |
| T6 | Extract-only fallback | Hard error | Still useful and still cited | No synthesis |

Add rows as you decide. Each should name the measurement behind it.

## 16. Technical risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Real limits differ from documentation | Wrong gateway numbers | Read headers at runtime, mark confidence |
| Header names or 429 body differ from assumptions | Stand-in mismatch | Capture real responses first |
| Generator and stand-in share CPU | Distorted latency | Watch CPU, report as a limitation |
| PDF extraction quality | Retrieval misses | Inspect and tag kinds |
| Clock misuse | Wrong durations | Monotonic clock for durations |
| Changing code between runs | Tables describe different systems | Code freeze and commit hash in traces |
