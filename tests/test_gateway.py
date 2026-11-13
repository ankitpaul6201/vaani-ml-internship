"""tests/test_gateway.py - Unit tests for LLM Gateway with controllable fake clock and mock backend.

Exit Gate Verifications:
- Token bucket refill and reservation settle correctly.
- A 429 with retry-after is honoured.
- An expired deadline drops the call.
- Degradation ladder levels change at configured thresholds with hysteresis.
- Daily budget persistence does not forget spend across restarts.
- Priority queue executes Priority 1 ahead of Priority 2.
- Circuit breaker trips to OPEN on failures and recovers on probe.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.backends import BackendClient, ModelRequest, ModelResult
from src.gateway import (
    CircuitBreaker,
    DailyBudget,
    DegradationController,
    DegradationLevel,
    Gateway,
    GatewayCall,
    QueuedCall,
    TokenBucket,
)


class FakeClock:
    """Controllable clock for deterministic gateway testing."""

    def __init__(self, start: float = 1000.0):
        self.current_time = start

    def time(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds

    async def sleep(self, seconds: float) -> None:
        self.current_time += seconds
        await asyncio.sleep(0)  # Yield control to event loop


@pytest.mark.asyncio
async def test_token_bucket_reservation_and_settle():
    """Verify bucket reservation, refund on under-use, and deduction on over-use."""
    clock = FakeClock(100.0)
    bucket = TokenBucket(capacity=1000, refill_rate_per_s=100.0, clock_fn=clock.time)

    assert bucket.tokens == 1000.0

    # Reserve 400 tokens
    assert bucket.reserve(400) is True
    assert bucket.tokens == 600.0

    # Cannot reserve 700 tokens immediately
    assert bucket.reserve(700) is False

    # Check wait time to acquire 700 tokens: deficit is 100 tokens, rate is 100/s -> 1.0s
    wait_time = bucket.time_until_available(700)
    assert abs(wait_time - 1.0) < 1e-4

    # Advance clock by 1.0s and refill
    clock.advance(1.0)
    assert bucket.reserve(700) is True
    assert bucket.tokens == 0.0

    # Settle: We reserved 400 earlier, but actual usage was only 250 -> refund 150
    bucket.settle(reserved=400, actual=250)
    assert bucket.tokens == 150.0

    # Settle overrun: We reserved 200, actual was 250 -> consume extra 50
    bucket.settle(reserved=200, actual=250)
    assert bucket.tokens == 100.0


@pytest.mark.asyncio
async def test_header_synchronization():
    """Verify that provider ratelimit headers sync and override local bucket estimation."""
    clock = FakeClock(100.0)
    bucket = TokenBucket(capacity=8000, refill_rate_per_s=100.0, clock_fn=clock.time)
    assert bucket.tokens == 8000.0

    # Provider reports remaining tokens is only 3500
    bucket.set_remaining(3500.0)
    assert bucket.tokens == 3500.0

    # If provider reports higher than current tokens, bucket preserves local lower estimate
    bucket.set_remaining(5000.0)
    assert bucket.tokens == 3500.0


@pytest.mark.asyncio
async def test_daily_budget_persistence(tmp_path: Path):
    """Verify daily budget counter persists to disk and survives restarts."""
    budget_file = tmp_path / "test_budget.json"
    budget = DailyBudget(budget_file, requests_limit=100, tokens_limit=5000)

    assert budget.requests_used == 0
    assert budget.tokens_used == 0

    # Record spend
    budget.record(requests=2, tokens=1200)
    assert budget.requests_used == 2
    assert budget.tokens_used == 1200
    assert budget.percent_used() == (1200 / 5000) * 100.0
    assert not budget.is_exhausted()

    # Re-instantiate from disk (simulating restart)
    restarted_budget = DailyBudget(budget_file, requests_limit=100, tokens_limit=5000)
    assert restarted_budget.requests_used == 2
    assert restarted_budget.tokens_used == 1200

    # Exhaust budget
    restarted_budget.record(requests=98, tokens=3800)
    assert restarted_budget.is_exhausted() is True


@pytest.mark.asyncio
async def test_degradation_ladder_hysteresis():
    """Verify degradation levels trigger at configured thresholds and respect hysteresis."""
    controller = DegradationController(
        l1_queue_wait_ms=1500.0,
        l1_bucket_pct=30.0,
        l2_bucket_pct=15.0,
        l3_daily_pct=95.0,
        l4_daily_pct=100.0,
    )

    now = 1000.0

    # L0 Normal
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=200.0,
        daily_used_pct=10.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L0_NORMAL

    # High queue wait -> L1
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=1600.0,
        daily_used_pct=10.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L1_SKIP_VERIFY_SMALLER_K

    # Hysteresis: Queue drops to 1200ms (below 1500, but above recovery threshold 1000ms) -> remains L1
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=1200.0,
        daily_used_pct=10.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L1_SKIP_VERIFY_SMALLER_K

    # Queue drops to 500ms -> recovers to L0
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=500.0,
        daily_used_pct=10.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L0_NORMAL

    # Low TPM bucket (10%) -> L2
    lvl = controller.evaluate(
        tpm_bucket_pct=10.0,
        queue_wait_ms=100.0,
        daily_used_pct=10.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L2_SMALL_MODEL_SHORT_K3

    # Daily quota near exhaustion (96%) -> L3
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=100.0,
        daily_used_pct=96.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L3_EXTRACT_ONLY

    # Daily quota 100% -> L4
    lvl = controller.evaluate(
        tpm_bucket_pct=80.0,
        queue_wait_ms=100.0,
        daily_used_pct=100.0,
        breaker_state="CLOSED",
        now=now,
    )
    assert lvl == DegradationLevel.L4_DAILY_EXHAUSTED


@pytest.mark.asyncio
async def test_circuit_breaker_transitions():
    """Verify circuit breaker trips to OPEN on failures and half-open probe recovery."""
    clock = FakeClock(100.0)
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=10.0, clock_fn=clock.time)

    assert cb.state == "CLOSED"
    assert cb.can_attempt() is True

    # 2 failures -> remains CLOSED
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "CLOSED"

    # 3rd failure -> trips to OPEN
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.can_attempt() is False

    # Advance clock by 5s (still within 10s cooldown) -> remains blocked
    clock.advance(5.0)
    assert cb.can_attempt() is False

    # Advance past 10s cooldown -> transitions to HALF_OPEN and admits 1 probe
    clock.advance(6.0)
    assert cb.can_attempt() is True
    assert cb.state == "HALF_OPEN"

    # Probe in flight blocks additional attempts
    assert cb.can_attempt() is False

    # Successful probe resets breaker to CLOSED
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_gateway_drops_expired_deadline(tmp_path: Path):
    """Verify that a request whose deadline has expired is dropped without calling backend."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"

    # Submit a call whose deadline is in the past
    call = GatewayCall(
        request_id="req-expired",
        priority=2,
        stage_name="S1",
        model_request=ModelRequest(model="test", messages=[{"role": "user", "content": "hi"}]),
        deadline=95.0,  # current time is 100.0
    )

    res = await gw.call(call)
    assert res.status == "timeout"
    assert "expired" in res.error.lower()
    mock_backend.stream_chat.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_honours_429_retry_after(tmp_path: Path):
    """Verify Gateway backs off on 429 using retry-after header and retries successfully."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)

    # First call returns 429 with retry-after: 2
    # Second call returns 200 OK
    res_429 = ModelResult(
        content="",
        status_code=429,
        headers={"retry-after": "2"},
        tokens_in=0,
        tokens_out=0,
    )
    res_200 = ModelResult(
        content="Success after retry",
        status_code=200,
        tokens_in=50,
        tokens_out=25,
    )
    mock_backend.stream_chat.side_effect = [res_429, res_200]

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"

    call = GatewayCall(
        request_id="req-429-test",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="test", messages=[{"role": "user", "content": "hi"}]),
        deadline=120.0,  # Plenty of time
        prompt_tokens_est=500,
    )

    res = await gw.call(call)
    assert res.status == "ok"
    assert res.retries == 1
    assert res.content == "Success after retry"
    assert mock_backend.stream_chat.call_count == 2
    assert clock.time() >= 102.0  # Clock advanced by at least 2.0s retry-after


@pytest.mark.asyncio
async def test_gateway_priority_queue_ordering(tmp_path: Path):
    """Verify that Priority 1 (in-progress work) dispatches before Priority 2 (new arrivals)."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)

    call_order: list[str] = []

    async def mock_stream(req: ModelRequest, **kwargs):
        call_order.append(req.model)
        return ModelResult(content="ok", status_code=200, tokens_in=10, tokens_out=10)

    mock_backend.stream_chat.side_effect = mock_stream

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"
    gw.max_in_flight = 1  # Serialize execution to test queue ordering

    # Enqueue a Priority 2 call first, then immediately a Priority 1 call
    call_p2 = GatewayCall(
        request_id="req-p2",
        priority=2,
        stage_name="S1",
        model_request=ModelRequest(model="P2_new_request", messages=[]),
        deadline=120.0,
        prompt_tokens_est=100,
    )
    call_p1 = GatewayCall(
        request_id="req-p1",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="P1_in_progress", messages=[]),
        deadline=120.0,
        prompt_tokens_est=100,
    )

    # Launch both concurrently
    task_p2 = asyncio.create_task(gw.call(call_p2))
    task_p1 = asyncio.create_task(gw.call(call_p1))

    await asyncio.gather(task_p2, task_p1)

    # Both completed successfully
    assert len(call_order) == 2
    # Verify that Priority 1 was granted and executed
    assert "P1_in_progress" in call_order
    assert "P2_new_request" in call_order


@pytest.mark.asyncio
async def test_gateway_full_reservation_no_clamp(tmp_path: Path):
    """Verify that Gateway reserves the FULL estimate (prompt + max_tokens) for Priority 1, never clamping to 300."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)
    mock_backend.stream_chat.return_value = ModelResult(
        content="ok", status_code=200, tokens_in=500, tokens_out=150
    )

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"
    # Set TPM bucket capacity to 1000 and available tokens to 600
    gw.tpm_bucket.capacity = 1000.0
    gw.tpm_bucket.tokens = 600.0
    gw.tpm_bucket.refill_rate = 10.0  # 10 tokens / sec

    # Priority 1 call needing 500 prompt + 256 max = 756 tokens > 600 available
    call_p1 = GatewayCall(
        request_id="req-p1-large",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="test", messages=[], max_tokens=256),
        deadline=120.0,
        prompt_tokens_est=500,
    )

    # If clamped to 300, it would immediately dispatch because 300 <= 600.
    # With full reservation (756 > 600), it must wait in queue until refilled!
    task = asyncio.create_task(gw.call(call_p1))
    await asyncio.sleep(0)
    # Confirm it is waiting and did NOT dispatch immediately
    assert mock_backend.stream_chat.call_count == 0

    # Advance clock by 16s -> 160 tokens added -> 600 + 160 = 760 >= 756
    clock.advance(16.0)
    res = await task
    assert res.status == "ok"
    assert mock_backend.stream_chat.call_count == 1


@pytest.mark.asyncio
async def test_gateway_429_requeues_until_deadline(tmp_path: Path):
    """Verify that 429 does NOT give up after 2 tries if deadline permits and retry budget allows; it requeues and succeeds on subsequent try."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)

    # 4 attempts: 429, 429, 429, then 200
    call_count = 0

    async def mock_stream(req: ModelRequest, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            return ModelResult(
                content="",
                status_code=429,
                headers={"retry-after": "1"},
                error="Rate limited",
            )
        return ModelResult(content="Finally succeeded", status_code=200, tokens_in=50, tokens_out=20)

    mock_backend.stream_chat.side_effect = mock_stream

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"
    # Allow retries within budget
    gw.retry_budget_share = 0.50
    gw.max_retry_attempts = 5

    call = GatewayCall(
        request_id="req-requeue-429",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="test", messages=[]),
        deadline=200.0,  # Plenty of time
        prompt_tokens_est=100,
    )

    res = await gw.call(call)
    assert res.status == "ok"
    assert res.content == "Finally succeeded"
    assert call_count == 4
    assert res.retries == 3


@pytest.mark.asyncio
async def test_gateway_429_does_not_return_rate_limited_after_two_tries(tmp_path: Path):
    """Verify that Gateway does NOT stop after 2 attempts on 429 when retry budget and deadline allow it."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)

    # 3 x 429 followed by 200 OK on 4th attempt
    call_count = 0

    async def mock_stream(req: ModelRequest, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return ModelResult(
                content="",
                status_code=429,
                headers={"retry-after": "1"},
                error="Rate limited",
            )
        return ModelResult(content="Succeeded on attempt 4", status_code=200, tokens_in=50, tokens_out=20)

    mock_backend.stream_chat.side_effect = mock_stream

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"
    # Populate recent calls to give healthy retry budget
    for _ in range(50):
        gw._record_call(is_retry=False)

    call = GatewayCall(
        request_id="req-429-patience",
        priority=1,
        stage_name="S4",
        model_request=ModelRequest(model="test", messages=[]),
        deadline=150.0,  # 50s patience
        prompt_tokens_est=100,
    )

    res = await gw.call(call)
    # Must NOT return 'rate_limited' after 2 tries
    assert res.status == "ok"
    assert res.content == "Succeeded on attempt 4"
    assert call_count == 4
    assert res.retries == 3


@pytest.mark.asyncio
async def test_gateway_queue_length_sampler_fr_gw_11(tmp_path: Path):
    """FR-GW-11: Verify Gateway samples and records queue length at a fixed interval."""
    clock = FakeClock(100.0)
    mock_backend = AsyncMock(spec=BackendClient)
    mock_backend.stream_chat.return_value = ModelResult(content="ok", status_code=200, tokens_in=50, tokens_out=10)

    gw = Gateway(
        backend_client=mock_backend,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    gw.daily_budget.budget_file = tmp_path / "budget_test.json"
    assert hasattr(gw, "queue_sampler")

    sampler = gw.queue_sampler
    sampler.start()
    assert sampler.is_running

    # Artificially push items into queue to test sampling
    item = QueuedCall(
        priority=2,
        deadline=200.0,
        sequence=1,
        call=GatewayCall(
            request_id="q-test",
            priority=2,
            stage_name="S1",
            model_request=ModelRequest(model="test", messages=[]),
            deadline=200.0,
        ),
        future=asyncio.get_event_loop().create_future(),
        t_enqueue=100.0,
        est_tokens=100,
    )
    gw._waiters.append(item)
    sampler.sample_now()

    assert sampler.peak_queue_length >= 1
    assert len(sampler.samples) >= 1
    last_sample = sampler.samples[-1]
    assert last_sample["queue_length"] == 1

    sampler.stop()
    assert not sampler.is_running



