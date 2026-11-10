# The Relay: Software Development Life Cycle (SDLC) Plan

**Version:** 1.0 draft **Related documents:** PRD, TRD, SRS, Output To Be Achieved, build phases guide

This plan says how the work is organised from first requirement to submission and live defence: the life-cycle model, the stages and gates, roles, the rules for working with an AI coding assistant, source control, testing, change control and risk handling.

## 1. Purpose and scope

The project is a one-person build with an AI coding assistant, about 6 to 8 hours of real work spread over up to four days, followed by a live conversation where the system is explained and changed. The plan keeps the work small, measurable and defensible.

## 2. Chosen life-cycle model

**Iterative and incremental, with a measurement gate at the end of every phase.**

| Model | Why not (or why) |
| --- | --- |
| Waterfall | Rejected. Key numbers (limits, latency, quality) are unknown until you measure, so a fully upfront design would be wrong |
| Scrum-style sprints | Too heavy for one person and four days |
| Iterative with gates (chosen) | Build a thin version, measure, decide the next step from the numbers |

The loop for every phase is: **define the exit test, build the smallest thing that can pass it, measure, record the decision, move on.** The brief rewards choices that follow from measurements, so the plan makes that the rule.

## 3. Stages, activities and gates

| Stage | Phase | Activities | Artifacts | Gate to pass |
| --- | --- | --- | --- | --- |
| Requirements | 0 | Read the brief, fix the results schema, set up repo, key and papers | PRD, SRS, schema, `decisions.md` | Key works, papers verified at the pinned versions, no secret in git |
| Design | 0 to 1 | Architecture, TRD, retrieval design | Design doc, TRD | Components and interfaces agreed |
| Implementation | 1 to 2 | Ingestion, retriever, pipeline v0 on real Groq | Code, sample trace | Answers cite correctly, refusals work, verifier catches a seeded bad draft |
| Discovery | 3 | Probe the real service | Probe data, draft Table 1 | Every limit has a source and a confidence |
| Implementation | 4 to 5 | Gateway, stand-in server, calibration | Code, comparison | Unit tests pass, stand-in percentiles close to real |
| Verification | 6 to 7 | Load scenarios, quality and retrieval runs | Raw files | Valid CSV rows, each scenario runs end to end |
| Analysis | 8 | Generate tables | Tables, surprising number | Tables recompute from the file |
| Release | 9 | Write-up, history, rehearsal, packaging | Submission | Definition of done ticked |
| Maintenance | After | Live conversation, changes on request | Updated config and runs | Can explain and change the system |

Do not start a stage before its gate passes. If a gate fails, fix or reduce scope. Record which.

## 4. Roles and responsibilities

| Role | Held by | Responsibilities |
| --- | --- | --- |
| Product owner | You | Decide what matters, set targets, choose trade-offs |
| Engineer | You, with the assistant | Build and own every line you submit |
| Tester and reviewer | You | Check everything the assistant produces, run the tests, recompute numbers |
| AI assistant | Tool | Draft code, suggest options, explain. It decides nothing |
| Reviewers | The assessors | Read tables, recompute, question you live |

The rule that follows from the brief: you are accountable for anything submitted, even if the assistant wrote it.

## 5. Working with the AI assistant

The brief evaluates how you direct the assistant: how you frame problems, how you check output, and where you overrule it. Treat that as a process, not an afterthought.

### Protocol

1. **Frame.** State the goal, constraints, and the exact acceptance test before asking for code.
2. **Generate.** Ask for small pieces, one component at a time.
3. **Verify.** Read the code. Run it. Run the tests. Check claims against the real source (the Groq documentation, the response headers, the paper PDFs).
4. **Decide.** Accept, change, or reject. Write the decision and the reason in `decisions.md`.
5. **Log.** Save the conversation uncleaned. Note each mistake the assistant made and how you found it.

### Things to verify every time

- Any provider limit, header name, price or model name the assistant states. These change, so confirm in the documentation or in real responses.
- Any paper fact or page citation. Open the PDF.
- Any statistics or percentile code. Recompute a sample by hand.
- Any claim that the code works. Run it.
- Any piece of code you cannot explain. Rewrite it until you can.

### What to record for the reflection section

- The three prompts that changed the outcome most, and why.
- One mistake the assistant made, and the check that caught it.
- One decision where you overruled it, and your reason.
- What in the submission is distinctly yours: your measurements, your trade-offs, your choices.

## 6. Source control

| Rule | Detail |
| --- | --- |
| Repository | Private Git repo from day one |
| Branches | `main` always runs. Short feature branches such as `feat/gateway`, `feat/standin` |
| Commits | Small and frequent, message states what and why, for example `gateway: honour retry-after with jitter` |
| Tags | One per phase gate, such as `phase-3-probe` and `final-runs` |
| Secrets | `.env` in `.gitignore`, a secret scan before each commit, no key in history |
| Data | Raw results are committed or archived with the commit hash they came from |
| Code freeze | Before the final runs, freeze the code. Any change afterwards means rerunning the affected scenarios |

Every trace record stores the commit hash, so any result can be tied to the exact code that produced it.

## 7. Environment and dependencies

- Python virtual environment, pinned `requirements.txt`.
- Fixed random seeds for load generation and the stand-in, recorded in each run's metadata.
- A `config.yaml` that holds every threshold. No magic numbers in code.
- Record machine, network and time of day for each real run, since they affect latency.
- A clean-clone test: the repo runs from scratch with only the key supplied.

## 8. Testing and quality gates

| Level | Target | Gate |
| --- | --- | --- |
| Unit | Buckets, queue order, retry rules, ladder transitions, citation checker | All pass with a fake clock |
| Integration | Pipeline against the stand-in | Scripted questions pass |
| Contract | Stand-in response matches captured real responses | Same status codes, headers and shape |
| Load | Five scenarios | Each runs end to end and writes valid rows |
| Evaluation | 15 or more questions across three settings | Graded with a written rubric |
| Reproducibility | Tables from the file | Independent recomputation of sampled values |

Focus test effort on the gateway and the citation checker, because they carry the most risk.

## 9. Definition of ready and done

**A task is ready when** it has an exit test written down and the inputs it needs.

**A task is done when:**

- [ ] It meets its exit test.
- [ ] It is committed with a clear message.
- [ ] Its decision and its measured reason are in `decisions.md`.
- [ ] You can explain how it works without the assistant.
- [ ] Any number it produced is traceable to the raw file.

## 10. Quota and risk management

The daily provider budget is shared by everything you run. Plan it before you spend it.

| Risk | Likelihood | Impact | Response |
| --- | --- | --- | --- |
| Daily quota burned early | Medium | High | Budget tokens per phase, keep a reserve, put heavy load on the stand-in |
| Real limits differ from documentation | Medium | High | Read headers at runtime, mark confidence |
| Stand-in unlike the real service | Medium | High | Compare percentiles, state differences |
| Run on a bad network day | Low | Medium | Note conditions, rerun if needed |
| Tables do not match the raw file | Medium | High | Generate tables only from the file |
| Scope creep in the pipeline | High | Medium | Stop polishing, measure instead |
| Cannot defend a choice live | Medium | High | Decision log with the measurement behind every choice |

Stay within the provider's terms. Do not use extra accounts or any trick to get around limits.

## 11. Change control

- Changes to thresholds go through `config.yaml` and are logged with the reason.
- After the code freeze, only fixes that block the submission are allowed, and each one triggers a rerun of the affected scenarios.
- Keep a list of every change made after any real or stand-in run, so you know which results are still valid.
- If a measurement contradicts an earlier assumption, update the assumption and note it in the write-up under what you are unsure about.

## 12. Documentation and traceability

| Document | Purpose |
| --- | --- |
| PRD | What the product must do and why |
| SRS | Numbered requirements that can be tested |
| TRD | How it is built |
| Architecture and design | Components and the main decision |
| Output To Be Achieved | Deliverables and result tables |
| `decisions.md` | Running log of choices, the measurement behind each, and the cost |
| Assistant history | Evidence of how you worked |

Every requirement in the SRS maps to a test or a table, and every table number maps to the raw file.

## 13. Submission process

1. Code freeze and tag `final-runs`.
2. Run the final scenarios and save the raw files with commit hash.
3. Run the analysis script and fill the tables.
4. Write the two-page document and check the heading order and length.
5. Visit vaani.dev and write your feedback from what you actually saw.
6. Add the assistant history and the half-page reflection.
7. Secret scan, clean-clone test, check the page count.
8. Package and submit within four days of receiving the brief.

## 14. After submission: live conversation readiness

Expect to explain results and change the system. Prepare:

- A script that prints the timeline of any request id.
- Config switches for the limits, patience, ladder thresholds, retrieval setting and the verifier.
- Three rehearsed changes: halve a limit, switch the retrieval setting, turn the verifier off. For each, say what you expect before you run it.
- The answer to "what is your least certain assumption and what breaks if it is wrong?"

## 15. Process metrics

Track lightly, to see where the time went and where to cut:

| Metric | Why |
| --- | --- |
| Hours spent per phase | Compare with the plan, cut scope early if behind |
| Real tokens and requests used per day | Avoid running out unplanned |
| Number of assistant mistakes caught | Evidence for the reflection section |
| Tests passing at each gate | Confidence before moving on |
