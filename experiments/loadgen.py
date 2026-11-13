"""experiments/loadgen.py - Open-loop load generator for The Relay.

Implements the five required load scenarios:
1. steady: Constant arrival rate below limit (baseline latency).
2. burst: Sudden spike to multiple of capacity, testing queueing & recovery.
3. sustained_overload: Arrival rate above capacity, testing shedding and ladder.
4. limit_halved: Capacity halved, testing gateway adaptation.
5. daily_exhausted: Daily budget running out mid-run, testing L4 cutoff.
Plus:
6. retry_storm: Paired comparison of naive retries vs budgeted jittered retries under overload.

Open-loop property:
Requests are dispatched on schedule regardless of whether earlier ones have finished.
Logs scheduled vs actual send times to verify the generator keeps up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx
import numpy as np

from src.backends import BackendClient
from src.gateway import DailyBudget, Gateway
from src.pipeline import Pipeline, PipelineResponse

logger = logging.getLogger(__name__)

# Representative questions from the three papers
QUESTION_POOL = [
    "How many parallel attention heads does the base Transformer use?",
    "What neural audio codec does Moshi use for real-time dialogue?",
    "What hierarchical architecture is introduced in Dynamic Chunking?",
    "What is the key dimension dk in the Transformer base model?",
    "What speech tokenizer framerate does Mimi operate at?",
    "How does H-Net learn data-dependent chunk boundaries?",
    "What BLEU score did the Transformer base model achieve on English-to-German?",
    "What are the primary components of H-Net in Dynamic Chunking?",
    "What is the latency of Moshi text-speech foundation model?",
    "What optimizer and learning rate schedule was used for the base Transformer?",
]


@dataclass
class ScheduledRequest:
    index: int
    scheduled_offset_s: float
    question: str
    scenario: str
    request_id: str


@dataclass
class LoadRunSummary:
    scenario: str
    total_requests: int
    duration_s: float
    offered_rate_per_min: float
    generator_max_lag_ms: float
    succeeded_count: int
    rate_limited_count: int
    timeout_count: int
    error_count: int
    ttft_p50_ms: float
    ttft_p95_ms: float
    ttft_p99_ms: float
    total_p50_ms: float
    total_p95_ms: float
    total_p99_ms: float
    retries_per_req: float
    tokens_in_avg: float
    tokens_out_avg: float
    peak_queue_len: int = 0


class OpenLoopLoadGenerator:
    """Dispatches requests on a pre-planned open-loop schedule."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        standin_base_url: str = "http://127.0.0.1:8000/v1",
    ):
        self.root = Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path) if config_path else self.root / "config.yaml"
        self.standin_base_url = standin_base_url

    async def _reset_standin(
        self,
        rpm: float = 1000.0,
        tpm: float = 8000.0,
        concurrency_slowdown: float = 0.37,
    ) -> None:
        """Resets the stand-in server state before a scenario run."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                admin_base = self.standin_base_url.replace("/v1", "")
                await client.post(f"{admin_base}/admin/reset")
                await client.post(
                    f"{admin_base}/admin/config",
                    json={
                        "rpm_limit": rpm,
                        "tpm_limit": tpm,
                        "concurrency_slowdown": concurrency_slowdown,
                    },
                )
        except Exception as e:
            logger.warning("Could not reach stand-in admin endpoint: %s. Continuing...", e)

    def plan_schedule(
        self,
        scenario: str,
        count: int,
        rate_per_min: float | None = None,
    ) -> list[ScheduledRequest]:
        """Generates arrival schedule upfront for open-loop dispatch."""
        schedule: list[ScheduledRequest] = []

        if scenario == "steady":
            # Constant rate strictly below measured TPM limits (7,200 TPM ceiling = 120 tokens/s).
            # Arithmetic: Standard request consumes ~800 tokens (S1 ~115 + S4 ~530 + S5 ~365).
            # At 6.0 req/min (interval 10.0s), offered rate is 6 * 800 = 4,800 TPM, which is 66.7% of TPM capacity.
            # Refill rate (120 tok/s) adds 1,200 tokens every 10s > 800 tokens consumed, guaranteeing 0 queue growth.
            rpm = rate_per_min or 6.0
            interval = 60.0 / rpm
            for i in range(count):
                q = QUESTION_POOL[i % len(QUESTION_POOL)]
                schedule.append(
                    ScheduledRequest(
                        index=i,
                        scheduled_offset_s=i * interval,
                        question=q,
                        scenario="steady",
                        request_id=f"steady-{i:03d}",
                    )
                )

        elif scenario == "burst":
            # 3 baseline requests (5s apart), sudden burst of (count - 6) requests (0.15s apart), then 3 quiet (5s apart)
            t = 0.0
            pre_burst = 3 if count >= 10 else 1
            post_burst = 3 if count >= 10 else 1
            burst_size = count - pre_burst - post_burst

            idx = 0
            for _ in range(pre_burst):
                schedule.append(
                    ScheduledRequest(
                        idx,
                        t,
                        QUESTION_POOL[idx % len(QUESTION_POOL)],
                        "burst",
                        f"burst-{idx:03d}",
                    )
                )
                t += 5.0
                idx += 1

            for _ in range(burst_size):
                schedule.append(
                    ScheduledRequest(
                        idx,
                        t,
                        QUESTION_POOL[idx % len(QUESTION_POOL)],
                        "burst",
                        f"burst-{idx:03d}",
                    )
                )
                t += 0.15  # Offered rate = 400 req/min during burst
                idx += 1

            for _ in range(post_burst):
                schedule.append(
                    ScheduledRequest(
                        idx,
                        t,
                        QUESTION_POOL[idx % len(QUESTION_POOL)],
                        "burst",
                        f"burst-{idx:03d}",
                    )
                )
                t += 5.0
                idx += 1

        elif scenario == "sustained_overload":
            # Sustained overload kept strictly separate from steady:
            # Rate consistently exceeding token bucket capacity (60 req/min -> 1 req every 1.0s)
            # Arithmetic: 60 req/min * 800 tokens/req = 48,000 TPM, which is 6.7x the 7,200 TPM ceiling!
            rpm = rate_per_min or 60.0
            interval = 60.0 / rpm
            for i in range(count):
                q = QUESTION_POOL[i % len(QUESTION_POOL)]
                schedule.append(
                    ScheduledRequest(
                        index=i,
                        scheduled_offset_s=i * interval,
                        question=q,
                        scenario="sustained_overload",
                        request_id=f"overload-{i:03d}",
                    )
                )

        elif scenario == "limit_halved":
            # Constant rate (6.0 req/min = 4,800 TPM), but limits are cut in half to 3,600 TPM (60 tok/s).
            # Arithmetic: 4,800 TPM offered > 3,600 TPM capacity (133% load), forcing gateway adaptation.
            rpm = rate_per_min or 6.0
            interval = 60.0 / rpm
            for i in range(count):
                q = QUESTION_POOL[i % len(QUESTION_POOL)]
                schedule.append(
                    ScheduledRequest(
                        index=i,
                        scheduled_offset_s=i * interval,
                        question=q,
                        scenario="limit_halved",
                        request_id=f"halved-{i:03d}",
                    )
                )

        elif scenario == "daily_exhausted":
            # Normal rate (6.0 req/min), but daily budget configured to run out mid-run
            rpm = rate_per_min or 6.0
            interval = 60.0 / rpm
            for i in range(count):
                q = QUESTION_POOL[i % len(QUESTION_POOL)]
                schedule.append(
                    ScheduledRequest(
                        index=i,
                        scheduled_offset_s=i * interval,
                        question=q,
                        scenario="daily_exhausted",
                        request_id=f"daily-{i:03d}",
                    )
                )

        return schedule

    async def execute_scenario(
        self,
        scenario: str,
        count: int = 15,
        rate_per_min: float | None = None,
        tmp_budget_file: Path | str | None = None,
        tmp_results_csv: Path | str | None = None,
        tmp_traces_jsonl: Path | str | None = None,
        override_limits: dict[str, Any] | None = None,
    ) -> LoadRunSummary:
        """Executes an open-loop scenario against the stand-in server."""
        print("\n=======================================================")
        print(f"[*] Starting Load Scenario: {scenario.upper()} ({count} requests)")
        print("=======================================================")

        # 1. Reset stand-in server
        standin_rpm = (
            500.0
            if scenario == "limit_halved"
            else (override_limits.get("rpm", 1000.0) if override_limits else 1000.0)
        )
        standin_tpm = (
            4000.0
            if scenario == "limit_halved"
            else (override_limits.get("tpm", 8000.0) if override_limits else 8000.0)
        )
        await self._reset_standin(rpm=standin_rpm, tpm=standin_tpm)

        # 2. Build Pipeline in Stand-In mode
        client = BackendClient(base_url=self.standin_base_url)
        gw = Gateway(
            config_path=self.config_path,
            backend_client=client,
        )

        if scenario == "limit_halved":
            gw.rpm_limit = 450.0
            gw.tpm_limit = 3600.0
            gw.rpm_bucket.capacity = 450.0
            gw.rpm_bucket.tokens = 450.0
            gw.rpm_bucket.refill_rate = 450.0 / 60.0
            gw.tpm_bucket.capacity = 3600.0
            gw.tpm_bucket.tokens = 3600.0
            gw.tpm_bucket.refill_rate = 3600.0 / 60.0

        if scenario == "daily_exhausted":
            # Set daily budget ceiling to 4,000 tokens so request 4-5 exhausts it
            budget_path = (
                Path(tmp_budget_file)
                if tmp_budget_file
                else self.root / "results" / "loadgen_daily_budget.json"
            )
            gw.daily_budget = DailyBudget(
                budget_file=budget_path,
                requests_limit=100,
                tokens_limit=3500,  # Runs out around 4-5 requests
            )
        elif tmp_budget_file:
            gw.daily_budget = DailyBudget(
                budget_file=tmp_budget_file,
                requests_limit=900,
                tokens_limit=90000,
            )

        pipeline = Pipeline(
            config_path=self.config_path,
            backend_client=client,
            gateway=gw,
        )
        if tmp_results_csv:
            pipeline.results_csv_path = Path(tmp_results_csv)
        if tmp_traces_jsonl:
            pipeline.traces_jsonl_path = Path(tmp_traces_jsonl)

        # 3. Plan open-loop schedule
        schedule = self.plan_schedule(scenario, count, rate_per_min)

        # Fail loudly if any request_id repeats
        seen_ids = set()
        for item in schedule:
            if item.request_id in seen_ids:
                raise ValueError(f"Duplicate request_id detected: {item.request_id}")
            seen_ids.add(item.request_id)
        total_planned_duration = schedule[-1].scheduled_offset_s if schedule else 0.0
        offered_rate = (
            (count / total_planned_duration) * 60.0 if total_planned_duration > 0 else 60.0
        )

        results: list[PipelineResponse] = []
        max_generator_lag = 0.0

        async def worker(item: ScheduledRequest, t_zero: float):
            nonlocal max_generator_lag
            target_time = t_zero + item.scheduled_offset_s
            now = time.perf_counter()
            sleep_needed = target_time - now
            if sleep_needed > 0:
                await asyncio.sleep(sleep_needed)

            actual_send = time.perf_counter()
            lag_ms = (actual_send - target_time) * 1000.0
            if lag_ms > max_generator_lag:
                max_generator_lag = lag_ms

            res = await pipeline.run(
                question=item.question,
                scenario=item.scenario,
                request_id=item.request_id,
            )
            results.append(res)
            print(
                f"  [{item.request_id}] Status: {res.status:12s} | TTFT: {res.first_token_ms:6.1f}ms | "
                f"Total: {res.total_ms:6.1f}ms | DegLvl: {res.degradation_level} | Tokens: {res.tokens_in}in/{res.tokens_out}out"
            )

        # 4. Launch open-loop tasks
        gw.queue_sampler.start()
        t_start_wall = time.perf_counter()
        tasks = [asyncio.create_task(worker(item, t_start_wall)) for item in schedule]
        await asyncio.gather(*tasks)
        t_end_wall = time.perf_counter()
        gw.queue_sampler.stop()
        real_duration_s = t_end_wall - t_start_wall

        # 5. Compute summary statistics
        succeeded = sum(1 for r in results if r.status == "ok")
        rate_limited = sum(1 for r in results if r.status == "rate_limited")
        timeouts = sum(
            1 for r in results if r.status in ("timeout", "dropped") or r.outcome == "given_up"
        )
        errors = sum(1 for r in results if r.status not in ("ok", "rate_limited", "timeout"))

        ttfts = [r.first_token_ms for r in results if r.first_token_ms > 0]
        totals = [r.total_ms for r in results if r.total_ms > 0]
        retries = [r.retries for r in results]
        tokens_in = [r.tokens_in for r in results]
        tokens_out = [r.tokens_out for r in results]

        summary = LoadRunSummary(
            scenario=scenario,
            total_requests=len(results),
            duration_s=round(real_duration_s, 2),
            offered_rate_per_min=round(offered_rate, 1),
            generator_max_lag_ms=round(max_generator_lag, 2),
            succeeded_count=succeeded,
            rate_limited_count=rate_limited,
            timeout_count=timeouts,
            error_count=errors,
            ttft_p50_ms=round(float(np.percentile(ttfts, 50)), 1) if ttfts else 0.0,
            ttft_p95_ms=round(float(np.percentile(ttfts, 95)), 1) if ttfts else 0.0,
            ttft_p99_ms=round(float(np.percentile(ttfts, 99)), 1) if ttfts else 0.0,
            total_p50_ms=round(float(np.percentile(totals, 50)), 1) if totals else 0.0,
            total_p95_ms=round(float(np.percentile(totals, 95)), 1) if totals else 0.0,
            total_p99_ms=round(float(np.percentile(totals, 99)), 1) if totals else 0.0,
            retries_per_req=round(float(np.mean(retries)), 2) if retries else 0.0,
            tokens_in_avg=round(float(np.mean(tokens_in)), 1) if tokens_in else 0.0,
            tokens_out_avg=round(float(np.mean(tokens_out)), 1) if tokens_out else 0.0,
            peak_queue_len=gw.queue_sampler.peak_queue_length,
        )

        print(f"\n--- Scenario Summary: {scenario} ---")
        print(f"Requests: {summary.total_requests} in {summary.duration_s}s")
        print(f"Offered Rate: {summary.offered_rate_per_min} req/min")
        print(
            f"Outcomes: Succeeded: {succeeded}/{count} ({succeeded / count * 100:.1f}%), "
            f"Rate-limited: {rate_limited}, Timeouts/Given-up: {timeouts}, Errors: {errors}"
        )
        print(
            f"Latency TTFT: p50={summary.ttft_p50_ms}ms, p95={summary.ttft_p95_ms}ms, p99={summary.ttft_p99_ms}ms"
        )
        print(
            f"Latency Total: p50={summary.total_p50_ms}ms, p95={summary.total_p95_ms}ms, p99={summary.total_p99_ms}ms"
        )
        print(
            f"Tokens/Answer (avg): {summary.tokens_in_avg + summary.tokens_out_avg:.1f} "
            f"({summary.tokens_in_avg} in / {summary.tokens_out_avg} out)"
        )
        print(f"Generator Max Scheduling Lag: {summary.generator_max_lag_ms}ms")

        return summary

    async def run_retry_comparison(self, count: int = 15) -> tuple[LoadRunSummary, LoadRunSummary]:
        """Paired experiment comparing (a) naive immediate retries vs (b) budgeted jittered retries under load."""
        print("\n=======================================================")
        print("[*] Starting Retry Experiment: Naive vs Budgeted Policy")
        print("=======================================================")

        # Case A: Naive immediate retries without budget cap
        client = BackendClient(base_url=self.standin_base_url)
        gw_naive = Gateway(config_path=self.config_path, backend_client=client)
        gw_naive.max_retry_attempts = 4
        gw_naive.retry_budget_share = 1.0  # Unlimited retries allowed
        gw_naive.backoff_base_s = 0.05  # Immediate retry
        gw_naive.backoff_max_s = 0.10

        pipe_naive = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_naive)

        await self._reset_standin(rpm=1000.0, tpm=8000.0, concurrency_slowdown=0.50)
        sched = self.plan_schedule("sustained_overload", count=count, rate_per_min=120.0)

        results_naive: list[PipelineResponse] = []
        for item in sched:
            res = await pipe_naive.run(
                item.question, scenario="retry_naive", request_id=f"naive-{item.index:03d}"
            )
            results_naive.append(res)
            await asyncio.sleep(0.1)

        # Case B: Budgeted jittered retries
        gw_budgeted = Gateway(config_path=self.config_path, backend_client=client)
        gw_budgeted.max_retry_attempts = 2
        gw_budgeted.retry_budget_share = 0.10
        gw_budgeted.backoff_base_s = 0.5
        gw_budgeted.backoff_max_s = 2.0

        pipe_budgeted = Pipeline(
            config_path=self.config_path, backend_client=client, gateway=gw_budgeted
        )
        await self._reset_standin(rpm=1000.0, tpm=8000.0, concurrency_slowdown=0.50)

        results_budgeted: list[PipelineResponse] = []
        for item in sched:
            res = await pipe_budgeted.run(
                item.question, scenario="retry_budgeted", request_id=f"budgeted-{item.index:03d}"
            )
            results_budgeted.append(res)
            await asyncio.sleep(0.1)

        retries_naive = sum(r.retries for r in results_naive)
        retries_budgeted = sum(r.retries for r in results_budgeted)

        n = count
        succ_naive = sum(1 for r in results_naive if r.status == "ok" and r.total_ms <= 10000.0)
        given_up_naive = sum(
            1
            for r in results_naive
            if r.status in ("timeout", "dropped") or r.outcome == "given_up" or r.status == "rate_limited"
        )
        totals_naive = [r.total_ms for r in results_naive if r.total_ms > 0]
        p95_naive = float(np.percentile(totals_naive, 95)) if totals_naive else 0.0

        succ_budgeted = sum(1 for r in results_budgeted if r.status == "ok" and r.total_ms <= 10000.0)
        given_up_budgeted = sum(
            1
            for r in results_budgeted
            if r.status in ("timeout", "dropped") or r.outcome == "given_up" or r.status == "rate_limited"
        )
        totals_budgeted = [r.total_ms for r in results_budgeted if r.total_ms > 0]
        p95_budgeted = float(np.percentile(totals_budgeted, 95)) if totals_budgeted else 0.0

        goodput_naive_pct = (succ_naive / n) * 100.0
        goodput_budgeted_pct = (succ_budgeted / n) * 100.0
        given_up_naive_pct = (given_up_naive / n) * 100.0
        given_up_budgeted_pct = (given_up_budgeted / n) * 100.0

        print("\n========================================================================================")
        print("[*] PAIRED RETRY EXPERIMENT (NAIVE VS BUDGETED RETRIES UNDER OVERLOAD)")
        print("========================================================================================")
        print(f"{'Metric':30s} | {'Naive Retries':20s} | {'Budgeted Policy':20s} | {'Delta':10s}")
        print("-" * 88)
        print(f"{'Requests Sent':30s} | {n:<20d} | {n:<20d} | -")
        print(
            f"{'Goodput within Patience (%)':30s} | {goodput_naive_pct:5.1f}% ({succ_naive:<2d})        | {goodput_budgeted_pct:5.1f}% ({succ_budgeted:<2d})        | {goodput_budgeted_pct - goodput_naive_pct:+5.1f}%"
        )
        print(
            f"{'Given-up / Timed-out (%)':30s} | {given_up_naive_pct:5.1f}% ({given_up_naive:<2d})        | {given_up_budgeted_pct:5.1f}% ({given_up_budgeted:<2d})        | {given_up_budgeted_pct - given_up_naive_pct:+5.1f}%"
        )
        print(
            f"{'Total Retries Incurred':30s} | {retries_naive:<20d} | {retries_budgeted:<20d} | {retries_budgeted - retries_naive:+d}"
        )
        print(
            f"{'Retries per Request':30s} | {retries_naive/n:<20.2f} | {retries_budgeted/n:<20.2f} | {retries_budgeted/n - retries_naive/n:+.2f}"
        )
        print(
            f"{'Total Latency p95 (ms)':30s} | {p95_naive:<17.1f} ms | {p95_budgeted:<17.1f} ms | {p95_budgeted - p95_naive:+7.1f} ms"
        )
        print("========================================================================================")


async def run_all_scenarios():
    """Runs remaining load scenarios and the retry comparison."""
    gen = OpenLoopLoadGenerator()

    # 1. Steady (Already completed: 25 requests)
    # await gen.execute_scenario(scenario="steady", count=25)

    # 2. Burst (50 requests: 3 pre, 44 burst at 400 rpm, 3 post)
    await gen.execute_scenario(scenario="burst", count=50)

    # 3. Sustained Overload (60 requests at 60 rpm = 48,000 TPM, 6.7x ceiling)
    await gen.execute_scenario(scenario="sustained_overload", count=60)

    # 4. Limit Halved (15 requests)
    await gen.execute_scenario(scenario="limit_halved", count=15)

    # 5. Daily Budget Exhausted (15 requests)
    await gen.execute_scenario(scenario="daily_exhausted", count=15)

    # 6. Retry Experiment (Naive vs Budgeted under 120 rpm overload, 30 requests each)
    await gen.run_retry_comparison(count=30)


if __name__ == "__main__":
    asyncio.run(run_all_scenarios())
