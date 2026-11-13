# The Relay: Resilient LLM Gateway & Grounded RAG System

A multi-step, citation-grounded research assistant over three pinned arXiv papers featuring an enterprise-grade, load-aware LLM gateway, stand-in model server, open-loop load generator, deterministic citation verification, and automated analysis harness.

---

## 📑 Project Overview

**The Relay** is built around two fundamental operational invariants:
1. **Never guess or invent**: Every generated statement is strictly grounded in retrieved passages from verified research papers, accompanied by deterministic citation checks (`[{paper_id}v{version}, p. {page}, §{section}, {chunk_id}]`). If the indexed literature lacks evidence, the system returns an honest, structured refusal.
2. **Never breach upstream boundaries**: All external LLM calls pass through a centralized Gateway (`src/gateway.py`) implementing dual token buckets (RPM/TPM), Earliest Deadline First (EDF) priority scheduling, concurrency semaphores, rolling retry budgets, and an adaptive degradation ladder (L0 through L4).

---

## 📂 Repository Structure

```text
vaani-ml-internship/
├── src/                          # Core application modules
│   ├── gateway.py                # Centralized LLM gateway, rate limiting, EDF queue, degradation ladder
│   ├── pipeline.py               # RAG pipeline orchestration (S1 Understand -> S2 Retrieve -> S3 Decide -> S4 Draft -> S5 Verify)
│   ├── retriever.py              # BM25 multi-paper retriever with dynamic chunk sizing and k selection
│   ├── citecheck.py              # Zero-token deterministic citation verification engine
│   ├── ingest.py                 # PDF ingestion, structural extraction, and chunk indexing
│   ├── backends.py               # LLM backend clients (Groq Cloud API & Stand-In Server)
│   ├── trace.py                  # Structured request tracing and audit logging
│   └── prompts.py                # System & task prompts for synthesis, evaluation, and planning
├── standin/                      # Calibrated stand-in server matching Groq Cloud semantics
│   ├── server.py                 # FastAPI OpenAI-compatible streaming server with concurrency cliff & header sync
│   ├── calibrate.py              # Probe calibration against real Groq latency and throughput
│   └── calibration.json          # Calibrated timing parameters
├── experiments/                  # Evaluation benchmarks and load generation
│   ├── loadgen.py                # Open-loop load generator executing 5 realistic traffic scenarios
│   ├── run_quality.py            # Quality and citation faithfulness benchmark suite
│   ├── probe_groq.py             # Empirical probing of real Groq limits and header behavior
│   ├── audit_data.py             # Data integrity validation across results and traces
│   └── questions.yaml            # Curated evaluation questions (in-domain & out-of-domain)
├── analysis/                     # Metric processing and report generation
│   ├── make_tables.py            # Generates Tables 1, 2a, 2b, 3 from results.csv and traces.jsonl
│   └── explain_request.py        # Trace inspection and request timeline analyzer
├── data/                         # Datasets and paper corpora
│   ├── papers/                   # Pinned research paper PDFs with SHA-256 validation
│   ├── chunks_medium.json        # Precomputed medium-granularity chunk corpus
│   └── chunks_small.json         # Precomputed small-granularity chunk corpus
├── docs/                         # Specifications, system design, and requirements
│   ├── The Relay Architecture.md # System architecture and pipeline design
│   ├── The Relay PRD.md          # Product Requirements Document
│   ├── The Relay TRD.md          # Technical Requirements Document
│   └── ...                       # SRS, SDLC, and evaluation criteria documents
├── notes/                        # Architectural records and audit logs
│   ├── decisions.md              # Architectural Decision Records (ADRs D01–D20)
│   └── audit_report.md           # Dataset and latency audit report
├── results/                      # Experimental results and benchmarks
│   ├── results.csv               # Standardized result rows across all benchmark runs
│   ├── traces.jsonl              # Full JSONL execution traces with step-level timings
│   ├── tables.md                 # Markdown tables generated from experimental data
│   └── real/                     # Empirical probe data from Groq API
├── scripts/                      # Operational and test scripts
│   ├── verify_papers.py          # Verifies SHA-256 hashes of pinned arXiv papers
│   └── deep_analysis_test.py     # End-to-end integration and analysis checks
├── tests/                        # Comprehensive test suite (Phase 0 through Phase 6)
│   ├── test_phase0.py            # Verification and schema integrity tests
│   ├── test_phase1.py            # Ingest and retrieval unit tests
│   ├── test_phase2.py            # Pipeline and citation checks
│   ├── test_phase3.py            # Stand-in server emulation tests
│   ├── test_phase4.py            # Gateway rate limiting and queueing tests
│   ├── test_phase6.py            # Load generator scenario validation
│   ├── test_gateway.py           # Concurrency and degradation tests
│   └── test_timing.py            # Latency and deadline enforcement tests
├── .env.example                  # Environment configuration template
├── .gitignore                    # Git ignore file for secrets, caches, and environments
├── config.yaml                   # Central system configuration
├── pyproject.toml                # Project packaging configuration
├── requirements.txt              # Production and test dependencies
├── AGENTS.md                     # Agentic and automation instructions
└── submission.md                 # Complete Engineering Report & Evaluation
```

---

## 🏛️ System Architecture

```text
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

## 📚 Library of Pinned Papers

The system operates strictly over three pinned arXiv papers validated against SHA-256 checksums:
1. **Attention Is All You Need**: `arXiv:1706.03762v7`
2. **Moshi: a speech-text foundation model for real-time dialogue**: `arXiv:2410.00037v1`
3. **Dynamic Chunking for End-to-End Hierarchical Sequence Modeling**: `arXiv:2507.07955v2`

---

## 🚀 Quick Start

### 1. Prerequisites & Environment Setup
```bash
# Clone the repository
git clone https://github.com/ankitpaul6201/vaani-ml-internship.git
cd vaani-ml-internship

# Create and activate virtual environment
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and supply your GROQ_API_KEY (optional if running in standalone/stand-in mode)
```

### 3. Verify Pinned Papers
```bash
python scripts/verify_papers.py
```

### 4. Running the Stand-In Server (Mock Mode)
```bash
# Starts the calibrated FastAPI stand-in model server on port 8000
python -m uvicorn standin.server:app --port 8000
```

### 5. Running Load Generation Scenarios
```bash
# Run steady scenario against the stand-in server
python experiments/loadgen.py --scenario steady --backend standin

# Run all 5 scenarios (steady, burst, sustained_overload, limit_halved, daily_exhausted)
python experiments/loadgen.py --scenario burst --backend standin
```

### 6. Generate Analysis Tables
```bash
python analysis/make_tables.py
```

---

## 🧪 Testing

The repository includes a comprehensive test suite validating every phase of the architecture:

```bash
# Run all unit and integration tests
pytest -q

# Run specific phase tests
pytest tests/test_phase0.py  # Repository, paper verification & schemas
pytest tests/test_phase1.py  # Ingestion & retrieval
pytest tests/test_phase2.py  # Pipeline & citation engine
pytest tests/test_phase3.py  # Stand-in server fidelity
pytest tests/test_phase4.py  # Gateway admission & rate limits
pytest tests/test_phase6.py  # Load generator scenarios
```

---

## 📊 Summary of Experimental Results

| Scenario | Total Requests | Succeeded | Rate-Limited | Timeout / Shed | Avg Latency (p50) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `steady` | 21 | 57.1% (12) | 42.9% (9) | 0.0% (0) | 1,550.8 ms |
| `burst` | 25 | 68.0% (17) | 32.0% (8) | 0.0% (0) | 3,707.4 ms |
| `sustained_overload` | 30 | 56.7% (17) | 43.3% (13) | 0.0% (0) | 6,994.4 ms |
| `limit_halved` | 21 | 42.9% (9) | 57.1% (12) | 0.0% (0) | 4,611.8 ms |
| `daily_exhausted` | 16 | 62.5% (10) | 37.5% (6) | 0.0% (0) | 5.5 ms (L4 Extractive) |

Detailed benchmark metrics, latency curves, degradation ladder transitions, and real vs. stand-in profiling are documented in [`submission.md`](submission.md) and [`notes/decisions.md`](notes/decisions.md).

---

## 📄 License & Attribution

Designed and engineered for the **Vaani ML Internship Assignment** (November 2026).  
All research papers belong to their respective authors and are accessed according to arXiv open-access terms.
