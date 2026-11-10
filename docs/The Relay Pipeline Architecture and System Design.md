# The Relay: Pipeline Architecture and System Design

Design doc for the cited-answer assistant over three arXiv papers, its LLM gateway, the stand-in model server, and the load and measurement harness.

Status: draft v1. Numbers marked **TBD** must come from your own measurements. Nothing here is a Groq limit until you have verified it.

## 1. Purpose and goals

Build a small multi-step pipeline that answers engineers' questions from three papers, then show how it behaves under load and what each trade-off costs.

The four goals pull against each other (fast, good, cheap, nobody turned away). One rule outranks all of them: **never send a wrong or uncited answer to stay fast.** Every degradation path must end in either a cited answer or an honest refusal.

### Proposed service targets (set yours after probing)

| Target | Proposed value | Notes |
| --- | --- | --- |
| Time to first token (TTFT), p95, normal load | TBD (start with 3,000 ms) | Defined in section 8 |
| Total time, p95, normal load | TBD (start with 12,000 ms) |  |
| Patience limit (when a person gives up) | TBD (start with 20 s) | Drives deadlines and given-up counts |
| Answers that are wrong | As low as possible; refusing is preferred to guessing |  |
| Requests turned away outright | Minimised, but allowed under sustained overload | State it openly |

## 2. Scope

**In scope:** ingestion, retrieval, 5-step pipeline, LLM gateway, real Groq backend, stand-in server, load generator, trace logging, analysis script that produces every table from `results.csv`.

**Out of scope (candidates for "two things I did not build"):** multi-user auth, persistent storage beyond files, a web UI, conversation memory across turns, hedged (duplicate) requests, autoscaling, a distributed queue.

## 3. Architecture overview

```
                 +--------------------+
  load generator | Request intake     |  deadline = now + patience
  or user  ----->| + admission control|--- shed / queue-with-ETA --> fast honest reply
                 +---------+----------+
                           |
                           v
                 +--------------------+
                 | Pipeline runner    |  one async task per request
                 | (state machine)    |
                 +---------+----------+
                           |
   S1 Understand --> S2 Retrieve --> S3 Decide --> S4 Draft --> S5 Verify --> Release
        |                |             |  |          |            |
        |                |             |  +-refuse---+------------+--> Refusal reply
        |                |             +-widen retrieval (loop, max 1)
        |                |                          +-redraft (loop, max 1)
        |                v
        |         Retriever (tool, local, zero Groq tokens)
        |         + Citation checker (tool, deterministic)
        v
   +-----------------------------------------------+
   | LLM gateway (owns EVERY model call)           |
   |  priority queue | RPM and TPM token buckets   |
   |  concurrency cap | daily budget tracker       |
   |  retry policy | circuit breaker | ladder      |
   +-----------------------+-----------------------+
                           |
              base_url switch (one config value)
                +----------+-----------+
                v                      v
        Groq (real, OpenAI-style)   Stand-in server (FastAPI, same API)

  Every stage writes a trace record --> traces.jsonl --> results.csv --> tables
```

### Design principles

1. **One place owns the limits.** Pipeline steps never call the model directly or retry on their own. They ask the gateway. This avoids each step retrying independently and multiplying load.
2. **Backends are swappable by URL.** The stand-in copies the real API shape, so pipeline code is identical in real and stand-in runs.
3. **Deadlines travel with the request.** Work for a person who has already given up is cancelled, not finished.
4. **Measure first, then set numbers.** Every threshold in this doc is a placeholder until probing replaces it.

## 4. Pipeline steps

The pipeline is a small state machine. It satisfies the brief's requirements: at least 4 steps, at least one tool call, and at least one branch or loop.

| Step | Name | Uses | Output | Typical model need |
| --- | --- | --- | --- | --- |
| S1 | Understand | Small model, or rules for trivial cases | Intent (answerable-looking / off-topic / ambiguous), rewritten search query, which papers likely relevant | Small, short output |
| S2 | Retrieve | **Tool**: local retriever | Top-k chunks with paper, section, page, chunk id | None (no Groq tokens) |
| S3 | Decide | Rules plus score thresholds | Branch: answer, widen once, or refuse | None |
| S4 | Draft | Larger model, streamed into a buffer | Answer where each claim carries a chunk citation | Largest token cost |
| S5 | Verify | **Tool**: citation checker, then optional small-model check | Pass, fail (redraft once), or refuse | Small, optional |

### S1 Understand

Turns the raw question into a retrieval query and a coarse label. Keep the output tiny (a few dozen tokens) because this step runs for every request. If the question is trivially in scope by keyword rules, skipping the model here is a cheap degradation option.

### S2 Retrieve (the tool call)

A local function, not a model call. It returns chunks with metadata needed for citation: paper id and version, section heading, page number, chunk id, and the exact text. See section 6.

### S3 Decide (branch and loop)

- **Answer:** top scores clear the threshold and at least one chunk plausibly addresses the question.
- **Widen once (loop):** scores are borderline. Fetch more or different chunks (for example, query each paper separately) and re-decide. Maximum one widening, so cost stays bounded.
- **Refuse:** nothing relevant enough. Return a refusal that says the papers do not cover it, without calling the draft model at all. This is the cheapest path and is also the correct behaviour for unanswerable questions.

The threshold is a tuning parameter. Too low produces wrong answers, too high produces wrongly refused answers. Pick it from your question set, and report both error types.

### S4 Draft

The model is instructed to answer only from supplied chunks, cite chunk ids inline, and say so when the chunks do not support an answer. Cap `max_tokens`. The output is streamed from the model into a buffer (see section 8 for why it is not yet shown to the person).

### S5 Verify (loop)

Two layers:

1. **Deterministic citation checker (always runs, zero tokens):** every cited chunk id exists in the retrieved set, every cited span or quoted phrase appears in that chunk, and every sentence making a factual claim carries a citation.
2. **Optional small-model support check:** does the cited chunk actually support the claim? Skipped at higher degradation levels.

On failure: redraft once with the failure reason added to the prompt. If it fails again, refuse. Maximum one redraft.

## 5. Data model

### Chunk

```
chunk_id      "attn-v7-s3.2-p4-c012"
paper_id      "1706.03762"
version       7
section       "3.2 Attention"
page          4
text          "..."
token_count   n
kind          prose | table | equation | caption | references
```

### Request trace (one record per request, JSONL)

Holds everything needed for section 4 of the write-up (latency split) and for the CSV:

```
request_id, scenario, source, start_time
deadline_ms, outcome, degradation_level, retrieval_setting
stages: [ {name, t_enqueue, t_start, t_first_byte, t_end, attempt, status} ... ]
queue_wait_ms, work_ms, retry_ms          (derived: waiting, working, retrying)
tokens_in, tokens_out, first_token_ms, total_ms
answer_grade                              (filled by your grading script)
```

### results.csv

The brief fixes these columns exactly: `request_id, scenario, source, start_time, first_token_ms, total_ms, status, retries, tokens_in, tokens_out, retrieval_setting, answer_grade`. Generate it from `traces.jsonl`. Keep the extra detail (queue and retry split, degradation level) in the trace file and submit that too, so Table 2b's "peak queue length" and section 4's split are both reproducible.

## 6. Ingestion and retrieval

### Ingestion

1. Download the three PDFs at the exact versions (v7, v1, v2) and record file hashes.
2. Extract text page by page, keeping page numbers and section headings.
3. Tag each chunk's `kind`. Tables, equations and captions are flagged because plain text extraction mangles them.
4. Drop or down-weight the references section for retrieval, but keep it addressable.

### Retrieval settings to compare (Table 3 needs at least three)

| Setting | Piece size | Pieces fetched | Re-rank | What you expect to learn |
| --- | --- | --- | --- | --- |
| A | small (about 200 tokens) | 8 | no | Precise but fragmented context |
| B | medium (about 500 tokens) | 4 | no | Cheaper, may miss multi-part answers |
| C | medium (about 500 tokens) | 10 fetched, 4 kept | yes (local re-ranker) | Better precision, extra latency |

These are starting points. Retrieval can be BM25, local embeddings, or both. Use a local method so retrieval costs no Groq quota. For each setting record: correct and cited %, wrong %, correctly refused %, tokens per answer, TTFT and total time at p50 and p95.

**When the limit is close:** retrieval is the cheapest knob to turn. The ladder (section 7) moves from C to B to a smaller k. Shorter context directly cuts tokens per request, which is what the per-minute token limit counts.

### How this library can mislead a naive retriever (candidates to verify)

Pick three and prove each with a real failing question from your own runs.

1. **Word collisions across papers.** "Chunking" in Dynamic Chunking means splitting a byte or token sequence inside a model. A naive retriever may return RAG-style or text-splitting passages for it.
2. **One paper's vocabulary dominating.** "Attention", "transformer" and "sequence" appear in all three. The first paper's passages can crowd out the others in lexical scoring.
3. **Tables, equations and captions.** Numbers like BLEU scores or formulas live there, and extracted text may be garbled, so the right passage never surfaces.
4. **Version and scope traps.** Questions about content that is not in these exact versions, or only partly covered, tempt the retriever to return a nearby passage that does not actually answer.

## 7. LLM gateway (the core component)

All model calls from every pipeline step go through this one component.

### Responsibilities

| Concern | Mechanism |
| --- | --- |
| Requests per minute | Token bucket sized from the limit you measured, with a safety margin |
| Tokens per minute | Second bucket. Each call reserves `prompt tokens + max_tokens`, then settles to the real `usage` after the response |
| Requests in flight | Semaphore. Start from what you find, and adjust down on timeouts or slowdowns |
| Daily requests and tokens | Counter persisted to disk, so a restart does not forget today's spend |
| Learning limits | Read the rate-limit headers on every real response (remaining, reset) and `retry-after` on 429s. Trust these over hard-coded numbers |
| Priority | See below |
| Deadlines | Each call inherits the request deadline. Expired calls are dropped, not sent |
| Retries | See section 9 |
| Circuit breaker | Opens after repeated 429s, timeouts or errors, then half-opens with a probe |

### Priority order

1. Calls for requests that have already spent tokens (S4, S5). They are partly paid for, so finishing them wastes the least.
2. S1 and S2 for new requests.
3. Optional work (LLM verification, evaluation runs).

This choice protects work in progress, and it means new arrivals wait. State that cost in your write-up.

### Admission control

On arrival, estimate the request's token cost and the current queue wait. If the projected finish time exceeds the deadline, do not accept it silently. Choose between:

- **Queue with an honest ETA** (default while capacity is recoverable).
- **Shed immediately** with a clear "busy" reply and a suggested retry time (when the queue can no longer meet any deadline).

A fast honest "busy" is better than a person waiting 20 seconds for a timeout. It is also the explicit trade-off against "nobody turned away".

### Degradation ladder

| Level | Trigger (placeholders) | Changes | What is given up |
| --- | --- | --- | --- |
| L0 normal | Healthy | Full retrieval setting, model verify on, large draft model | Nothing |
| L1 | Queue wait above X ms, or token bucket under 30% | Skip model verify (citation checker still runs), smaller k | Verification depth |
| L2 | Token bucket under 10%, or upstream slow | Smaller draft model, shorter `max_tokens`, k = 3 | Answer richness and nuance |
| L3 | Daily budget above 95%, or breaker open | Extractive answer: top cited passages shown verbatim, no generation | Synthesis, but still cited |
| L4 | Daily budget exhausted | Retrieval-only for everyone, no model calls | All generation, with an honest explanation |

**Hard rule at every level:** the output is a cited answer, a cited extract, or a refusal. Never an uncited guess.

### The one decision that shapes everything

Centralising all model access in a gateway that does token-aware admission, priority, and a degradation ladder, instead of letting each step retry and fail on its own. Everything else (what is delayed, dropped or weakened) falls out of it. Replace this with your own framing if your measurements point elsewhere.

## 8. Streaming and time to first token

Define **first\_token\_ms** as the time from request start to the first token of the *final reply* the person sees (answer, extract or refusal), including any queueing, S1, S2 and the verification gate described below.

### The trade-off to decide and measure

S4 produces the answer and S5 checks it. Streaming the draft straight to the person is fastest, but risks showing something that later fails verification.

| Mode | Behaviour | TTFT | Risk |
| --- | --- | --- | --- |
| Buffered (default) | Draft streams into a buffer. After verify passes, release it to the person | Higher: includes full draft and verify | None: nothing unverified is shown |
| Gated streaming | Stream sentence by sentence, releasing each sentence only after the citation checker passes it | Lower | More complex, and a late failure means a partial answer |

Start with buffered mode, measure the TTFT cost, and only then consider gated streaming. You can also send a status event ("searching the papers...") early, but do **not** count it as the first token. Report it separately so TTFT is not flattered.

## 9. Retry policy

| Failure | Action |
| --- | --- |
| 429 from the provider | Wait at least `retry-after`, plus random jitter, then requeue at the same priority. Counts against the retry budget |
| 5xx, connection error | Up to 2 retries, exponential backoff with full jitter |
| Timeout before first token | Cancel the call, retry once only if the deadline allows and the retry budget has room |
| Stall mid-stream (no tokens for N ms) | Cancel and retry once, or fall back to an extract if time is short |
| 4xx other than 429 | Do not retry. Fix the request or fail |
| Daily limit reached | Do not retry. Go to L4 |

**Retry budget:** retries may not exceed a fixed share (for example 10%) of recent calls. When the budget is spent, fail fast or degrade instead of retrying.

### The case where retrying makes things worse (build this experiment on the stand-in)

"Slow success" under load. The stand-in gets slower as in-flight requests rise. Clients time out and retry, but the original call is still running on the server. Load rises, so everything gets slower, so more calls time out, so more retries. Compare the same overload run with (a) naive immediate retries and (b) your budgeted jittered policy, and report the difference in success %, p95 and retries per request.

## 10. Stand-in model server

A FastAPI service that mimics the real API so only `base_url` changes.

### Endpoint and features

- `POST /v1/chat/completions` with streaming (server-sent events), plus non-streaming.
- Returns `usage` (prompt and completion tokens) like the real service.
- Rate-limit headers and 429 with `retry-after`, in the same shape you observed from Groq.
- An admin endpoint (or config file) to change behaviour between runs without restarting.

### Behaviours it must support

| Behaviour | Model | Calibrated from |
| --- | --- | --- |
| Slow | Time to first token drawn from a fitted distribution (log-normal is a common starting point), then a per-token delay | Your probe: TTFT and inter-token times |
| Fail | Probability of 5xx, connection reset, or hang | Your probe, plus your own injected rates |
| Refuse on limits | Token buckets for RPM, TPM, plus daily counters. Returns 429 | Limits you found |
| Slower under load | Latency multiplier that grows with in-flight requests, for example `1 + a * (inflight / capacity)^b` | Probe at different concurrency levels, if quota allows |
| Stream piecewise | Emits tokens at a measured rate with jitter | Probe's streaming timings |
| Slow success | Occasional requests that take a multiple of the median but succeed | Tail of your probe distribution |

Output text can be canned or templated, since quality is judged on the real model. Make output length vary so token accounting behaves realistically.

### Calibration procedure

1. Send a modest set of real requests at several prompt sizes and, if quota allows, a few concurrency levels.
2. Fit TTFT and per-token timing, from your CSV, by prompt size.
3. Record the 429 response body, headers, and time to recover.
4. Set the stand-in's parameters from those fits, then check that its latency percentiles match the real ones on the same request mix.

### Where it will differ from the real service (say this plainly)

- It has one machine's CPU, not a datacentre. Running the load generator on the same machine adds noise.
- Real load-dependent slowdown depends on other customers you cannot see.
- Real failures are correlated (outages, bad periods). Random failures are not.
- It cannot reproduce changes in limits without notice.
- Real output length and content vary with the prompt.

## 11. Load scenarios and measurement

| Scenario | Backend | Shape | Purpose |
| --- | --- | --- | --- |
| Steady | Real (small) and stand-in | Constant arrival rate below the limit | Baseline latency |
| Burst | Stand-in | Sudden spike to a multiple of the rate, then back | Queueing, admission control, recovery time |
| Sustained overload | Stand-in | Arrival rate above capacity for minutes | Shedding, ladder, retry behaviour |
| Per-minute limit halved | Stand-in (limit configured at half) | Same as steady | Effect of a changed limit |
| Daily budget exhausted | Stand-in, plus a short real check if affordable | Budget runs out mid-run | L4 behaviour and what the person sees |

Use **open-loop** arrivals (requests arrive on a schedule whether or not earlier ones finished). Closed-loop clients slow themselves down and hide overload. Record the scheduled start time and the actual send time, to catch the generator itself falling behind.

The load generator marks a request **given up** when its patience deadline passes. Count it separately from rate-limited, error and timeout so the tables stay consistent.

### Metrics mapping to the brief's tables

| Table field | Computed from |
| --- | --- |
| First token and total time p50 / p95 / p99 | `first_token_ms`, `total_ms` over completed requests (say how you treat given-up requests) |
| Succeeded, rate-limited, timed out or given up % | `status` column |
| Retries per request | `retries` column average |
| Peak queue length | Gateway queue samples in the trace file |
| Tokens per answer, cost per 1,000 answers | `tokens_in`, `tokens_out` and the provider's pricing (use the free-tier equivalent price, and say so) |

Write one analysis script that reads only `results.csv` (and the trace file where needed) and prints every table, so the numbers in the write-up always match the raw file.

## 12. Quality evaluation

- At least 15 questions you write yourself, with gold answers and the page or section that supports each.
- Suggested mix: about 6 single-paper facts, 3 multi-passage questions, 3 cross-paper questions, and at least 3 questions the papers do not answer (these test refusal).
- Grade each answer as `correct` (right and properly cited), `wrong`, `refused_correctly`, or `refused_wrongly`.
- Run the same questions across the three retrieval settings, and record tokens and latency alongside the grade.
- Check citations by hand on a sample. A confident answer with a wrong page number counts as wrong.

## 13. Failure modes and what the person sees

| Situation | Reply |
| --- | --- |
| Normal | Cited answer |
| Queue is long but recoverable | Early status message with an honest wait estimate, then the answer |
| Provider limit near | Shorter, still cited answer (L1 or L2) |
| Breaker open or daily budget at 95% | Extract: the most relevant passages with citations, labelled as an extract |
| Daily budget exhausted (the 3pm case) | Immediate reply stating that generated answers are unavailable until the budget resets, plus the top cited passages from the papers, plus when to try again |
| Papers do not cover it | Refusal that says so and, where useful, points to the nearest related passage |

The 3pm reply must not hang, must not pretend to answer, and must not hide that the answer is reduced.

## 14. Repository layout

```
relay/
  README.md
  .env.example            # no real key, ever
  config.yaml             # backend url, limits, thresholds, ladder, retrieval setting
  data/papers/            # the 3 PDFs (exact versions) and hashes
  src/
    ingest.py             # parse, chunk, index
    retriever.py          # S2 tool, supports settings A, B, C
    pipeline.py           # state machine S1 to S5
    gateway.py            # buckets, queue, retries, breaker, ladder, budget
    citecheck.py          # deterministic citation checker
    backends.py           # groq and stand-in via base_url
    trace.py              # trace records, jsonl writer
  standin/
    server.py             # FastAPI stand-in
    calibrate.py          # fit parameters from probe data
  experiments/
    probe_groq.py         # real probing, logs headers and latencies
    loadgen.py            # open-loop scenarios
    run_quality.py        # question set across retrieval settings
    questions.yaml        # 15+ questions with gold answers
  analysis/
    make_tables.py        # results.csv to Tables 1, 2a, 2b, 3
  results/
    real/  standin/       # raw outputs, results.csv, traces.jsonl
  notes/
    assistant_log/        # your conversation history, uncleaned
    decisions.md          # running log for the write-up
```

## 15. Build order

1. Ingest papers, with citation metadata. Check extraction quality by eye.
2. Pipeline S1 to S5 against real Groq on a handful of questions.
3. Probe real Groq (requests, TPM, headers, 429 behaviour, recovery). Save raw data immediately.
4. Gateway with buckets, retries and budget tracking.
5. Stand-in server, calibrated from step 3.
6. Load generator and the five scenarios.
7. Retrieval experiments and grading.
8. Analysis script and tables.
9. Write-up, within 2 pages.

## 16. Quota plan for the real service

Your daily limit is shared by every run, so budget in tokens, not just requests. Before you start, estimate tokens per answer for S1, S4 and S5, and list the real runs you need: probing, quality grading across three retrieval settings, and a small real steady run. Keep a reserve for reruns. Everything heavy goes to the stand-in.

## 17. Risk register

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Daily quota burned early | Cannot finish real runs | Budget in tokens, log spend, use stand-in for load |
| Limits differ from documentation | Wrong gateway numbers | Read headers at runtime, mark confidence in Table 1 |
| Stand-in unlike the real service | Misleading load results | Check percentiles against real data, list differences |
| Generator and server share a CPU | Distorted latency | Watch CPU, keep rates modest, report the limitation |
| PDF extraction mangles tables | Retrieval misses answers | Tag kinds, inspect samples |
| Tables do not match CSV | Lost credibility | Generate every table from the file |
| Cannot explain a choice live | Weak defence | Log each decision and its measured reason in `decisions.md` |

## 18. Open questions to answer from measurements

Fill these in as you go. Each answer becomes a number or a sentence in the write-up.

- Which limit binds first on your chosen models: requests per minute or tokens per minute?
- What is the real TTFT and per-token speed by prompt size, and how heavy is the tail?
- How many requests can be in flight before latency degrades?
- How long does a 429 take to clear, and does `retry-after` match what you saw?
- How much does verification cost in tokens and time, and how many bad answers does it catch?
- Which retrieval setting gives the best quality per token?
- At what offered rate does your gateway start shedding, and is that the right point?
