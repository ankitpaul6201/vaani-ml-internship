# The Relay

A multi-step, citation-grounded research assistant over three pinned arXiv papers with a load-aware LLM gateway, stand-in model server, open-loop load generator, and automated analysis harness.

## Library of Pinned Papers
1. **Attention Is All You Need**: arXiv:1706.03762v7
2. **Moshi: a speech-text foundation model for real-time dialogue**: arXiv:2410.00037v1
3. **Dynamic Chunking for End-to-End Hierarchical Sequence Modeling**: arXiv:2507.07955v2

## Quick Start
```bash
# Set up virtual environment
python -m venv .venv
source .venv/bin/activate  # Or on Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# (Fill in GROQ_API_KEY in .env)

# Verify pinned research papers
python scripts/verify_papers.py

# Run test suite
pytest -q

# Run code formatting and linting
ruff check . && ruff format .
```

## Architecture
- **Pipeline (`src/pipeline.py`)**: S1 Understand -> S2 Retrieve (tool) -> S3 Decide -> S4 Draft -> S5 Verify -> Release.
- **Gateway (`src/gateway.py`)**: Token buckets (RPM & TPM), reserve & settle, EDF priority queue, admission shedding, retry budget, degradation ladder L0..L4.
- **Stand-In Server (`standin/server.py`)**: Calibrated FastAPI mock matching Groq's OpenAI streaming endpoint.
- **Load Generator (`experiments/loadgen.py`)**: Open-loop test harness executing 5 realistic scenarios.
- **Analysis (`analysis/make_tables.py`)**: Reproduces Tables 1, 2a, 2b, 3 directly from `results.csv`.
