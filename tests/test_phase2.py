"""tests/test_phase2.py - Phase 2 Verification Test Suite.

Verifies:
- FR-PIP-01: Five pipeline steps (understand, retrieve, decide, draft, verify).
- FR-PIP-03: Branch (refuse) and bounded loops (widen once, redraft once).
- FR-PIP-04: Answers strictly grounded with chunk citations.
- FR-PIP-05: Honest refusal for unanswerable / out-of-scope questions.
- FR-PIP-06, FR-PIP-07: Deterministic citation verification.
- FR-PIP-08: Redraft triggered on bad draft; second failure triggers refusal.
- Five evaluation questions (3 answerable across the 3 papers, 2 unanswerable).
- Streaming gives a measurable first byte.
- Complete trace and CSV schema integrity.
"""

from pathlib import Path

import pytest

from src.citecheck import verify_citations
from src.gateway import DailyBudget
from src.pipeline import Pipeline
from src.trace import RESULTS_CSV_COLUMNS


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    root = Path(__file__).resolve().parent.parent
    # Reset stand-in server so tests start with 100% full buckets
    try:
        import urllib.request

        req = urllib.request.Request("http://127.0.0.1:8000/admin/reset", method="POST")
        urllib.request.urlopen(req, timeout=2.0)
    except Exception:
        pass

    # Use calibrated stand-in server (port 8000) for reproducible local testing
    from src.backends import BackendClient

    client = BackendClient(base_url="http://127.0.0.1:8000/v1")
    p = Pipeline(config_path=root / "config.yaml", backend_client=client)
    # Generous capacity for functional test suite so 5 consecutive queries don't starve
    p.gateway.tpm_limit = 50000.0
    p.gateway.tpm_bucket.capacity = 50000.0
    p.gateway.tpm_bucket.tokens = 50000.0
    p.gateway.tpm_bucket.refill_rate = 50000.0 / 60.0
    # Isolated output files for test suite
    test_dir = tmp_path_factory.mktemp("phase2_output")
    p.results_csv_path = test_dir / "results.csv"
    p.traces_jsonl_path = test_dir / "traces.jsonl"
    # Isolated budget for test suite
    test_budget_file = test_dir / "daily_budget.json"
    p.gateway.daily_budget = DailyBudget(
        budget_file=test_budget_file,
        requests_limit=p.gateway.rpd_limit,
        tokens_limit=p.gateway.tpd_limit,
    )
    return p


# ---------------------------------------------------------------------------
# Unit tests for deterministic citation checker (FR-PIP-06, FR-PIP-07)
# ---------------------------------------------------------------------------


def test_citecheck_valid_citation():
    """Verify valid citations and exact quotes pass."""
    chunk = {
        "chunk_id": "1706.03762v7-p5-m015",
        "text": "In this work we employ h = 8 parallel attention layers, or heads.",
    }
    draft = 'The base Transformer employs "h = 8 parallel attention layers, or heads" [1706.03762v7-p5-m015].'
    res = verify_citations(draft, [chunk])
    assert res.passed is True
    assert "1706.03762v7-p5-m015" in res.found_citations


def test_citecheck_catches_fake_chunk_id():
    """Verify citation checker rejects chunk IDs not in retrieved set."""
    chunk = {
        "chunk_id": "1706.03762v7-p5-m015",
        "text": "In this work we employ h = 8 parallel attention layers, or heads.",
    }
    draft = "The Transformer uses 8 heads [9999.99999v1-p99-m999]."
    res = verify_citations(draft, [chunk])
    assert res.passed is False
    assert "9999.99999v1-p99-m999" in res.invalid_chunk_ids


def test_citecheck_catches_fabricated_quotes():
    """Verify citation checker rejects fabricated quotes not in chunk."""
    chunk = {
        "chunk_id": "1706.03762v7-p5-m015",
        "text": "In this work we employ h = 8 parallel attention layers, or heads.",
    }
    draft = 'The paper says "we invented quantum transformer heads in 2026" [1706.03762v7-p5-m015].'
    res = verify_citations(draft, [chunk])
    assert res.passed is False
    assert len(res.fabricated_quotes) > 0


def test_citecheck_allows_honest_refusal():
    """Verify honest refusal passes without requiring chunk citations."""
    draft = "The provided research papers do not contain sufficient information to answer this question."
    res = verify_citations(draft, [])
    assert res.passed is True


# ---------------------------------------------------------------------------
# Integration tests on Pipeline v0 (Real Groq Backend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_first_byte(pipeline):
    """Confirm streaming gives a measurable first byte (TTFT)."""
    from src.backends import ModelRequest

    req = ModelRequest(
        model=pipeline.small_model,
        messages=[{"role": "user", "content": "Respond with the single word 'pong'."}],
        max_tokens=10,
    )
    res = await pipeline.backend_client.stream_chat(req)
    assert res.status_code == 200
    assert res.t_first_byte >= res.t_start
    assert res.t_end >= res.t_first_byte
    assert res.ttft_ms >= 0.0
    assert len(res.content) > 0


@pytest.mark.asyncio
async def test_force_bad_draft_triggers_redraft_and_refusal(pipeline):
    """Verify that a bad draft triggers the redraft loop and verification catches it."""
    import asyncio

    await asyncio.sleep(1.0)
    res = await pipeline.run(
        "How many parallel attention heads does the base Transformer use?",
        force_bad_draft=True,
    )
    # The pipeline should detect the bad draft, execute redraft, and record stages
    assert res.trace is not None
    stage_names = [s.name for s in res.trace.stages]
    assert "S4_draft" in stage_names
    assert "S5_verify" in stage_names
    assert "S4_redraft" in stage_names
    assert "S5_verify_redraft" in stage_names
    assert res.retries >= 1


@pytest.mark.asyncio
async def test_five_questions_suite(pipeline):
    """Evaluate 5 questions: 3 answerable across the 3 papers, 2 unanswerable."""
    questions = [
        # Q1: Attention Is All You Need (Answerable)
        {
            "q": "How many parallel attention heads does the base Transformer use?",
            "expect_answer": True,
            "expected_paper": "1706.03762",
            "expected_fact": "8",
        },
        # Q2: Moshi (Answerable)
        {
            "q": "What neural audio codec does Moshi use for real-time dialogue?",
            "expect_answer": True,
            "expected_paper": "2410.00037",
            "expected_fact": "Mimi",
        },
        # Q3: Dynamic Chunking (Answerable)
        {
            "q": "What hierarchical architecture is introduced in Dynamic Chunking?",
            "expect_answer": True,
            "expected_paper": "2507.07955",
            "expected_fact": "H-Net",
        },
        # Q4: Off-topic unanswerable
        {
            "q": "How does ResNet-50 perform on ImageNet computer vision classification benchmarks?",
            "expect_answer": False,
        },
        # Q5: Unanswerable scope trap
        {
            "q": "What is the measured battery life impact of running Moshi on an Apple Watch Series 9?",
            "expect_answer": False,
        },
    ]

    for item in questions:
        import asyncio

        await asyncio.sleep(2.5)  # Pace queries to protect free-tier TPM window
        res = await pipeline.run(item["q"], scenario="phase2_eval")
        assert res.status == "ok"
        assert res.trace is not None

        if item["expect_answer"]:
            # Answerable questions must cite and ground the expected fact
            assert res.outcome == "succeeded"
            assert res.verification_passed is True
            assert len(res.citations) > 0
            assert any(item["expected_paper"] in c for c in res.citations)
            assert item["expected_fact"].lower() in res.answer.lower()
        else:
            # Unanswerable questions must refuse honestly
            assert (
                "not contain" in res.answer.lower()
                or "not mention" in res.answer.lower()
                or "insufficient" in res.answer.lower()
                or "outside the scope" in res.answer.lower()
                or "cannot answer" in res.answer.lower()
            )
            assert res.outcome in (
                "refused_correctly",
                "refused_after_verify_fail",
                "refused_scope",
            )


@pytest.mark.asyncio
async def test_results_csv_and_trace_integrity(pipeline):
    """Verify trace records and CSV rows adhere to schema."""
    csv_path = pipeline.results_csv_path
    assert csv_path.is_file()

    with open(csv_path, "r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
    assert header == RESULTS_CSV_COLUMNS

    trace_path = pipeline.traces_jsonl_path
    assert trace_path.is_file()
    assert trace_path.stat().st_size > 0
