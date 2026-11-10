# The Relay: Software Requirements Specification (SRS)

**Version:** 1.0 draft **Style:** modelled on the IEEE 830 and ISO/IEC/IEEE 29148 structure, kept short **Related documents:** PRD, TRD, SDLC plan, Output To Be Achieved

Requirement wording: **shall** means mandatory. Priority: **P0** must have, **P1** should have, **P2** nice to have. Verification method: **T** test, **I** inspection, **D** demonstration, **A** analysis. Values marked **TBD** are set from your measurements.

## 1. Introduction

### 1.1 Purpose

This document specifies the requirements of The Relay: a multi-step assistant that answers questions from three research papers with citations, together with the gateway, stand-in service, load generator and analysis tools needed to measure how it behaves under load.

### 1.2 Scope

In scope: ingestion and retrieval of three pinned papers, a five-step pipeline, a gateway that controls all model calls, a real backend (Groq free tier), a stand-in backend, an open-loop load generator, request tracing, and table generation from a raw results file.

Out of scope: user accounts, billing, a polished web interface, multi-turn memory, questions outside the three papers, and any attempt to bypass the provider's limits.

### 1.3 Definitions

| Term | Meaning |
| --- | --- |
| Gateway | The single component through which every model call passes |
| Stand-in | A model service you write that imitates the provider's behaviour |
| TTFT | Time to first token: request start to the first token of the final reply the person sees |
| Given up | A request abandoned because its patience deadline passed |
| Extract | A reply made of the top cited passages with no generated text |
| Ladder | The ordered set of degradation levels 0 to 4 |
| Open-loop | Load whose arrivals follow a schedule regardless of earlier completions |

### 1.4 References

- The assignment brief (The Relay).
- The three papers: arXiv 1706.03762 v7, arXiv 2410.00037 v1, arXiv 2507.07955 v2.
- Groq documentation for rate limits and the chat completions API (verify the current version).
- The PRD, TRD, SDLC plan and Output To Be Achieved documents.

### 1.5 Document overview

Section 2 describes the product. Section 3 lists the numbered requirements. Section 4 gives the verification and traceability plan.

## 2. Overall description

### 2.1 Product perspective

The Relay is a self-contained Python system. It talks to one of two interchangeable backends through the same API shape. The real backend is an external service with limits that can change without notice. The stand-in is local.

### 2.2 Product functions (summary)

1. Ingest and chunk the papers with page and section metadata.
2. Retrieve passages locally with three switchable settings.
3. Run understand, retrieve, decide, draft and verify, with a refusal branch and two bounded loops.
4. Control all model calls through one gateway with queueing, limits, retries and a degradation ladder.
5. Imitate the provider with a calibrated stand-in.
6. Generate load and record every request.
7. Produce all report tables from the raw file.

### 2.3 User classes

| Class | Needs |
| --- | --- |
| Question asker | Correct, cited, fast replies, or an honest explanation |
| Operator | Visibility into queue, limits and degradation |
| Reviewer | Reproducible numbers and a system that can be changed live |

### 2.4 Operating environment

Python 3.11 or newer on a laptop or a notebook environment, internet access to the provider for real runs, three cooperating processes (stand-in, pipeline with gateway, load generator).

### 2.5 Design and implementation constraints

- Language: Python, C++ or Go. Python assumed.
- Real model calls use only your own free account and key.
- The key shall not appear in the submission.
- The provider's terms shall be respected, with no extra accounts or tricks.
- At most two pages of written submission, plus code, raw results and assistant history.

### 2.6 Assumptions and dependencies

- Provider limits differ by plan and model and can change without notice.
- The provider's responses include enough rate-limit information to read at runtime. Verify this in the probe.
- The papers can be downloaded at the pinned versions and their text extracted well enough for retrieval.

## 3. Specific requirements

### 3.1 External interface requirements

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| EI-01 | The system shall call the backend through an OpenAI-style chat completions interface with streaming | P0 | I |
| EI-02 | The backend base URL shall be selectable by one configuration value | P0 | I |
| EI-03 | The stand-in shall return the same status codes, error shape and rate-limit headers as the real service | P0 | T |
| EI-04 | The stand-in shall provide an admin interface to change behaviour and reset counters | P1 | D |
| EI-05 | The system shall expose a command-line way to ask a question and print the reply with citations | P1 | D |
| EI-06 | The API key shall be read from an environment variable and never logged | P0 | I |

### 3.2 Functional requirements

#### Ingestion (FR-ING)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-ING-01 | The system shall use exactly the pinned paper versions and record file hashes | P0 | I |
| FR-ING-02 | The system shall extract text per page and keep section headings | P0 | T |
| FR-ING-03 | Each chunk shall carry paper, version, section, page, chunk id and content kind | P0 | I |
| FR-ING-04 | Tables, equations and captions shall be tagged so their handling can be evaluated | P1 | I |

#### Retrieval (FR-RET)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-RET-01 | The retriever shall return ranked passages with citation metadata | P0 | T |
| FR-RET-02 | The retriever shall use no provider tokens | P0 | I |
| FR-RET-03 | The retriever shall support at least three settings that differ in piece size, pieces fetched and re-ranking | P0 | T |
| FR-RET-04 | The retrieval setting shall be switchable by configuration | P1 | D |
| FR-RET-05 | The retriever shall expose scores so the pipeline can decide whether evidence is sufficient | P0 | T |

#### Pipeline (FR-PIP)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-PIP-01 | The pipeline shall have at least four steps: understand, retrieve, decide, draft and verify | P0 | I |
| FR-PIP-02 | The pipeline shall make at least one tool call (the retriever) | P0 | I |
| FR-PIP-03 | The pipeline shall contain at least one branch (refuse) and bounded loops (widen retrieval once, redraft once) | P0 | T |
| FR-PIP-04 | The draft step shall answer only from supplied passages and cite chunk ids | P0 | T |
| FR-PIP-05 | The system shall refuse, saying the papers do not say, when evidence is insufficient | P0 | T |
| FR-PIP-06 | The verify step shall run a deterministic citation check on every draft | P0 | T |
| FR-PIP-07 | A draft that fails verification shall not be shown to the person | P0 | T |
| FR-PIP-08 | After one failed redraft the system shall refuse | P0 | T |
| FR-PIP-09 | No pipeline step shall call a backend or retry on its own | P0 | I |
| FR-PIP-10 | Each request shall carry a deadline, and work for a past-deadline request shall be cancelled | P0 | T |

#### Gateway (FR-GW)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-GW-01 | Every model call shall pass through the gateway | P0 | I |
| FR-GW-02 | The gateway shall enforce requests per minute and tokens per minute using buckets sized from measured limits with a margin | P0 | T |
| FR-GW-03 | The gateway shall reserve prompt plus maximum output tokens before a call and settle to actual usage after | P0 | T |
| FR-GW-04 | The gateway shall cap in-flight calls | P0 | T |
| FR-GW-05 | The gateway shall prioritise calls for requests already in progress over new requests | P0 | T |
| FR-GW-06 | The gateway shall order calls within a priority by earliest deadline | P1 | T |
| FR-GW-07 | The gateway shall perform admission control, choosing accept, queue with an estimated wait, or shed | P0 | T |
| FR-GW-08 | The gateway shall track daily requests and tokens and persist them across restarts | P0 | T |
| FR-GW-09 | The gateway shall read rate-limit information from real responses and prefer it over local estimates | P1 | T |
| FR-GW-10 | The gateway shall adapt the in-flight cap to observed latency | P1 | A |
| FR-GW-11 | The gateway shall sample and record queue length at a fixed interval | P0 | T |

#### Retries (FR-RTY)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-RTY-01 | On 429 the system shall wait at least the retry-after time plus random jitter | P0 | T |
| FR-RTY-02 | On 5xx or connection error the system shall retry at most twice with exponential backoff and full jitter | P0 | T |
| FR-RTY-03 | Retries shall not exceed a configured share of recent calls | P0 | T |
| FR-RTY-04 | The system shall not retry other client errors or when the daily limit is exhausted | P0 | T |
| FR-RTY-05 | Retries shall stop when the request deadline cannot be met | P0 | T |
| FR-RTY-06 | A circuit breaker shall open on repeated failures or slow calls and recover through a probe | P1 | T |
| FR-RTY-07 | The system shall detect a stalled stream and cancel it | P1 | T |

#### Degradation (FR-DEG)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-DEG-01 | The system shall implement ladder levels 0 to 4 as defined in the TRD | P0 | T |
| FR-DEG-02 | Level transitions shall use hysteresis and be logged with the reason | P1 | T |
| FR-DEG-03 | At every level the output shall be a cited answer, a cited extract or a refusal | P0 | T |
| FR-DEG-04 | When the daily budget is exhausted the system shall make no model calls and shall return an extract with an honest explanation | P0 | D |
| FR-DEG-05 | A reduced reply shall say that it is reduced | P0 | I |
| FR-DEG-06 | When no deadline can be met the system shall return a fast busy reply with a suggested retry time | P0 | D |

#### Stand-in (FR-SIM)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-SIM-01 | The stand-in shall be able to respond slowly with a configurable latency distribution | P0 | T |
| FR-SIM-02 | The stand-in shall be able to fail with configurable rates of server errors, resets and hangs | P0 | T |
| FR-SIM-03 | The stand-in shall refuse with 429 when its configured limits are reached | P0 | T |
| FR-SIM-04 | The stand-in shall become slower as in-flight requests rise | P0 | T |
| FR-SIM-05 | The stand-in shall stream replies a piece at a time | P0 | T |
| FR-SIM-06 | The stand-in shall support slow-but-successful calls | P1 | T |
| FR-SIM-07 | The stand-in shall return token usage so accounting matches the real service | P0 | T |
| FR-SIM-08 | Stand-in parameters shall be calibrated from measurements of the real service and the comparison documented | P0 | A |
| FR-SIM-09 | The system shall state where the stand-in differs most from the real service | P0 | I |

#### Load generation (FR-LD)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-LD-01 | The load generator shall be open-loop | P0 | I |
| FR-LD-02 | The system shall run at least steady, burst and sustained overload, and shall also run per-minute limit halved and daily budget exhausted | P0 | D |
| FR-LD-03 | The generator shall record scheduled and actual send time | P1 | T |
| FR-LD-04 | Requests passing the patience limit shall be counted as given up | P0 | T |
| FR-LD-05 | Real and stand-in runs shall be labelled in the raw file | P0 | I |
| FR-LD-06 | Raw results shall be written incrementally so a crash does not lose data | P1 | T |

#### Logging and analysis (FR-LOG)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-LOG-01 | Every request shall produce one row in `results.csv` with the columns fixed by the brief | P0 | I |
| FR-LOG-02 | Every request shall produce a trace with stage timings and the code commit hash | P0 | I |
| FR-LOG-03 | Durations shall use a monotonic clock | P0 | I |
| FR-LOG-04 | The system shall derive waiting, working and retrying time per request | P0 | T |
| FR-LOG-05 | One analysis script shall produce Tables 1, 2a, 2b and 3 from the files alone | P0 | T |
| FR-LOG-06 | A script shall print the timeline of any request id | P1 | D |
| FR-LOG-07 | Percentiles and request counts shall be stated for every latency figure | P0 | I |

#### Evaluation (FR-EVAL)

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| FR-EVAL-01 | The question set shall contain at least 15 questions written by you, including unanswerable ones | P0 | I |
| FR-EVAL-02 | Each answer shall be graded correct, wrong, refused correctly or refused wrongly using a written rubric | P0 | I |
| FR-EVAL-03 | Quality, tokens per answer and latency shall be reported for at least three retrieval settings | P0 | A |
| FR-EVAL-04 | Citations shall be hand-checked on a sample | P1 | I |
| FR-EVAL-05 | One paired run shall compare naive retries with the budgeted policy under overload | P0 | A |

### 3.3 Performance requirements

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| NFR-PERF-01 | TTFT p95 under steady load shall be at or below a target set from measurement (TBD) | P1 | A |
| NFR-PERF-02 | Total time p95 under steady load shall be at or below a target set from measurement (TBD) | P1 | A |
| NFR-PERF-03 | Gateway overhead per call shall be small compared with model latency and measured | P1 | A |
| NFR-PERF-04 | The load generator's actual send time shall stay within a stated tolerance of schedule | P1 | T |
| NFR-PERF-05 | The stand-in's p50 and p95 shall be within a stated percentage of the real service for the same request mix | P0 | A |
| NFR-PERF-06 | Under sustained overload the queue shall remain bounded and retries shall not grow without limit | P0 | T |

### 3.4 Quality attributes

| Attribute | Requirement |
| --- | --- |
| Correctness | Wrong or uncited answers are the worst outcome and shall be minimised by refusing instead of guessing (P0) |
| Reliability | The system shall degrade through the ladder rather than crash when limits are reached or calls fail (P0) |
| Observability | Any result shall be explainable from its trace (P0) |
| Reproducibility | Runs shall record seeds, config, machine and commit so results can be reproduced (P1) |
| Maintainability | Thresholds shall live in configuration, not in code (P1) |
| Security | No secrets in code, traces, repository history or submission (P0) |
| Portability | The system shall run from a clean clone on a laptop and, optionally, in a notebook (P2) |

### 3.5 Legal and policy requirements

| ID | Requirement | Pri | Ver |
| --- | --- | --- | --- |
| LP-01 | Use only your own free account, with no extra accounts or tricks around limits | P0 | I |
| LP-02 | Stay within the provider's terms | P0 | I |
| LP-03 | Include the real conversation history with the assistant, not a cleaned-up version | P0 | I |

## 4. Verification and traceability

| Requirement group | Primary verification | Phase | Evidence |
| --- | --- | --- | --- |
| FR-ING, FR-RET | Spot checks of citations on sample queries | 1 | Query log, retrieval mistake examples |
| FR-PIP | Scripted questions, seeded bad draft | 2 | Traces |
| Probe, Table 1 | Real probe runs | 3 | Probe data, headers |
| FR-GW, FR-RTY, FR-DEG | Unit tests with fake clock, then load runs | 4 | Test report, ladder logs |
| FR-SIM | Percentile and response-shape comparison with the real service | 5 | Comparison table |
| FR-LD | Dry runs of each scenario | 6 | Raw file |
| FR-EVAL | Graded question set across settings, paired retry run | 7 | Table 3, retry comparison |
| FR-LOG | Independent recomputation of sampled numbers | 8 | Tables and the raw file |
| LP, security | Secret scan, clean clone, checklist | 9 | Submission checklist |

## 5. Open items (TBD list)

- Provider limits and their confidence, from the probe.
- Gateway margins, in-flight cap, retry budget share and ladder thresholds.
- Patience limit that defines given-up requests.
- Latency targets for NFR-PERF-01 and NFR-PERF-02.
- Stand-in fidelity tolerance for NFR-PERF-05.
- Treatment of given-up requests in the status column and in latency percentiles.
