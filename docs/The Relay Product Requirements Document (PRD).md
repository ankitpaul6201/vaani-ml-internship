# The Relay: Product Requirements Document (PRD)

**Version:** 1.0 draft **Product:** The Relay, a cited-answer research assistant with a load-aware model gateway **Related documents:** TRD, SRS, SDLC plan, Output To Be Achieved, plus the architecture and how-it-works docs

Priority tiers used throughout: **P0** must have, **P1** should have, **P2** nice to have. Any number marked **TBD** must come from your own measurements, not from guesses.

## 1. Overview

The Relay is an assistant that answers engineers' questions about a small library of three research papers. It works out what is being asked, retrieves passages, decides what to do, drafts a cited reply, and verifies the reply before releasing it.

The product has a second, equally important job: to stay trustworthy when demand spikes. Requests arrive in bursts, the model provider enforces limits, calls fail or crawl, and people give up quickly. The product must decide who waits, who is told to retry, and who receives a reduced answer, while never sending a wrong or uncited answer to stay fast.

## 2. Problem statement

Engineers ask questions in bursts, usually when something has gone wrong for many people at once. A naive chain of model calls fails exactly then: it hits per-minute limits, retries in lockstep, hangs on slow calls, and either times out or guesses. The company wants every reply fast, good, cheap and never refused, but those goals conflict. The product must make the conflict explicit and manage it with measured, defensible choices.

## 3. Goals and non-goals

### Goals

| Tier | Goal |
| --- | --- |
| P0 | Every answer is grounded in the three papers and cites where it came from |
| P0 | When the papers do not say, the assistant says so instead of guessing |
| P0 | The system respects the provider's limits and degrades gracefully instead of failing |
| P0 | Every decision about limits, retries and degradation is backed by measurement |
| P1 | Time to first token stays low for most people even during bursts |
| P1 | Cost per answer is known and kept small |
| P2 | The people operating it can see at a glance what the system is doing |

### Non-goals

- Answering questions about anything outside the three papers.
- Multi-turn conversation memory.
- User accounts, billing, or a polished web interface.
- Beating the provider's limits by any means other than working inside them (extra accounts and similar tricks are forbidden).
- Perfect answers at any cost. A correct refusal is better than a confident wrong answer.

## 4. Users and scenarios

| User | Situation | What they need |
| --- | --- | --- |
| Engineer under pressure | An incident just happened and many colleagues are asking at once | A fast, correct, cited answer, or an honest instruction to wait or retry |
| Engineer reading closely | Calm afternoon, wants depth | A thorough answer with exact citations to check |
| Service operator | Watches the system during a burst | Visibility into queue length, limits, and how many people get reduced answers |
| Reviewer | Evaluates the submission | Reproducible numbers, honest trade-offs, a system that can be changed live |

## 5. Product principles, in priority order

1. **Correctness before speed.** A wrong or uncited answer is the worst outcome.
2. **Honesty about reduction.** If an answer is shorter, an extract, or a refusal, the person is told.
3. **Fast first token for the people we do serve.** Perceived speed matters most while waiting.
4. **Serve as many people as possible.** Turn people away only when no deadline can be met.
5. **Keep the bill small.** Cost is last because the earlier principles are non-negotiable.

These rank the four conflicting goals in the brief. State this ranking in the write-up and show what it cost.

## 6. Requirements by priority

### P0: must have

| ID | Requirement | User value | Acceptance |
| --- | --- | --- | --- |
| PR-01 | Answer questions from the three specified papers at the specified versions only | Trustworthy source | Every answer chunk comes from v7, v1 or v2 as pinned |
| PR-02 | Cite paper, version, section and page for each claim | Checkable answers | Citations resolve to real passages in a spot check |
| PR-03 | Refuse honestly when the papers are silent | No guessing | Unanswerable test questions are refused, not answered |
| PR-04 | Verify every draft before release | No bad answers shown | Seeded bad drafts are caught and never reach the person |
| PR-05 | At least four pipeline steps, one tool call, one branch or loop | Required structure | Visible in the trace of each request |
| PR-06 | One gateway owns all model calls and enforces the provider's limits | No limit surprises | No uncontrolled bursts of calls to the provider |
| PR-07 | Requests carry a patience deadline, and work for abandoned requests is cancelled | No wasted effort | Given-up requests stop consuming calls |
| PR-08 | Retry only with a budget, jitter and respect for retry-after | No retry storms | Retry-per-request numbers stay bounded under overload |
| PR-09 | Degradation ladder ending in a cited extract or an honest refusal | Reduced but safe answers | Ladder transitions logged, no uncited output at any level |
| PR-10 | Stand-in model service that can be slow, fail, refuse on limits, slow under load and stream | Safe load testing | All five behaviours demonstrated |
| PR-11 | Stream replies and measure time to first token and total time | Perceived speed | Both recorded per request |
| PR-12 | Record every request in a raw results file from which all report numbers are computed | Credible results | Tables recompute from the file |

### P1: should have

| ID | Requirement | Acceptance |
| --- | --- | --- |
| PR-21 | Three retrieval settings that can be switched by config | Compared in Table 3 |
| PR-22 | Circuit breaker for a failing or crawling provider | Opens and recovers in a test |
| PR-23 | Adaptive concurrency that lowers in-flight calls when latency rises | Observed in a run |
| PR-24 | Early status message with an honest wait estimate | Not counted as the first token |
| PR-25 | Script that explains any request id as a timeline | Used in the latency breakdown |
| PR-26 | All thresholds in one config file that can change without code edits | Live changes work |

### P2: nice to have

| ID | Requirement |
| --- | --- |
| PR-31 | Sentence-level gated streaming that releases each sentence only after it passes citation checks |
| PR-32 | Small HTML dashboard of the run results generated from the results file |
| PR-33 | Command line chat interface for demonstration |

## 7. What the person sees

| Situation | Reply type | Must include |
| --- | --- | --- |
| Normal | Cited answer | Inline citations to paper, section and page |
| Papers do not cover it | Honest refusal | A plain statement that the papers do not say, and the nearest related passage if useful |
| Heavy load | Shorter cited answer or an extract | A note that the reply is reduced |
| Wait is long but recoverable | Status message, then the answer | An honest estimate, not a promise |
| No deadline can be met | Fast busy reply | A suggested time to try again |
| Daily budget exhausted | Extract-only reply | A clear statement that generated answers are unavailable until the budget resets, the top cited passages, and when to return |

Never show an answer that failed verification. Never present an extract as a full answer.

## 8. Success metrics

All targets are placeholders until you have measured the real service. Set them from your measurements and say why.

| Metric | Definition | Target |
| --- | --- | --- |
| Correct and cited | Share of quality questions answered correctly with valid citations | TBD |
| Wrong answers | Share graded wrong | As low as possible, reported separately |
| Correctly refused | Share of unanswerable questions refused | TBD |
| Wrongly refused | Answerable questions refused | TBD, reported honestly |
| Time to first token | p50, p95 and p99 per load shape | TBD |
| Total time | p50, p95 and p99 per load shape | TBD |
| Given-up rate | Requests abandoned past the patience limit | TBD per scenario |
| Rate-limited rate | Requests that hit a provider limit | TBD per scenario |
| Retries per request | Mean number of retries | Bounded by the retry budget |
| Tokens per answer | Prompt plus output tokens | TBD per retrieval setting |
| Cost per 1,000 answers | Using provider pricing, and stating the free-tier assumption | TBD |

## 9. Constraints and assumptions

- Language is Python, C++ or Go. Python is assumed.
- Real model calls use a free Groq account with your own key. Limits are real and shared by everything you run in a day.
- The papers are fetched at exact versions: arXiv 1706.03762 v7, 2410.00037 v1, 2507.07955 v2.
- Stand-in results are labelled as stand-in everywhere.
- The key never appears in the submission.
- Assumption: provider limits can change without notice, so the gateway reads them at runtime where possible.

## 10. Dependencies

| Dependency | Risk if it changes |
| --- | --- |
| Groq API and its limits | Numbers in Table 1 become stale, so record the date of measurement |
| arXiv downloads | Wrong version used, so verify the version page and keep file hashes |
| Local retrieval libraries | Different results across versions, so pin versions |

## 11. Risks

| Tier | Risk | Mitigation |
| --- | --- | --- |
| P0 | Daily quota exhausted early | Plan tokens per phase, put heavy load on the stand-in |
| P0 | Stand-in does not resemble the real service | Compare percentiles, document differences |
| P0 | Tables do not match the raw file | Generate tables from the file only |
| P1 | PDF extraction garbles tables and equations | Tag content kinds, inspect samples |
| P1 | Generator and stand-in compete for CPU | Monitor CPU, keep rates modest, report the limitation |
| P2 | Unable to explain choices in the live conversation | Keep a decision log with the measurement behind each choice |

## 12. Release plan

The build follows phases 0 to 9 in the SDLC plan. Each phase ends with a gate. Final runs happen after a code freeze so the raw file and the write-up describe the same system.

| Milestone | Contents |
| --- | --- |
| M1 | Papers ingested, retrieval working, pipeline answers and refuses (phases 0 to 2) |
| M2 | Real service probed, gateway and calibrated stand-in working (phases 3 to 5) |
| M3 | Load scenarios, quality runs, tables generated (phases 6 to 8) |
| M4 | Write-up, assistant log, defence rehearsal, submission (phase 9) |

## 13. Open questions

- What does fast mean for this product: time to first token, total time, or both? Pick and justify.
- What patience limit should define a given-up request?
- Which limit binds first on your chosen models: requests or tokens per minute?
- Is an extract an acceptable reply when generation is unavailable, or should some questions be refused instead?
- Should draft streaming wait for verification (safe, slower) or release sentence by sentence (faster, more complex)?
