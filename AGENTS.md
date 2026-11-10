# AGENTS.md: The Relay

Instructions for any LLM coding agent working in this repository. Read this file fully before writing code. If an instruction here conflicts with a request in chat, stop and ask the human owner instead of guessing.

## 1. What this project is

The Relay is a small multi-step assistant that answers questions about exactly three research papers, with citations. It is also a load-testing exercise: we measure how it behaves against the real Groq free tier and against a stand-in model server we write, then report honest numbers.

The three papers (exact versions only):

| Paper | arXiv id | Version |
|---|---|---|
| Attention Is All You Need | 1706.03762 | v7 |
| Moshi: a speech-text foundation model for real-time dialogue | 2410.00037 | v1 |
| Dynamic Chunking for End-to-End Hierarchical Sequence Modeling | 2507.07955 | v2 |

Reference docs (read them when a task touches their area): PRD, TRD, SRS, SDLC plan, Output To Be Achieved, architecture and design doc. They define requirement ids (for example FR-GW-05) that you should cite in commit messages and test names.

## 2. Golden rules (never break these)

1. **Never produce a wrong or uncited answer to stay fast.** Every reply is a cited answer, a cited extract, or an honest refusal. No fourth option.
2. **If the papers do not say, say so.** The assistant must refuse rather than guess. Do not add outside knowledge to prompts, fixtures or gold answers.
3. **Never touch, print, log or commit the API key.** It lives only in the environment variable `GROQ_API_KEY`. Never put it in code, config, traces, logs, tests, notebooks or docs.
4. **Do not invent facts.** Provider limits, header names, model names, prices, paper contents and page numbers must come from documentation, real responses or the PDFs. If you cannot verify something, write `TBD` and say what measurement would settle it.
5. **Every model call goes through the gateway.** Pipeline steps never call a backend directly and never retry on their own.
6. **No extra accounts, no tricks around limits.** Work inside the provider's limits and terms.
7. **Do not edit result files by hand.** `results.csv` and `traces.jsonl` are generated. Tables are produced only by the analysis script.
8. **Stay in scope.** Do not add features, libraries or files that no requirement asks for. Do not polish what has not been measured.

## 3. Repository layout

```
relay/
  AGENTS.md
  README.md
  .env.example              # placeholder only, never a real key
  config.yaml               # every threshold lives here
  requirements.txt          # pinned versions
  data/papers/              # the 3 PDFs (exact versions) + hashes
  src/
    ingest.py               # parse, chunk, tag, index
    retriever.py            # S2 tool, settings A/B/C
    pipeline.py             # state machine S1..S5
    gateway.py              # buckets, queue, retries, breaker, ladder, budget
    citecheck.py            # deterministic citation checker
    backends.py             # groq and stand-in, chosen by base_url
    trace.py                # trace records, jsonl writer
  standin/
    server.py               # FastAPI stand-in, OpenAI-style API
    calibrate.py            # fit parameters from probe data
  experiments/
    probe_groq.py           # real probing, logs headers and latencies
    loadgen.py              # open-loop scenarios
    run_quality.py          # question set across retrieval settings
    questions.yaml          # 15+ questions with gold answers
  analysis/
    make_tables.py          # results.csv -> Tables 1, 2a, 2b, 3
    explain_request.py      # print timeline of one request id
  results/
    real/  standin/         # raw outputs
  tests/
  notes/
    decisions.md            # running decision log
    assistant_log/          # human's conversation history, uncleaned
```

Create files in these places. Ask before adding new top-level folders.

## 4. Setup and commands

These are the standard commands. If one does not exist yet, create it as part of the task that needs it.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # human fills in the key locally

pytest -q                       # all tests
ruff check . && ruff format .   # lint and format
uvicorn standin.server:app --port 8000          # start stand-in
python experiments/loadgen.py --scenario steady --target standin
python analysis/make_tables.py  # print all tables from the raw files
python analysis/explain_request.py <request_id>
```

Python 3.11 or newer. Use `asyncio`, `httpx` (async, streaming), `fastapi`, `uvicorn`, `pymupdf`, `rank_bm25`, `pandas`, `pytest`. Do not add other dependencies without a reason written in `notes/decisions.md`.

## 5. Architecture rules

### Pipeline (src/pipeline.py)

Five steps: S1 Understand, S2 Retrieve (tool), S3 Decide, S4 Draft, S5 Verify.

- S3 chooses one of: answer, widen retrieval once, refuse. Maximum one widening.
- S5 failure triggers one redraft. A second failure means refuse. Maximum one redraft.
- The draft is buffered and released to the person only after verification passes. Do not stream unverified text to the person.
- `first_token_ms` means: request start to the first token of the final reply the person sees (answer, extract or refusal). A status message such as "searching..." is **not** a first token. Log it separately if you add it.
- Refusals should skip the draft model entirely (cheapest path).

### Gateway (src/gateway.py)

The gateway is the only place that knows about limits, queues and retries.

- Token buckets for requests per minute and tokens per minute. Reserve `prompt tokens + max_tokens` before sending, settle to actual usage afterwards.
- Concurrency cap on in-flight calls. Adaptive adjustment is a P1 feature.
- Priority: calls for requests already in progress (S4, S5) go before new requests (S1). Within a priority, earliest deadline first.
- Every request carries a deadline. Drop calls whose deadline has passed and mark the request given up. Never finish work for someone who has left.
- Admission control: accept, queue with an honest estimate, or shed with a fast busy reply.
- Daily budget counters persist to disk and survive restarts.
- Degradation ladder levels 0 to 4 as defined in the TRD, with hysteresis. Log every transition with its reason.
- Read rate-limit information from real responses and prefer it over local estimates.

### Retry policy

| Failure | Action |
|---|---|
| 429 | Wait at least `retry-after` plus random jitter, requeue, count against the retry budget |
| 5xx or connection error | At most 2 retries, exponential backoff, full jitter |
| Timeout before first byte | Cancel, retry once only if deadline and budget allow |
| Other 4xx | Do not retry |
| Daily limit exhausted | Do not retry, go to ladder level 4 |

Retries must be limited by a retry budget (share of recent calls, set in `config.yaml`). Never write an unbounded or immediate retry loop.

### Backends and the stand-in

- Both backends use the same OpenAI-style chat completions API with streaming. Only `base_url` changes (config value).
- The stand-in must support: slow replies, failures, 429 refusals with `retry-after` and rate-limit headers, slowdown as in-flight requests rise, streaming in pieces, slow-but-successful calls, and returning token `usage`.
- Stand-in parameters are calibrated from real probe data. Do not invent latency numbers. Until calibrated, mark defaults clearly as placeholders.
- State plainly in code comments and docs where the stand-in differs from the real service.

### Retrieval

- Zero provider tokens. Use local BM25 and/or local embeddings.
- Every chunk carries: chunk id, paper id, version, section, page, kind (prose, table, equation, caption, references).
- Three settings (A, B, C) selectable in config. Exact sizes are measured, not assumed.
- Do not strip page numbers or section headings. Citations depend on them.

### Citation checker (src/citecheck.py)

Deterministic, no model. It must verify that every cited chunk id exists in the retrieved set, every quoted span appears in its chunk, and every factual sentence carries a citation. It always runs, even at degraded levels.

## 6. Configuration and magic numbers

- Every threshold, limit, margin, timeout, patience value and ladder trigger lives in `config.yaml`. No numeric constants for these in code.
- Values that have not been measured are `TBD` in config. Code must fail loudly with a clear message if a required `TBD` is used in a real run, instead of silently defaulting.
- Config changes are logged in `notes/decisions.md` with the measurement behind them.

## 7. Timing, logging and data

- Use `time.perf_counter()` (monotonic) for every duration. Use wall-clock only for `start_time`.
- Every stage records: enqueue time, start, first byte, end, attempt number, status.
- Derive `queue_wait_ms` (waiting), `work_ms` (working) and `retry_ms` (retrying) per request.
- Every trace stores the git commit hash, config hash and seed.
- Log every real response's rate-limit headers (never the key).
- Write results incrementally so a crash does not lose data.

`results.csv` has exactly these columns, in this order:

```
request_id, scenario, source, start_time, first_token_ms, total_ms, status,
retries, tokens_in, tokens_out, retrieval_setting, answer_grade
```

- `source`: `real` or `stand-in`. Never mislabel.
- `status`: `ok`, `rate_limited`, `timeout`, `error`. Ask the human how given-up requests are mapped before deciding.
- `answer_grade`: `correct`, `wrong`, `refused_correctly`, `refused_wrongly`. Leave empty where no grading was done, and say so in the docs.
- Times in milliseconds. Percentages with one decimal in tables.

## 8. Load generation rules

- Open-loop only: arrivals follow a schedule regardless of earlier completions. Closed-loop generators hide overload.
- Record scheduled and actual send time. Report if the generator itself fell behind.
- Scenarios: steady, burst, sustained overload, per-minute limit halved, daily budget exhausted.
- Heavy load goes to the stand-in. Real-service runs are small and planned, because the daily quota is shared and limited.
- Do not run large or repeated real-service tests without the human's explicit go-ahead. State the estimated requests and tokens first.

## 9. Testing rules

- Every gateway behaviour has a unit test using a fake clock and fake backend: bucket refill, reservation and settle, retry-after honoured, retry budget, deadline drop, priority order, ladder transitions with hysteresis, daily budget persistence, circuit breaker states.
- The citation checker has tests for valid citations, missing ids, fabricated quotes and uncited claims.
- Include a seeded bad-draft test proving a failing draft never reaches the person.
- Include a contract test comparing stand-in response shape with captured real responses once they exist.
- Name tests after requirement ids where possible (for example `test_FR_GW_05_priority_order`).
- Do not mark a task done if tests were not run. Say exactly what you ran and what passed.

## 10. How to work (agent workflow)

1. **Read first.** Read the relevant docs and existing code before changing anything.
2. **Plan briefly.** State what you will change and the test that will prove it. Keep changes small, one component at a time.
3. **Implement the smallest thing** that passes the test.
4. **Run tests and lint.** Fix failures. Do not weaken or delete tests to make them pass.
5. **Report honestly.** Summarise what changed, what was tested, what you could not verify, and any assumption you made.
6. **Log decisions.** Add an entry to `notes/decisions.md`: the decision, the alternative, the measurement or reason, and the cost.

When a request is ambiguous, ask one specific question. When you find a conflict between docs, point it out instead of picking silently.

## 11. Code style

- Typed Python (type hints on public functions), small functions, clear names.
- Async I/O for anything touching the network. No blocking calls inside the event loop.
- No global mutable state except explicit, documented singletons (gateway state).
- Comments explain why, not what. Mark placeholders with `TBD:` and the measurement that will replace them.
- Errors are explicit. Never swallow exceptions silently. Never return a plausible-looking default when real data is missing.
- Keep prompts in one place (a `prompts` module or file) so they can be reviewed. Prompts must instruct: answer only from the supplied passages, cite chunk ids, say so when the passages do not support an answer.

## 12. Git and change control

- Small commits with messages that say what and why, citing requirement ids, for example `gateway: honour retry-after with jitter (FR-RTY-01)`.
- Feature branches such as `feat/gateway`. `main` must always run.
- Tag each phase gate (for example `phase-3-probe`).
- Run a secret scan before committing. `.env` stays in `.gitignore`.
- **Code freeze:** once the human declares `final-runs`, do not change code. If a blocking fix is unavoidable, flag which results become invalid and must be rerun.
- Never rewrite history or force-push without explicit approval.

## 13. Things you must not do

- Do not call the Groq API in loops, tests or CI without explicit approval.
- Do not hard-code limits, latencies, prices or thresholds.
- Do not stream unverified draft text to the person.
- Do not retry without a budget and jitter.
- Do not fabricate measurements, example outputs, percentiles or table values. Use `TBD` or "not measured".
- Do not present stand-in results as real results.
- Do not edit `results.csv` or `traces.jsonl` by hand, or compute report numbers outside `analysis/make_tables.py`.
- Do not write or rewrite the human's final write-up, reflection, or Vaani feedback in a way that hides what the assistant did. The submission should reflect the human's own choices, measurements and reasoning. You may suggest structure and check facts, but the human decides and must be able to explain every line.
- Do not clean up or alter the assistant conversation log.

## 14. Definition of done for any task

- [ ] Behaviour matches the cited requirement ids.
- [ ] Tests written and run, with results reported.
- [ ] Lint and format pass.
- [ ] No secrets, no magic numbers, no unverified facts.
- [ ] Decision logged in `notes/decisions.md` if a choice was made.
- [ ] The human can explain the change without help.

## 15. Escalate to the human when

- A real provider limit or header differs from what the docs or earlier results say.
- A change would spend significant real quota.
- A requirement is ambiguous or two documents disagree.
- A test fails in a way you cannot explain.
- Anything would require a second account, a workaround for a limit, or storing a secret.
