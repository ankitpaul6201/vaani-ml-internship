"""tests/test_timing.py - Unit tests for timing fidelity and trace consistency.

Verifies:
- first_token_ms is when the first token is RELEASED to the person.
- model_first_byte_ms is recorded in traces.
- retry_ms and queue_wait_ms come from GatewayResult.
- seed is present in trace records.
- queue_wait_ms + work_ms + retry_ms == total_ms within tolerance (<= 1.0 ms).
- first_token_ms < total_ms for generated answers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.backends import BackendClient, ModelRequest, ModelResult
from src.gateway import Gateway, GatewayCall
from src.pipeline import Pipeline
from src.trace import TraceRecord, read_traces_jsonl


@pytest.mark.asyncio
async def test_gateway_tracks_retry_ms_and_queue_wait_ms(tmp_path: Path):
    """Verify GatewayResult contains accurate queue_wait_ms and retry_ms."""
    mock_backend = AsyncMock(spec=BackendClient)

    attempts = 0

    async def mock_stream(req: ModelRequest, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ModelResult(
                content="",
                status_code=429,
                headers={"retry-after": "0.1"},
                error="Rate limited",
            )
        return ModelResult(content="Success", status_code=200, tokens_in=50, tokens_out=10)

    mock_backend.stream_chat.side_effect = mock_stream

    gw = Gateway(backend_client=mock_backend)
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"

    call = GatewayCall(
        request_id="req-timing-1",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="test", messages=[]),
        deadline=asyncio.get_event_loop().time() + 10.0,
        prompt_tokens_est=100,
    )

    res = await gw.call(call)
    assert res.status == "ok"
    assert res.retries == 1
    assert res.retry_ms >= 50.0  # Spent at least ~100ms sleeping for retry
    assert res.queue_wait_ms >= 0.0


@pytest.mark.asyncio
async def test_pipeline_timing_invariants_and_trace_fields(tmp_path: Path):
    """Verify that:
    1. first_token_ms < total_ms for normal answers.
    2. trace contains seed and model_first_byte_ms.
    3. queue_wait_ms + work_ms + retry_ms == total_ms within 1.0ms.
    4. When S5 verifies, first_token_ms >= model_first_byte_ms (since tokens are held until verified).
    """
    mock_backend = AsyncMock(spec=BackendClient)

    # Return valid draft with citation
    mock_backend.stream_chat.return_value = ModelResult(
        content="The base Transformer uses 8 parallel attention heads [vaswani2017-attention, p. 5].",
        status_code=200,
        tokens_in=300,
        tokens_out=25,
    )

    pipe = Pipeline(backend_client=mock_backend)
    pipe.gateway.daily_budget.tokens_used = 0
    pipe.gateway.daily_budget.requests_used = 0
    pipe.results_csv_path = tmp_path / "results.csv"
    pipe.traces_jsonl_path = tmp_path / "traces.jsonl"

    resp = await pipe.run(
        question="How many attention heads are in the base Transformer?",
        scenario="steady",
        request_id="test-timing-req",
    )

    assert resp.status == "ok"
    assert resp.trace is not None

    # 1. First token released < total_ms
    assert resp.first_token_ms < resp.total_ms
    assert resp.first_token_ms > 0

    # 2. Trace fields
    trace = resp.trace
    assert hasattr(trace, "seed")
    assert hasattr(trace, "model_first_byte_ms")
    assert trace.model_first_byte_ms > 0
    assert trace.model_first_byte_ms <= resp.first_token_ms

    # 3. Additive timing invariant: queue + work + retry == total_ms (tolerance 1.0 ms)
    sum_ms = trace.queue_wait_ms + trace.work_ms + trace.retry_ms
    assert abs(sum_ms - resp.total_ms) <= 1.0, f"Timing mismatch: {sum_ms} vs {resp.total_ms}"
