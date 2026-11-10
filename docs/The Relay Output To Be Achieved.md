# The Relay: Output To Be Achieved

**Version:** 1.0 draft **Related documents:** PRD, TRD, SRS, SDLC plan

This document defines what a finished submission contains, what each deliverable must prove, and the blank result tables you must fill from your own measurements. Where a number is unknown it says **TBD**. Never fill a cell from memory or a guess. Write "not measured" where you did not measure something.

## 1. What done looks like

You hand in a working multi-step assistant that answers from three pinned papers with citations and refuses honestly when the papers are silent. It runs against the real Groq free tier and against a stand-in service you built. You have measured the real service, calibrated the stand-in from those measurements, run at least three load shapes (the tables ask for five), and recorded every request in one raw file. A two-page write-up, in the required heading order, explains your choices and what each cost. Every number in the write-up can be recomputed from the raw file. You can defend all of it in a live conversation and change the system on the spot.

## 2. Deliverables checklist

| # | Deliverable | Format | Acceptance criteria |
| --- | --- | --- | --- |
| D1 | Pipeline code | Python repo | At least 4 steps, 1 tool call, 1 branch or loop, retrieval from the pinned papers, citations, refusal |
| D2 | Gateway | Code in repo | Enforces limits, queue, retries with budget, ladder, daily budget |
| D3 | Stand-in model service | Code in repo | Can be slow, fail, refuse on limits, slow under load, stream in pieces. Calibrated from real measurements |
| D4 | Load generator | Code in repo | Open-loop scenarios, writes the raw file |
| D5 | Question set | File | At least 15 questions you wrote, with gold answers and page references, including unanswerable ones |
| D6 | Raw results | `results.csv` | One row per request, exact columns, real and stand-in rows labelled |
| D7 | Trace data | JSON lines | Stage timings that support the latency breakdown |
| D8 | Analysis script | Code | Produces every table from the files |
| D9 | Write-up | Two pages maximum | Required headings in required order, all tables complete |
| D10 | Assistant conversation history | Export or prompts | Real and uncleaned |
| D11 | Reflection on working with the assistant | Half a page | Three key prompts, one error caught, one decision overruled, what is distinctly yours |
| D12 | Vaani feedback | A few lines | Written after actually visiting vaani.dev |

Do not include your API key anywhere in the submission.

## 3. Working-system outcomes

| # | Outcome | How it is shown |
| --- | --- | --- |
| W1 | Cited answer to an answerable question | Demo question with correct paper, section and page |
| W2 | Honest refusal to an unanswerable question | Demo question that the papers do not cover |
| W3 | A bad draft never reaches the person | Seeded bad draft caught by verification |
| W4 | A per-minute limit does not crash the system | Run that reaches the limit, 429 handled, retry-after honoured |
| W5 | Daily budget exhausted gives an honest reduced reply | Run where the budget runs out mid-way |
| W6 | Stand-in behaves like the real service | Percentile comparison and response shape comparison |
| W7 | Retrying made things worse in at least one case | Paired run with naive and budgeted retries |
| W8 | One slow request is explained | Timeline split into waiting, working and retrying, plus before or after first token |

## 4. Measured outcomes: blank tables to fill

Times in milliseconds, percentages with one decimal. Copy these into the write-up once filled.

### Table 1: Limits found in the real service

| Limit | Value you found | How you found it | Confidence (high, medium, low) |
| --- | --- | --- | --- |
| Requests per minute | TBD | TBD | TBD |
| Tokens per minute | TBD | TBD | TBD |
| Requests per day | TBD | TBD | TBD |
| Tokens per day | TBD | TBD | TBD |
| Requests in flight at once | TBD | TBD | TBD |
| What it returns when a limit is hit | TBD | TBD | TBD |
| Time until it recovers | TBD | TBD | TBD |

### Table 2a: Latency, one row per load scenario

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| Steady | TBD | TBD | TBD | TBD | TBD | TBD |
| Burst | TBD | TBD | TBD | TBD | TBD | TBD |
| Sustained overload | TBD | TBD | TBD | TBD | TBD | TBD |
| Per-minute limit halved | TBD | TBD | TBD | TBD | TBD | TBD |
| Daily budget exhausted | TBD | TBD | TBD | TBD | TBD | TBD |

### Table 2b: Outcomes, same rows

| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Steady | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| Burst | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| Sustained overload | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| Per-minute limit halved | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| Daily budget exhausted | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

### Table 3: Retrieval settings (at least three)

| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A: TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| B: TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| C: TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

### Notes that must accompany the tables

- Say which percentiles you computed and over how many requests.
- Say how you treat given-up requests in latency percentiles.
- Say which runs were real and which used the stand-in.
- Include one number nobody could have guessed without running the system, and say why it surprised you.
- Report answer quality on at least 15 of your own questions, and tokens used per answer.

## 5. Raw file specification

One file named `results.csv`, one row per request.

| Column | Meaning | Allowed values or unit |
| --- | --- | --- |
| `request_id` | Unique id | Text |
| `scenario` | Load shape or quality run name | Text |
| `source` | Where the model calls went | `real` or `stand-in` |
| `start_time` | When the request started | Timestamp |
| `first_token_ms` | Request start to first token of the final reply | Milliseconds |
| `total_ms` | Request start to last token | Milliseconds |
| `status` | Outcome | `ok`, `rate_limited`, `timeout`, `error` |
| `retries` | Retries for this request | Integer |
| `tokens_in` | Prompt tokens across the request | Integer |
| `tokens_out` | Output tokens across the request | Integer |
| `retrieval_setting` | Which setting was used | Text such as A, B, C |
| `answer_grade` | Quality grade | `correct`, `wrong`, `refused_correctly`, `refused_wrongly` |

Decisions to record in your write-up:

- How given-up requests appear. The allowed `status` values do not include a separate given-up value, so decide whether they are `timeout`, and say so.
- Load-run rows from the stand-in use canned text, so decide whether `answer_grade` stays empty for them and state it.
- Use the same timing definitions as the design doc, and say if first token for a refusal or an extract is the first token of that reply.

## 6. Write-up outcome

Two pages of writing, plus code, raw results and conversation history. Use these headings in this order:

| # | Heading | What it must show |
| --- | --- | --- |
| 1 | What I built and why | The design and the one decision that shaped it most |
| 2 | The real service and my stand-in | Limits found and how, stand-in behaviour, calibration, biggest difference from the real service |
| 3 | Results | The tables, percentiles and request counts, real vs stand-in labels, answer quality, tokens per answer, the number nobody could guess |
| 4 | Where the time goes | One slow request split into waiting, working and retrying, and before or after first token, with proof |
| 5 | When a limit is hit | What happens at the per-minute and per-day limits, who is delayed, dropped or weakened, and why |
| 5a | Retrieval: quality versus cost | At least three settings, one chosen with reasons, what happens to retrieval when the limit is close |
| 5b | How this library will mislead a naive retriever | Three ways, each with an example question |
| 6 | Retries | Your policy and one case where retrying made things worse |
| 7 | What the person sees | The reply when the daily budget is gone at 3pm |
| 8 | What I'm unsure about, what I left out, what I learned | Least certain assumption and what breaks, two things not built, one new thing taught in your own words |
| 9 | Your feedback on Vaani | What stood out, what confused you, one thing to change |
| 10 | How I worked with the assistant | History, three key prompts, one error caught, one overruled decision, what is distinctly yours |

The brief places the two retrieval headings after heading 5 and the Vaani heading before the final section. Check the order against the brief before you submit.

## 7. Outcome targets

Set these after you have real measurements. Write the target, the result, and one sentence on the gap.

| Outcome | Target | Result | Gap and cost |
| --- | --- | --- | --- |
| Time to first token p95, steady | TBD | TBD | TBD |
| Total time p95, steady | TBD | TBD | TBD |
| Wrong answers on the question set | As low as possible | TBD | TBD |
| Correctly refused unanswerable questions | TBD | TBD | TBD |
| Given-up rate in sustained overload | TBD | TBD | TBD |
| Retries per request in sustained overload | Bounded by the budget | TBD | TBD |
| Stand-in versus real, p50 and p95 | Within a stated percentage | TBD | TBD |

## 8. Evidence map

| The brief requires | Where it is shown |
| --- | --- |
| At least four steps, a tool call, a branch or loop | Pipeline code, trace, heading 1 |
| Retrieval from the pinned papers | Ingestion code, file hashes |
| Citations and honest refusal | Question set, Table 3, heading 3 |
| Real Groq on the free tier | Probe data, real rows in the raw file |
| Stand-in that can be slow, fail, refuse, slow under load, stream | Stand-in code, heading 2 |
| Calibration from real measurements | Calibration script, comparison, heading 2 |
| At least three load shapes | Tables 2a and 2b, raw file |
| Raw results for real and stand-in | `results.csv`, trace files |
| Latency split for one slow request | Heading 4, request timeline output |
| Per-minute and per-day limit behaviour | Heading 5 |
| Retry policy and a case it hurt | Heading 6, paired run |
| The 3pm reply | Heading 7 |
| At least three retrieval settings | Table 3, heading 5a |
| Three retriever traps | Heading 5b |
| At least 15 own questions | Question set, Table 3 |
| Vaani feedback | Heading 9 |
| Assistant history and reflection | Heading 10, history file |
| Under two pages, key not included | Page count and secret scan |

## 9. Definition of done

Tick every line before submitting:

- [ ] Pipeline, gateway, stand-in, load generator and analysis script all run from a clean clone.
- [ ] Tables 1, 2a, 2b and 3 are complete, with "not measured" where true.
- [ ] Every table value is reproduced by the analysis script from the raw file.
- [ ] Real and stand-in rows are labelled in the file and in the write-up.
- [ ] The write-up is two pages or less, with every heading present in order.
- [ ] A retry case that made things worse is shown with numbers.
- [ ] The daily-budget reply is described exactly, and an example is shown.
- [ ] At least three retrieval settings and three retriever traps are covered.
- [ ] The conversation history is real and uncleaned.
- [ ] No key in the repo or its history. No extra accounts were used.
- [ ] You have rehearsed three live changes and can say what you expect before running each.

## 10. Stretch outcomes (only if time allows)

- Sentence-by-sentence gated streaming with a measured time to first token comparison against the buffered mode.
- A small HTML dashboard generated from the raw file.
- A second real run on another day to check whether the limits you found changed.
