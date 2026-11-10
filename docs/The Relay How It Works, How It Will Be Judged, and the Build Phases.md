# The Relay: How It Works, How It Will Be Judged, and the Build Phases

A working guide for the assignment: what happens to one request from start to finish, how reviewers are likely to evaluate the submission, and a phase-by-phase plan saying what to build and test at each step.

This pairs with the architecture and system design doc. That doc says what the components are. This one says how they behave, how they will be checked, and in what order to build them.

**Important:** the brief does not publish a scoring rubric or weights. The evaluation section below is inferred from what the brief says reviewers will read, recompute and ask about. Treat it as a strong guide, not an official marking scheme.

## 1. The system in one paragraph

A person asks a question about three papers. The system works out what is being asked, retrieves passages with a local tool, decides whether the papers can answer it, drafts a cited answer, and verifies the draft before releasing it. Every model call goes through one gateway that enforces the provider's limits, queues and prioritises work, retries carefully, and steps down to cheaper behaviour as limits get close. Behind the gateway is either the real Groq service or a stand-in server you wrote, switched by one config value. Every request writes a trace, and every table in the write-up is computed from the raw results file.

## 2. Step-by-step working: the life of one request

### Worked example

Question A (answerable): *"How many parallel attention heads does the base Transformer use?"* Check the exact answer and page in the PDF yourself before using it as a gold answer.

Question B (unanswerable): a question none of the papers cover, for example about image classification benchmarks. Confirm by searching the PDFs that they do not cover it.

### The path

```
arrive -> admit -> S1 understand -> S2 retrieve -> S3 decide -> S4 draft -> S5 verify -> release -> log
                                                      |           |           |
                                                      +-- refuse -+-----------+--> refusal reply
```

| # | What happens | Where it runs | What gets recorded |
| --- | --- | --- | --- |
| 1 | **Request arrives.** It gets an id, a start time, and a deadline (start time plus the patience limit you chose) | Intake | `request_id`, `start_time`, deadline |
| 2 | **Admission check.** The gateway estimates the token cost and current queue wait. It accepts, queues with an honest wait estimate, or sheds with a fast "busy" reply if no deadline can be met | Gateway | Queue length at arrival, admission decision |
| 3 | **S1 Understand.** A short model call turns the question into a search query and a coarse label (in scope, off topic, ambiguous). Trivial cases may skip the model | Gateway, then backend | Stage times, tokens |
| 4 | **S2 Retrieve (tool call).** A local retriever returns the top pieces with paper, version, section, page and chunk id. No Groq tokens are used | Local | Retrieval setting, scores, chunk ids |
| 5 | **S3 Decide.** Scores and rules choose one of three branches: answer, widen retrieval once and re-decide, or refuse | Local | Branch taken |
| 6 | **S4 Draft.** The larger model is told to answer only from the supplied pieces, cite chunk ids, and say so if the pieces do not support an answer. Output streams into a buffer, not to the person | Gateway, then backend | Time to first byte from model, tokens in and out |
| 7 | **S5 Verify.** A deterministic citation checker confirms every cited id exists, every quoted span appears in its chunk, and every factual sentence is cited. An optional small-model check tests whether the passage supports the claim | Local, then optional gateway call | Pass or fail, reason |
| 8 | **On verify failure.** Redraft once with the failure reason. If it fails again, refuse | Pipeline | Redraft count |
| 9 | **Release.** The verified answer (or extract, or refusal) streams to the person. The first token released stamps `first_token_ms` | Intake | `first_token_ms`, `total_ms` |
| 10 | **Finalise.** Token counts settle against real usage, the trace is written, and a row is appended to `results.csv` | Logging | All CSV columns |

### What happens inside every gateway call (steps 3, 6 and 7)

1. Check the request's deadline. If it has already passed, drop the call and mark the request given up.
2. Check the daily budget. If it is exhausted, do not call the model, go to the extractive or refusal path.
3. Reserve tokens (prompt plus `max_tokens`) from the per-minute token bucket, and a slot from the per-minute request bucket. Wait in the priority queue if they are not available.
4. Take a concurrency slot.
5. Send the call. Read the rate-limit headers on the response and update the gateway's picture of remaining capacity.
6. On success, settle the token reservation to the real usage.
7. On failure, apply the retry policy: honour `retry-after` with jitter for 429s, limited backoff for 5xx and timeouts, no retry for other client errors, and stop when the retry budget or the deadline runs out.

### How the same system treats four different requests

| Case | S3 decision | Draft model called? | Reply |
| --- | --- | --- | --- |
| Question A on a quiet system | Answer | Yes, full retrieval | Cited answer, fast |
| Question B (not in the papers) | Refuse | **No** (cheapest path) | Honest refusal saying the papers do not cover it |
| Question A when the token bucket is nearly empty | Answer | Yes, smaller model and fewer pieces (ladder L1 or L2) | Shorter cited answer |
| Question A after the daily budget is gone | Answer, extractive | No | Top cited passages shown verbatim, labelled as an extract, plus when generation returns |

## 3. How the system should behave under pressure

| Situation | What the system does | Who pays the cost |
| --- | --- | --- |
| Steady load below the limits | Normal path | Nobody |
| Sudden burst | Queue absorbs a short spike. Calls for requests already in progress go first. New arrivals wait, and some are shed with an honest "busy" if no deadline can be met | Late arrivals wait or are told to retry |
| Sustained overload | Admission control sheds early, the ladder steps down (skip model verify, smaller model, fewer pieces, extract only), retries are budgeted | Some people get weaker answers, some get "busy" |
| Per-minute limit reached | Token and request buckets hold calls back, `retry-after` is honoured, new work queues. The ladder steps down before the bucket is empty | Waiting time rises for new requests |
| Per-minute limit halved | Same logic, lower ceiling, ladder triggers sooner | More weaker answers and more shedding |
| Daily budget exhausted | No model calls at all. Retrieval-only extracts and honest refusals | Everyone gets a reduced answer, but nobody gets a guess |
| Upstream slow but succeeding | Stall detection and per-call timeouts. Circuit breaker opens if it persists | Some requests fall back to extracts |
| Retrieval tool times out | One retry, then refuse rather than answer without evidence | That request |

**Non-negotiable rule at every level:** the output is a cited answer, a cited extract, or a refusal. Never an uncited guess.

## 4. How the submission will be evaluated

### What the brief says reviewers will do

1. **Read the results tables first.** The brief says reviewers read them first.
2. **Recompute the numbers from your raw file.** Every table figure must be computable from `results.csv`.
3. **Check that your choices follow from your measurements** and that you state what each choice costs. The brief says this is what they care about.
4. **Read your assistant conversation history, uncleaned,** and look at how you directed it, checked it and overruled it.
5. **Hold a short live conversation** where you explain results and change your system. Only submit what you can defend.

### Likely evaluation criteria (inferred)

| Area | What reviewers check | Strong evidence | Weak or risky signs |
| --- | --- | --- | --- |
| Pipeline works | At least 4 steps, a tool call, a branch or loop, retrieval from the exact paper versions, citations, refusal when papers are silent | Demo questions with correct page citations and correct refusals | Answers without citations, guessing when papers do not say |
| Real service understanding | Table 1 with how each limit was found and a confidence level | Limits read from docs and response headers, a captured 429 and recovery time, honest "low confidence" labels | Limits quoted from memory, "not measured" hidden or guessed |
| Stand-in quality | Can be slow, fail, refuse on limits, slow under load, stream piecewise. Calibrated from real data. Differences stated | Real and stand-in latency percentiles compared side by side | A stand-in with made-up numbers and no comparison |
| Load results | At least three load shapes, percentiles with request counts, real vs stand-in labelled, given-up counted separately | Tables that match the CSV, a number nobody could have guessed without running it | Averages only, no percentiles, everything succeeding |
| Latency explanation | One slow request split into waiting, working and retrying, and whether the delay was before or after first token | Trace timeline that proves the split | A story with no trace |
| Limit handling | Who is delayed, who is dropped, who gets a weaker answer, and why, at both the per-minute and per-day limit | Clear policy that matches the measured behaviour | Vague "we retry" statements |
| Retries | A stated policy and one case where retrying made things worse | A paired run (naive vs budgeted) with numbers | Only saying retries are good |
| What the person sees | A specific reply when the daily budget is gone | A real example reply and how it is produced | "An error message is shown" |
| Retrieval | At least 3 settings compared, one chosen with reasons, plus three ways the library misleads a naive retriever with example questions | Table 3 with quality, tokens and TTFT, and real failing questions | One setting only, hypothetical examples |
| Answer quality | At least 15 questions you wrote, graded, tokens per answer | Questions include unanswerable ones, grading rubric stated | Only easy questions |
| Trade-off honesty | States what was given up for each goal | Explicit "I gave up X for Y because measurement Z" | Claiming nothing was sacrificed |
| Working with the assistant | Three key prompts, one error you caught and how, one decision where you overruled it, what is distinctly yours | Real examples from the conversation log | A cleaned-up story or a log that contradicts it |
| Presentation | Two pages maximum, headings in the required order, Vaani feedback, no key in the files | Complete tables, no skipped heading | Missing section, over length, secret in repo |
| Live defence | Can explain any number and change the system on the spot | Config-driven system, a script that explains any request id | Cannot explain code you submitted |

### Things that cost credibility

- Table numbers that do not match `results.csv`.
- Limits, prices or behaviour stated without a source or a measurement.
- A stand-in run presented as if it were the real service.
- Tables with no failures, no timeouts and no given-up requests under overload. It looks like the load was not real.
- Retries reported as fine without any case where they hurt.
- The API key anywhere in the submission.
- Extra accounts or tricks to get around Groq's limits. The brief forbids them.
- Hiding the assistant's mistakes.

### Questions you should expect in the live conversation

- Why did you choose these numbers for your gateway limits and patience?
- Show me where the time went for request X.
- What happens if the per-minute limit is halved again right now?
- Why does your system prioritise calls the way it does, and who loses?
- Turn off the verifier. What changes in quality, tokens and latency?
- What is your least certain assumption, and what breaks if it is wrong?
- Change the retrieval setting live and tell me what you expect before you run it.

## 5. Build phases

Total estimate is about 8 hours of real work, matching the brief's 6 to 8 hours. Do not move to the next phase until the exit criteria are met. Every phase ends with something saved for the write-up.

### Phase 0: Setup and ground rules (about 30 min)

**Build**

- Private repo, virtual environment, folder layout from the design doc.
- Groq account, key in `.env`, `.env` in `.gitignore`.
- Download the three PDFs at the exact versions, record hashes.
- Fix the `results.csv` schema and a `decisions.md` log. Start saving the assistant conversation now.

**Test**

- One tiny real call to Groq succeeds and prints the headers you will later use.
- `git status` shows no key or secret.

**Exit when:** key works, papers verified at the right versions, schema written down.

**Save for write-up:** versions and hashes, first look at limits in the docs.

### Phase 1: Ingestion and retrieval (about 1 hour)

**Build**

- Page-aware text extraction, chunking with metadata (paper, version, section, page, kind).
- Retriever with the three settings (piece size, number fetched, re-rank on or off) controlled by config.
- Local index (BM25, embeddings or both). No Groq tokens.

**Test**

- Ten hand-picked queries. For each, check the returned pieces by eye and confirm the page number is right.
- Look specifically at a table, an equation and a figure caption. Note what extraction does to them.
- Try one query that uses the word "chunking" and see which paper wins.

**Exit when:** citations map to the right page for your spot checks, and you have at least one real example of a retrieval mistake.

**Save for write-up:** the retrieval mistake examples (they feed the "mislead a naive retriever" section).

### Phase 2: Pipeline v0 on the real service (about 45 min)

**Build**

- S1 to S5 as a simple sequential pipeline calling Groq directly, with the branch (refuse) and the loops (widen once, redraft once).
- Trace writer that records stage start, first byte and end times.
- Deterministic citation checker.

**Test**

- Five questions: three answerable, two unanswerable. Confirm citations and refusals.
- Force a bad draft (for example, a fake citation id) and confirm the verifier catches it and the redraft loop runs.
- Confirm streaming gives a measurable first byte.

**Exit when:** the pipeline answers, cites and refuses correctly on those five, and every stage appears in the trace.

**Save for write-up:** a sample trace, early token counts per step.

### Phase 3: Probe the real service (about 30 min of work, spread over runs)

**Build**

- Probe script that sends a modest, planned set of requests: different prompt sizes, a few at light concurrency, and a small deliberate push to the per-minute limit.
- Logger for every response header and timing.

**Test**

- Capture a real 429: the status, body, headers and how long until calls work again.
- Find which limit binds first (requests or tokens per minute) for your chosen models.
- Do not try to exhaust the daily budget on the real service. Read it from the docs and headers, and test exhaustion on the stand-in.

**Exit when:** Table 1 is drafted with a source and a confidence level for every row, and the raw probe data is saved.

**Save for write-up:** Table 1, probe latency distribution, quota used so far.

### Phase 4: Gateway (about 1 hour)

**Build**

- Token buckets (requests and tokens per minute), concurrency cap, priority queue, deadline propagation.
- Retry policy with `retry-after`, jitter and a retry budget.
- Daily budget counter persisted to disk, circuit breaker, degradation ladder, admission control.
- Move every pipeline model call behind it.

**Test** (against a fake backend with a controllable clock, no real quota)

- Bucket refill and reservation settle correctly.
- A 429 with `retry-after` is honoured.
- An expired deadline drops the call.
- Ladder levels change at the thresholds you set.
- A restart does not forget the day's spend.

**Exit when:** these behaviours pass as repeatable tests.

**Save for write-up:** thresholds and why you chose them, the priority order and its cost.

### Phase 5: Stand-in server (about 1 hour)

**Build**

- API-compatible service with streaming, `usage`, rate-limit headers and 429.
- Controllable slowness, failure rate, load-dependent slowdown, slow-but-successful calls, daily limit.
- Calibration script that fits parameters from the Phase 3 probe data.

**Test**

- Send the same request mix to the real service (small) and the stand-in. Compare time to first token and total time at p50 and p95.
- Compare the 429 response shape.
- Confirm that raising in-flight requests raises latency.

**Exit when:** the stand-in's percentiles are close enough to the real ones that you can say how close, and you can list where it differs.

**Save for write-up:** the comparison, calibration method, differences.

### Phase 6: Load generator and scenarios (about 45 min)

**Build**

- Open-loop generator: requests are sent on schedule whether or not earlier ones have finished.
- Scenarios: steady, burst, sustained overload, per-minute limit halved, daily budget exhausted.
- Incremental writes to `results.csv` and traces.

**Test**

- A short dry run for each scenario.
- Check that the generator itself keeps up (scheduled vs actual send time) and note CPU use.
- Check that given-up requests are counted separately.

**Exit when:** each scenario runs end to end and writes valid CSV rows.

**Save for write-up:** scenario definitions, raw outputs.

### Phase 7: Quality and retrieval experiments (about 45 min)

**Build**

- At least 15 questions you wrote, with gold answers and supporting pages. Include several the papers cannot answer.
- Runner that asks every question under each retrieval setting and records grade, tokens and latency.

**Test**

- Grade with a written rubric (`correct`, `wrong`, `refused_correctly`, `refused_wrongly`). Hand-check citations on a sample.
- Include the retry experiment: the same overload run with naive retries and with your budgeted policy.

**Exit when:** Table 3 data exists for at least three settings and you can justify one choice.

**Save for write-up:** Table 3, one retry case that backfired, answer quality numbers.

### Phase 8: Analysis and tables (about 30 min)

**Build**

- One script that reads `results.csv` (and the trace file where needed) and prints Tables 2a, 2b and 3, plus the Table 1 inputs.

**Test**

- Recompute two or three numbers independently (for example, with a quick pandas one-liner) and confirm they match.
- Check that percentages add up and request counts match the rows.
- Find the number you could not have guessed without running it.

**Exit when:** every table value can be traced to the file and no cell is blank (use "not measured" where true).

**Save for write-up:** final tables, the surprising number.

### Phase 9: Write-up and defence preparation (about 1 hour 15 min)

**Build**

- The two-page write-up with every required heading in the right order.
- Vaani feedback after visiting the site.
- The assistant section, with the conversation log and your three prompts, the error you caught and the decision you overruled.
- A small `explain_request.py` that prints the stage timeline for any request id.

**Test**

- Check the page count and heading order against the brief.
- Confirm no key is in the repo or history.
- Practise three live changes: halve a limit, switch a retrieval setting, turn the verifier off. Say what you expect before you run each.

**Exit when:** you can defend every number and every choice out loud.

## 6. Suggested four-day schedule

| Day | Phases | Notes |
| --- | --- | --- |
| 1 | 0, 1, 2 | Light real use. Get a working pipeline before anything else |
| 2 | 3, 4, 5 | Probe real service once, early. Build gateway and calibrated stand-in |
| 3 | 6, 7, 8 | Heavy runs on the stand-in. Spend real quota only on quality questions and one small real steady run |
| 4 | 9 plus buffer | Write-up, defence practice, fixes. Keep time spare for reruns |

Plan the real quota across days and keep a reserve. Running out is part of the problem, but you should run out on purpose, on the stand-in.

## 7. Pre-submission checklist

- [ ] Pipeline has at least 4 steps, a tool call and a branch or loop.
- [ ] Answers cite paper, section and page, and refuse when the papers do not say.
- [ ] Tables 1, 2a, 2b and 3 are complete, with "not measured" where true.
- [ ] Every table number matches `results.csv`, and real and stand-in rows are labelled.
- [ ] Percentiles state which percentiles and how many requests.
- [ ] At least three load shapes were run (the table asks for five rows).
- [ ] At least 15 of your own questions, including unanswerable ones.
- [ ] One slow request has a waiting, working and retrying split with proof.
- [ ] A retry case that made things worse is documented.
- [ ] The daily-budget reply is described precisely.
- [ ] Three retrieval settings compared, one chosen with reasons.
- [ ] Three ways the library misleads a naive retriever, each with an example.
- [ ] Vaani feedback written after actually visiting the site.
- [ ] Conversation history included uncleaned, plus the half-page reflection.
- [ ] Two pages maximum, headings in the brief's order.
- [ ] No API key anywhere. No extra accounts used.

## 8. Common mistakes to avoid

- Polishing the pipeline for hours before measuring anything.
- Using a closed-loop load generator that hides overload.
- Letting the load generator and stand-in compete for the same CPU without saying so.
- Spending the daily quota on early experiments.
- Writing the tables by hand instead of generating them.
- Treating a status message as the first token.
- Making the stand-in so harsh or so gentle that its results prove nothing.
- Submitting code you cannot explain. The live conversation will expose it.
