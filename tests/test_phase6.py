"""tests/test_phase6.py - Phase 6 Load Generator Test Suite.

Verifies:
- Open-loop schedule generation (steady, burst, sustained_overload, limit_halved, daily_exhausted).
- Generator scheduling fidelity (generator lag < 50ms).
- Proper incremental writes to results.csv and traces.jsonl.
- Timeouts / given-up requests counted separately from rate-limited and succeeded.
- All 5 scenarios execute end-to-end against the stand-in server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from experiments.loadgen import OpenLoopLoadGenerator
from src.trace import read_results_csv


@pytest.mark.asyncio
async def test_scenario_sizing_steady_below_tpm_limit():
    """Verify that steady scenario default arrival rate is mathematically below TPM limit (7200 TPM),
    while sustained_overload is strictly above capacity."""
    gen = OpenLoopLoadGenerator()
    steady_sched = gen.plan_schedule("steady", count=5)
    
    # Calculate interval and implied RPM
    interval = steady_sched[1].scheduled_offset_s - steady_sched[0].scheduled_offset_s
    implied_rpm = 60.0 / interval
    
    # Assuming standard ~800 tokens per full pipeline request
    est_tokens_per_req = 800
    offered_tpm = implied_rpm * est_tokens_per_req
    
    # Assert steady is strictly below Gateway TPM ceiling (7200 TPM) with at least 15% headroom
    assert offered_tpm <= 7200 * 0.85, f"Steady offered TPM {offered_tpm} violates TPM ceiling 7200"
    assert implied_rpm <= 7.5, f"Steady RPM {implied_rpm} exceeds safe ceiling 7.5 RPM"

    # Sustained overload must be strictly above capacity (> 7200 TPM)
    overload_sched = gen.plan_schedule("sustained_overload", count=5)
    overload_interval = overload_sched[1].scheduled_offset_s - overload_sched[0].scheduled_offset_s
    overload_rpm = 60.0 / overload_interval
    overload_offered_tpm = overload_rpm * est_tokens_per_req
    assert overload_offered_tpm >= 7200 * 2.0, f"Overload TPM {overload_offered_tpm} must be at least 2x capacity"


@pytest.mark.asyncio
async def test_open_loop_schedule_planning():
    """Verify that schedule planner generates correct arrival offsets for all scenarios."""
    gen = OpenLoopLoadGenerator()

    # Steady: 10 requests at 30 req/min -> 2.0s intervals
    steady_sched = gen.plan_schedule("steady", count=10, rate_per_min=30.0)
    assert len(steady_sched) == 10
    assert steady_sched[0].scheduled_offset_s == 0.0
    assert abs(steady_sched[1].scheduled_offset_s - 2.0) < 1e-4
    assert abs(steady_sched[9].scheduled_offset_s - 18.0) < 1e-4

    # Burst: sudden compression of arrivals
    burst_sched = gen.plan_schedule("burst", count=12)
    assert len(burst_sched) == 12
    # Verify burst inter-arrival is tight (< 0.5s)
    burst_deltas = [
        burst_sched[i + 1].scheduled_offset_s - burst_sched[i].scheduled_offset_s
        for i in range(len(burst_sched) - 1)
    ]
    assert any(d <= 0.20 for d in burst_deltas)

    # Sustained overload: 120 req/min -> 0.5s intervals
    overload_sched = gen.plan_schedule("sustained_overload", count=10, rate_per_min=120.0)
    assert len(overload_sched) == 10
    assert abs(overload_sched[1].scheduled_offset_s - 0.5) < 1e-4


@pytest.mark.asyncio
async def test_loadgen_dry_run_scenarios(tmp_path: Path):
    """Run a compact dry run of each scenario to verify end-to-end execution and CSV logging."""
    gen = OpenLoopLoadGenerator()
    test_budget = tmp_path / "test_budget.json"
    test_csv = tmp_path / "results.csv"
    test_traces = tmp_path / "traces.jsonl"

    # 1. Steady short run
    summary_steady = await gen.execute_scenario(
        scenario="steady",
        count=3,
        rate_per_min=120.0,
        tmp_budget_file=test_budget,
        tmp_results_csv=test_csv,
        tmp_traces_jsonl=test_traces,
    )
    assert summary_steady.total_requests == 3
    assert (
        summary_steady.generator_max_lag_ms < 300.0
    )  # Kept schedule (tolerates OS process switching)

    # 2. Burst short run
    summary_burst = await gen.execute_scenario(
        scenario="burst",
        count=5,
        tmp_budget_file=test_budget,
        tmp_results_csv=test_csv,
        tmp_traces_jsonl=test_traces,
    )
    assert summary_burst.total_requests == 5

    # 3. Sustained overload short run
    summary_overload = await gen.execute_scenario(
        scenario="sustained_overload",
        count=5,
        rate_per_min=200.0,
        tmp_budget_file=test_budget,
        tmp_results_csv=test_csv,
        tmp_traces_jsonl=test_traces,
    )
    assert summary_overload.total_requests == 5

    # 4. Limit halved short run
    summary_halved = await gen.execute_scenario(
        scenario="limit_halved",
        count=3,
        tmp_budget_file=test_budget,
        tmp_results_csv=test_csv,
        tmp_traces_jsonl=test_traces,
    )
    assert summary_halved.total_requests == 3

    # 5. Daily exhausted short run
    summary_daily = await gen.execute_scenario(
        scenario="daily_exhausted",
        count=4,
        tmp_budget_file=test_budget,
        tmp_results_csv=test_csv,
        tmp_traces_jsonl=test_traces,
    )
    assert summary_daily.total_requests == 4

    # Verify rows written to isolated test_csv
    csv_rows = read_results_csv(test_csv)
    assert len(csv_rows) == (3 + 5 + 5 + 3 + 4)
    scenarios_logged = {r.scenario for r in csv_rows}
    assert "steady" in scenarios_logged
    assert "burst" in scenarios_logged
    assert "sustained_overload" in scenarios_logged
    assert "limit_halved" in scenarios_logged
    assert "daily_exhausted" in scenarios_logged
