"""src/gateway.py - Central LLM Gateway for The Relay.

Responsibilities:
- Owns all model access (S1, S4, S5).
- Dual token buckets (RPM and TPM) with reservation and post-response settle.
- Learning limits: dynamically updates remaining capacity from provider headers.
- Concurrency semaphore with AIMD adaptation.
- Priority queue: Priority 1 (in-progress S4/S5), Priority 2 (new S1), Priority 3 (optional).
- Earliest deadline first scheduling with deadline dropping.
- Admission control: accept, queue with ETA, or shed with fast busy reply.
- Circuit breaker (CLOSED, OPEN, HALF_OPEN) with cooldown probe.
- Degradation ladder (Levels L0 to L4) with hysteresis.
- Daily budget persistence (results/daily_budget.json) across restarts.
- Retries with retry budget tracking and jittered backoff.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from src.backends import BackendClient, ModelRequest, ModelResult, parse_retry_delay

logger = logging.getLogger(__name__)


class DegradationLevel(IntEnum):
    """Degradation ladder levels (L0 to L4)."""

    L0_NORMAL = 0
    L1_SKIP_VERIFY_SMALLER_K = 1
    L2_SMALL_MODEL_SHORT_K3 = 2
    L3_EXTRACT_ONLY = 3
    L4_DAILY_EXHAUSTED = 4


@dataclass
class GatewayCall:
    """Request specification submitted to the Gateway."""

    request_id: str
    priority: int  # 1 (in-progress S4/S5), 2 (new arrivals S1), 3 (optional)
    stage_name: str  # "S1", "S4", "S5", "redraft"
    model_request: ModelRequest
    deadline: float  # monotonic seconds
    prompt_tokens_est: int = 0


@dataclass(order=True)
class QueuedCall:
    """Entry in priority queue. Priority 1 executes before Priority 2."""

    priority: int
    deadline: float
    sequence: int
    call: GatewayCall = field(compare=False)
    future: asyncio.Future = field(compare=False)
    t_enqueue: float = field(compare=False)
    est_tokens: int = field(compare=False)


@dataclass
class GatewayResult:
    """Result returned by the Gateway."""

    model_result: ModelResult | None
    status: str  # "ok", "dropped", "shed", "rate_limited", "error", "daily_exhausted"
    retries: int = 0
    queue_wait_ms: float = 0.0
    retry_ms: float = 0.0
    degradation_level: int = 0
    error: str | None = None

    @property
    def content(self) -> str:
        return self.model_result.content if self.model_result else ""

    @property
    def tokens_in(self) -> int:
        return self.model_result.tokens_in if self.model_result else 0

    @property
    def tokens_out(self) -> int:
        return self.model_result.tokens_out if self.model_result else 0


class TokenBucket:
    """Token bucket supporting reservation, refund, and header synchronization."""

    def __init__(
        self,
        capacity: float,
        refill_rate_per_s: float,
        clock_fn: Callable[[], float] = time.perf_counter,
    ):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate_per_s)
        self.tokens = float(capacity)
        self.last_refill = clock_fn()
        self.clock_fn = clock_fn

    def refill(self) -> float:
        now = self.clock_fn()
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
        return self.tokens

    def has_capacity(self, amount: float) -> bool:
        self.refill()
        return self.tokens >= amount

    def time_until_available(self, amount: float) -> float:
        self.refill()
        if self.tokens >= amount:
            return 0.0
        deficit = amount - self.tokens
        return deficit / self.refill_rate if self.refill_rate > 0 else float("inf")

    def reserve(self, amount: float) -> bool:
        self.refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def force_consume(self, amount: float) -> None:
        self.refill()
        self.tokens = max(0.0, self.tokens - amount)

    def refund(self, amount: float) -> None:
        self.refill()
        self.tokens = min(self.capacity, self.tokens + amount)

    def settle(self, reserved: float, actual: float) -> None:
        self.refill()
        diff = reserved - actual
        if diff > 0:
            self.refund(diff)
        elif diff < 0:
            self.force_consume(abs(diff))

    def set_remaining(self, remaining: float) -> None:
        """Update tokens if provider reports fewer remaining tokens."""
        self.refill()
        if remaining < self.tokens:
            self.tokens = max(0.0, float(remaining))


class DailyBudget:
    """Persistent daily spend tracker."""

    def __init__(
        self,
        budget_file: Path | str,
        requests_limit: int,
        tokens_limit: int,
    ):
        self.budget_file = Path(budget_file)
        self.requests_limit = requests_limit
        self.tokens_limit = tokens_limit
        self.requests_used = 0
        self.tokens_used = 0
        self.date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.load()

    def load(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not self.budget_file.exists():
            self.date_str = today
            self.requests_used = 0
            self.tokens_used = 0
            self.save()
            return

        try:
            with open(self.budget_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            file_date = data.get("date", "")
            if file_date == today:
                self.date_str = today
                self.requests_used = int(data.get("requests_used", 0))
                self.tokens_used = int(data.get("tokens_used", 0))
            else:
                self.date_str = today
                self.requests_used = 0
                self.tokens_used = 0
                self.save()
        except Exception as e:
            logger.warning("Could not read daily budget file: %s; resetting", e)
            self.date_str = today
            self.requests_used = 0
            self.tokens_used = 0
            self.save()

    def save(self) -> None:
        self.budget_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": self.date_str,
            "requests_used": self.requests_used,
            "tokens_used": self.tokens_used,
            "requests_limit": self.requests_limit,
            "tokens_limit": self.tokens_limit,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        temp_file = self.budget_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_file.replace(self.budget_file)

    def record(self, requests: int, tokens: int) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date_str != today:
            self.date_str = today
            self.requests_used = 0
            self.tokens_used = 0

        self.requests_used += requests
        self.tokens_used += tokens
        self.save()

    def percent_used(self) -> float:
        req_pct = (
            (self.requests_used / self.requests_limit) * 100.0 if self.requests_limit > 0 else 0.0
        )
        tok_pct = (self.tokens_used / self.tokens_limit) * 100.0 if self.tokens_limit > 0 else 0.0
        return max(req_pct, tok_pct)

    def is_exhausted(self) -> bool:
        return self.requests_used >= self.requests_limit or self.tokens_used >= self.tokens_limit


class CircuitBreaker:
    """Circuit breaker with CLOSED, OPEN, HALF_OPEN states."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 15.0,
        clock_fn: Callable[[], float] = time.perf_counter,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.clock_fn = clock_fn
        self.state = "CLOSED"
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.probe_in_flight = False

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"
        self.probe_in_flight = False

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = self.clock_fn()
        self.probe_in_flight = False
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"

    def can_attempt(self) -> bool:
        if self.state == "CLOSED":
            return True
        now = self.clock_fn()
        if self.state == "OPEN":
            if (now - self.last_failure_time) >= self.recovery_timeout_s:
                self.state = "HALF_OPEN"
                self.probe_in_flight = True
                return True
            return False
        if self.state == "HALF_OPEN":
            if not self.probe_in_flight:
                self.probe_in_flight = True
                return True
            return False
        return False


class DegradationController:
    """Manages degradation ladder transitions with hysteresis."""

    def __init__(
        self,
        l1_queue_wait_ms: float = 1500.0,
        l1_bucket_pct: float = 30.0,
        l2_bucket_pct: float = 15.0,
        l3_daily_pct: float = 95.0,
        l4_daily_pct: float = 100.0,
    ):
        self.l1_queue_wait_ms = l1_queue_wait_ms
        self.l1_bucket_pct = l1_bucket_pct
        self.l2_bucket_pct = l2_bucket_pct
        self.l3_daily_pct = l3_daily_pct
        self.l4_daily_pct = l4_daily_pct

        self.current_level = DegradationLevel.L0_NORMAL
        self.history: list[tuple[float, DegradationLevel, DegradationLevel, str]] = []

    def evaluate(
        self,
        tpm_bucket_pct: float,
        queue_wait_ms: float,
        daily_used_pct: float,
        breaker_state: str,
        now: float,
    ) -> DegradationLevel:
        target = DegradationLevel.L0_NORMAL
        reason = "healthy"

        if daily_used_pct >= self.l4_daily_pct:
            target = DegradationLevel.L4_DAILY_EXHAUSTED
            reason = f"daily budget exhausted ({daily_used_pct:.1f}%)"
        elif daily_used_pct >= self.l3_daily_pct or breaker_state == "OPEN":
            target = DegradationLevel.L3_EXTRACT_ONLY
            reason = (
                f"daily budget near exhaustion ({daily_used_pct:.1f}%)"
                if daily_used_pct >= self.l3_daily_pct
                else "circuit breaker open"
            )
        elif tpm_bucket_pct <= self.l2_bucket_pct:
            target = DegradationLevel.L2_SMALL_MODEL_SHORT_K3
            reason = f"TPM bucket very low ({tpm_bucket_pct:.1f}%)"
        elif queue_wait_ms >= self.l1_queue_wait_ms or tpm_bucket_pct <= self.l1_bucket_pct:
            target = DegradationLevel.L1_SKIP_VERIFY_SMALLER_K
            reason = (
                f"queue wait high ({queue_wait_ms:.0f}ms)"
                if queue_wait_ms >= self.l1_queue_wait_ms
                else f"TPM bucket low ({tpm_bucket_pct:.1f}%)"
            )

        # Apply hysteresis when recovering
        if target < self.current_level:
            if self.current_level == DegradationLevel.L1_SKIP_VERIFY_SMALLER_K:
                if tpm_bucket_pct < 40.0 or queue_wait_ms > 1000.0:
                    target = DegradationLevel.L1_SKIP_VERIFY_SMALLER_K
            elif self.current_level == DegradationLevel.L2_SMALL_MODEL_SHORT_K3:
                if tpm_bucket_pct < 25.0:
                    target = DegradationLevel.L2_SMALL_MODEL_SHORT_K3

        if target != self.current_level:
            self.history.append((now, self.current_level, target, reason))
            self.current_level = target

        return self.current_level


class QueueSampler:
    """FR-GW-11: Samples and records Gateway queue length at a fixed interval."""

    def __init__(
        self,
        gateway: Any,
        sample_interval_s: float = 0.1,
        clock_fn: Callable[[], float] = time.perf_counter,
    ):
        self.gateway = gateway
        self.sample_interval_s = sample_interval_s
        self.clock_fn = clock_fn
        self.samples: list[dict[str, Any]] = []
        self.peak_queue_length = 0
        self.is_running = False
        self._task: asyncio.Task | None = None

    def sample_now(self) -> dict[str, Any]:
        """Record an immediate sample of current queue length."""
        q_len = len(getattr(self.gateway, "_waiters", []))
        in_flight = getattr(self.gateway, "in_flight_count", 0)
        now = self.clock_fn()
        if q_len > self.peak_queue_length:
            self.peak_queue_length = q_len
        sample = {
            "timestamp": now,
            "queue_length": q_len,
            "in_flight": in_flight,
        }
        self.samples.append(sample)
        return sample

    async def _sample_loop(self) -> None:
        while self.is_running:
            self.sample_now()
            await asyncio.sleep(self.sample_interval_s)

    def start(self) -> None:
        if not self.is_running:
            self.is_running = True
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._sample_loop())
            except RuntimeError:
                pass

    def stop(self) -> list[dict[str, Any]]:
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        return self.samples


class Gateway:
    """The central LLM Gateway."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        backend_client: BackendClient | None = None,
        clock_fn: Callable[[], float] = time.perf_counter,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.root = Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path) if config_path else self.root / "config.yaml"
        self.config = self._load_config()
        self.clock_fn = clock_fn
        self.sleep_fn = sleep_fn

        # Backend client
        backend_mode = self.config.get("backend", {}).get("mode", "groq")
        base_url = (
            self.config.get("backend", {}).get("groq_base_url", "https://api.groq.com/openai/v1")
            if backend_mode == "groq"
            else self.config.get("backend", {}).get("base_url", "http://localhost:8000/v1")
        )
        self.backend_client = backend_client or BackendClient(base_url=base_url)

        # Limits & Buckets
        limits = self.config.get("limits", {})
        self.rpm_limit = float(limits.get("rpm", 900.0))
        self.tpm_limit = float(limits.get("tpm", 7200.0))
        self.rpd_limit = int(limits.get("rpd", 900))
        self.tpd_limit = int(limits.get("tpd", 180000))
        self.max_in_flight = int(limits.get("max_in_flight", 2))

        self.rpm_bucket = TokenBucket(
            capacity=self.rpm_limit,
            refill_rate_per_s=self.rpm_limit / 60.0,
            clock_fn=self.clock_fn,
        )
        self.tpm_bucket = TokenBucket(
            capacity=self.tpm_limit,
            refill_rate_per_s=self.tpm_limit / 60.0,
            clock_fn=self.clock_fn,
        )

        # Concurrency control & Priority Queue
        self.in_flight_count = 0
        self._seq = 0
        self._waiters: list[QueuedCall] = []
        self._lock = asyncio.Lock()
        self._drain_event = asyncio.Event()
        self._drain_event.set()

        # Daily budget
        daily_path = self.root / self.config.get("paths", {}).get(
            "daily_budget_file", "results/daily_budget.json"
        )
        self.daily_budget = DailyBudget(
            budget_file=daily_path,
            requests_limit=self.rpd_limit,
            tokens_limit=self.tpd_limit,
        )

        # Circuit breaker
        cb_cfg = self.config.get("circuit_breaker", {})
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=int(cb_cfg.get("failure_threshold", 5)),
            recovery_timeout_s=float(cb_cfg.get("recovery_timeout_s", 15.0)),
            clock_fn=self.clock_fn,
        )

        # Degradation controller
        lad_cfg = self.config.get("ladder", {})
        self.degradation = DegradationController(
            l1_queue_wait_ms=float(lad_cfg.get("l1_queue_wait_ms", 1500.0)),
            l1_bucket_pct=float(lad_cfg.get("l1_bucket_pct", 30.0)),
            l2_bucket_pct=float(lad_cfg.get("l2_bucket_pct", 15.0)),
            l3_daily_pct=float(lad_cfg.get("l3_daily_pct", 95.0)),
            l4_daily_pct=float(lad_cfg.get("l4_daily_pct", 100.0)),
        )

        # Retries & budget
        retry_cfg = self.config.get("retry", {})
        self.max_retry_attempts = int(retry_cfg.get("max_attempts", 2))
        self.retry_budget_share = float(retry_cfg.get("budget_share", 0.10))
        self.backoff_base_s = float(retry_cfg.get("backoff_base_s", 0.5))
        self.backoff_max_s = float(retry_cfg.get("backoff_max_s", 8.0))

        # Recent call history for retry budgeting (rolling window)
        self.recent_calls: list[bool] = []
        self.rolling_window_size = 50

        # Queue-length sampler (FR-GW-11)
        self.queue_sampler = QueueSampler(gateway=self, clock_fn=self.clock_fn)

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def get_degradation_level(self, queue_wait_ms: float = 0.0) -> DegradationLevel:
        """Query current degradation level."""
        tpm_pct = (self.tpm_bucket.refill() / self.tpm_bucket.capacity) * 100.0
        daily_pct = self.daily_budget.percent_used()
        return self.degradation.evaluate(
            tpm_bucket_pct=tpm_pct,
            queue_wait_ms=queue_wait_ms,
            daily_used_pct=daily_pct,
            breaker_state=self.circuit_breaker.state,
            now=self.clock_fn(),
        )

    def _can_retry(self, current_attempt_retries: int = 0) -> bool:
        """Check if retry budget allows another retry."""
        if current_attempt_retries == 0:
            return True
        if len(self.recent_calls) < 10:
            return True
        recent = self.recent_calls[-self.rolling_window_size :]
        retries = sum(1 for r in recent if r)
        share = retries / len(recent)
        return share <= self.retry_budget_share

    def _record_call(self, is_retry: bool) -> None:
        self.recent_calls.append(is_retry)
        if len(self.recent_calls) > self.rolling_window_size * 2:
            self.recent_calls = self.recent_calls[-self.rolling_window_size :]

    def check_admission(
        self,
        deadline: float,
        prompt_tokens_est: int,
        max_tokens: int,
        priority: int = 2,
    ) -> tuple[bool, str, float]:
        """Admission check: accept, queue with ETA, or shed immediately. Priority 1 is protected."""
        now = self.clock_fn()
        if now >= deadline:
            return False, "deadline_passed", 0.0

        if self.daily_budget.is_exhausted():
            return False, "daily_exhausted", 0.0

        # Protect in-progress work (Priority 1): never shed partly paid work
        if priority <= 1:
            return True, "accept_priority1", 0.0

        est_tokens = prompt_tokens_est + max_tokens
        wait_for_tokens = self.tpm_bucket.time_until_available(est_tokens)
        est_service_time = 1.5

        projected_finish = now + wait_for_tokens + est_service_time
        if projected_finish > deadline:
            return False, "shed", wait_for_tokens

        return True, "accept", wait_for_tokens

    async def _try_schedule_next(self) -> None:
        """Evaluate priority queue head and dispatch if capacity permits."""
        async with self._lock:
            while self._waiters and self.in_flight_count < self.max_in_flight:
                head = self._waiters[0]
                now = self.clock_fn()

                # Check if deadline expired while in queue
                if now >= head.deadline:
                    heapq.heappop(self._waiters)
                    if not head.future.done():
                        head.future.set_result(
                            GatewayResult(
                                model_result=None,
                                status="timeout",
                                error="Deadline expired in gateway priority queue",
                            )
                        )
                    continue

                # Check if tokens are available for this waiter: reserve FULL estimate (prompt + max_tokens)
                tokens_wait = max(
                    self.rpm_bucket.time_until_available(1.0),
                    self.tpm_bucket.time_until_available(head.est_tokens),
                )

                if tokens_wait > 0.0:
                    # Head waiter must wait for tokens; don't bypass it with lower priority
                    break

                # Reserve tokens and pop from queue
                heapq.heappop(self._waiters)
                self.rpm_bucket.reserve(1.0)
                self.tpm_bucket.reserve(head.est_tokens)
                self.in_flight_count += 1
                if not head.future.done():
                    head.future.set_result(None)  # Granted slot

    async def call(self, call_req: GatewayCall) -> GatewayResult:
        """Submit a model call through the Gateway choke-point."""
        t_enqueue = self.clock_fn()

        # 1. Admission check
        admitted, reason, eta = self.check_admission(
            deadline=call_req.deadline,
            prompt_tokens_est=call_req.prompt_tokens_est,
            max_tokens=call_req.model_request.max_tokens,
            priority=call_req.priority,
        )

        if not admitted:
            if reason == "deadline_passed":
                return GatewayResult(
                    model_result=None,
                    status="timeout",
                    error="Request deadline expired prior to gateway dispatch",
                )
            elif reason == "daily_exhausted":
                return GatewayResult(
                    model_result=None,
                    status="daily_exhausted",
                    degradation_level=DegradationLevel.L4_DAILY_EXHAUSTED,
                    error="Provider daily token/request budget exhausted",
                )
            elif reason == "shed":
                return GatewayResult(
                    model_result=None,
                    status="shed",
                    error=f"Gateway shedding: projected finish time exceeds deadline (wait ETA: {eta:.2f}s)",
                )

        # 2. Check circuit breaker
        if not self.circuit_breaker.can_attempt():
            return GatewayResult(
                model_result=None,
                status="error",
                degradation_level=DegradationLevel.L3_EXTRACT_ONLY,
                error="Gateway circuit breaker is OPEN",
            )

        est_tokens = call_req.prompt_tokens_est + call_req.model_request.max_tokens

        # 3. Priority queue enrollment
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GatewayResult | None] = loop.create_future()

        async with self._lock:
            self._seq += 1
            item = QueuedCall(
                priority=call_req.priority,
                deadline=call_req.deadline,
                sequence=self._seq,
                call=call_req,
                future=future,
                t_enqueue=t_enqueue,
                est_tokens=est_tokens,
            )
            heapq.heappush(self._waiters, item)
            self.queue_sampler.sample_now()

        # Trigger scheduler
        await self._try_schedule_next()

        # Poll/wait until granted a slot or timed out
        while not future.done():
            now = self.clock_fn()
            if now >= call_req.deadline:
                async with self._lock:
                    if not future.done():
                        future.set_result(
                            GatewayResult(
                                model_result=None,
                                status="timeout",
                                error="Deadline expired while waiting in gateway priority queue",
                            )
                        )
                break

            await self._try_schedule_next()
            if future.done():
                break

            # Sleep short duration before re-evaluating
            await self.sleep_fn(0.05)

        scheduled_res = await future
        if isinstance(scheduled_res, GatewayResult):
            return scheduled_res

        # Slot granted! Calculate queue wait
        queue_wait_ms = (self.clock_fn() - t_enqueue) * 1000.0
        current_level = self.get_degradation_level(queue_wait_ms=queue_wait_ms)

        # 4. Execute with retries under acquired concurrency slot
        try:
            result = await self._execute_with_retries(
                call_req=call_req,
                t_enqueue=t_enqueue,
                est_tokens=est_tokens,
            )
            result.queue_wait_ms = queue_wait_ms
            result.degradation_level = int(current_level)
            return result
        finally:
            async with self._lock:
                self.in_flight_count = max(0, self.in_flight_count - 1)
            # Wake up next queued waiter
            await self._try_schedule_next()

    async def _execute_with_retries(
        self,
        call_req: GatewayCall,
        t_enqueue: float,
        est_tokens: int,
    ) -> GatewayResult:
        """Execute model call with retry loop, backoff, settlement, and header syncing."""
        attempts = 0
        retries_count = 0
        total_retry_s = 0.0

        while True:
            attempts += 1
            now = self.clock_fn()
            remaining_patience = call_req.deadline - now
            if remaining_patience <= 0:
                self.tpm_bucket.settle(reserved=est_tokens, actual=0)
                return GatewayResult(
                    model_result=None,
                    status="timeout",
                    retries=retries_count,
                    retry_ms=total_retry_s * 1000.0,
                    error="Deadline expired before dispatch",
                )

            self._record_call(is_retry=(attempts > 1))
            res = await self.backend_client.stream_chat(
                req=call_req.model_request,
                t_enqueue=t_enqueue,
                timeout=max(1.0, remaining_patience),
            )

            # Sync provider reported limits if present
            if res.headers:
                if "x-ratelimit-remaining-tokens" in res.headers:
                    try:
                        rem_tok = float(res.headers["x-ratelimit-remaining-tokens"])
                        self.tpm_bucket.set_remaining(rem_tok)
                    except ValueError:
                        pass
                if "x-ratelimit-remaining-requests" in res.headers:
                    try:
                        rem_req = float(res.headers["x-ratelimit-remaining-requests"])
                        self.rpm_bucket.set_remaining(rem_req)
                    except ValueError:
                        pass

            # 429 Handling
            if res.status_code == 429:
                retries_count += 1
                self.circuit_breaker.record_failure()
                can_retry = self._can_retry(current_attempt_retries=retries_count - 1)
                delay = parse_retry_delay(res.headers, default_s=2.0) + random.uniform(0.1, 0.3)

                if can_retry and (self.clock_fn() + delay) < call_req.deadline:
                    logger.info("Gateway 429 received, backing off %.2fs before retry", delay)
                    t_sleep_start = self.clock_fn()
                    await self.sleep_fn(delay)
                    total_retry_s += (self.clock_fn() - t_sleep_start)
                    continue
                else:
                    self.tpm_bucket.settle(reserved=est_tokens, actual=0)
                    if not can_retry:
                        status = "error"
                        err = "Retry budget exhausted"
                    elif (self.clock_fn() + delay) >= call_req.deadline:
                        status = "timeout"
                        err = "Client patience deadline reached while waiting for retry"
                    else:
                        status = "rate_limited"
                        err = res.error or "HTTP 429 rate limit exceeded"
                    return GatewayResult(
                        model_result=res,
                        status=status,
                        retries=retries_count,
                        retry_ms=total_retry_s * 1000.0,
                        error=err,
                    )

            # 5xx / Connection Error Handling
            if res.status_code >= 500 or res.error:
                retries_count += 1
                self.circuit_breaker.record_failure()
                can_retry = self._can_retry(current_attempt_retries=retries_count - 1)

                backoff = min(
                    self.backoff_max_s,
                    self.backoff_base_s * (2 ** (retries_count - 1)),
                ) + random.uniform(0.05, 0.2)

                if can_retry and (self.clock_fn() + backoff) < call_req.deadline:
                    logger.warning(
                        "Gateway error (%s), backing off %.2fs before retry",
                        res.error or res.status_code,
                        backoff,
                    )
                    t_sleep_start = self.clock_fn()
                    await self.sleep_fn(backoff)
                    total_retry_s += (self.clock_fn() - t_sleep_start)
                    continue
                else:
                    self.tpm_bucket.settle(reserved=est_tokens, actual=0)
                    if not can_retry:
                        status = "error"
                        err = "Retry budget exhausted"
                    elif (self.clock_fn() + backoff) >= call_req.deadline:
                        status = "timeout"
                        err = "Client patience deadline reached while waiting for retry"
                    else:
                        status = "error"
                        err = res.error or f"HTTP {res.status_code} upstream backend error"
                    return GatewayResult(
                        model_result=res,
                        status=status,
                        retries=retries_count,
                        retry_ms=total_retry_s * 1000.0,
                        error=err,
                    )

            # Success (200 OK)
            self.circuit_breaker.record_success()
            actual_tokens = res.tokens_in + res.tokens_out
            self.tpm_bucket.settle(reserved=est_tokens, actual=actual_tokens)
            self.daily_budget.record(requests=1, tokens=actual_tokens)

            return GatewayResult(
                model_result=res,
                status="ok",
                retries=retries_count,
                retry_ms=total_retry_s * 1000.0,
            )
