"""standin/calibrate.py - Fit stand-in server parameters from real Groq probe data.

Requirements:
- Reads captured real probe records from results/real/probe_data.json.
- Fits empirical parameters:
  - base_ttft_mean_ms, base_ttft_std_ms (log-normal distribution)
  - ms_per_token generation rate
  - concurrency slowdown factor
- Outputs calibration parameters to standin/calibration.json.
- Generates side-by-side comparison of real vs calibrated model percentiles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

repo_root = Path(__file__).resolve().parent.parent
PROBE_DATA_FILE = repo_root / "results" / "real" / "probe_data.json"
CALIBRATION_FILE = repo_root / "standin" / "calibration.json"


def calibrate_from_probe(probe_path: Path | None = None) -> dict[str, Any]:
    """Fits statistical parameters from real probe data."""
    path = probe_path or PROBE_DATA_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing probe data at '{path}'. Run experiments/probe_groq.py first."
        )

    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)

    successful = [r for r in records if r["status_code"] == 200]
    if not successful:
        raise ValueError("No successful (200 OK) records found in probe data.")

    ttfts = np.array([r["ttft_ms"] for r in successful])
    totals = np.array([r["total_ms"] for r in successful])

    # Fit log-normal to TTFT: log(TTFT) ~ Normal(mu, sigma)
    log_ttfts = np.log(np.maximum(1.0, ttfts))
    mu = float(np.mean(log_ttfts))
    sigma = float(np.std(log_ttfts))
    if sigma < 0.05:
        sigma = 0.15

    # Estimate ms per token from generation phase: (total_ms - ttft_ms)
    gen_times = np.maximum(1.0, totals - ttfts)
    # Average tokens generated was ~15-25 tokens across probes
    ms_per_token = float(np.median(gen_times) / 15.0)
    ms_per_token = max(1.0, min(10.0, ms_per_token))

    # Concurrency factor
    conc_records = [r for r in records if r.get("scenario") == "concurrency"]
    if len(conc_records) >= 2:
        conc_ttfts = [r["ttft_ms"] for r in conc_records]
        # Ratio of slowest concurrent call to fastest
        concurrency_penalty = max(0.1, min(0.6, (max(conc_ttfts) - min(conc_ttfts)) / max(ttfts)))
    else:
        concurrency_penalty = 0.25

    # Real percentiles
    real_p50 = float(np.percentile(ttfts, 50))
    real_p90 = float(np.percentile(ttfts, 90))
    real_p95 = float(np.percentile(ttfts, 95))

    # Synthetic sample from calibrated distribution
    np.random.seed(42)
    sim_ttfts = np.random.lognormal(mu, sigma, size=1000)
    sim_p50 = float(np.percentile(sim_ttfts, 50))
    sim_p90 = float(np.percentile(sim_ttfts, 90))
    sim_p95 = float(np.percentile(sim_ttfts, 95))

    calib_params = {
        "fitted_model": "qwen/qwen3.8-27b",
        "sample_size": len(successful),
        "lognormal_mu": round(mu, 4),
        "lognormal_sigma": round(sigma, 4),
        "base_ttft_p50_ms": round(real_p50, 1),
        "base_ttft_p95_ms": round(real_p95, 1),
        "ms_per_token": round(ms_per_token, 2),
        "concurrency_penalty": round(concurrency_penalty, 2),
        "rpm_limit": 1000,
        "tpm_limit": 8000,
        "rpd_limit": 1000,
        "tpd_limit": 100000,
        "comparison": {
            "p50_ms": {
                "real": round(real_p50, 1),
                "standin_sim": round(sim_p50, 1),
                "diff_pct": round(abs(sim_p50 - real_p50) / real_p50 * 100, 1),
            },
            "p90_ms": {
                "real": round(real_p90, 1),
                "standin_sim": round(sim_p90, 1),
                "diff_pct": round(abs(sim_p90 - real_p90) / real_p90 * 100, 1),
            },
            "p95_ms": {
                "real": round(real_p95, 1),
                "standin_sim": round(sim_p95, 1),
                "diff_pct": round(abs(sim_p95 - real_p95) / real_p95 * 100, 1),
            },
        },
    }

    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
        json.dump(calib_params, f, indent=2)

    print("==========================================================================")
    print(" Stand-in Model Calibration Report (from Groq probe data)")
    print("==========================================================================")
    print(f" Sample records fitted: {len(successful)}")
    print(f" Fitted Lognormal: mu = {mu:.3f}, sigma = {sigma:.3f}")
    print(f" Generation speed: {ms_per_token:.2f} ms/token (~{1000 / ms_per_token:.0f} tokens/s)")
    print(f" Concurrency penalty factor: {concurrency_penalty:.2f} per concurrent request")
    print("\n Real vs Stand-In Percentiles (Exit Gate: within 20%):")
    print(
        f"   TTFT p50: Real {real_p50:.1f}ms vs Sim {sim_p50:.1f}ms (Diff: {calib_params['comparison']['p50_ms']['diff_pct']}%)"
    )
    print(
        f"   TTFT p90: Real {real_p90:.1f}ms vs Sim {sim_p90:.1f}ms (Diff: {calib_params['comparison']['p90_ms']['diff_pct']}%)"
    )
    print(
        f"   TTFT p95: Real {real_p95:.1f}ms vs Sim {sim_p95:.1f}ms (Diff: {calib_params['comparison']['p95_ms']['diff_pct']}%)"
    )
    print(f" Saved calibrated parameters to '{CALIBRATION_FILE}'")
    print("==========================================================================")

    return calib_params


if __name__ == "__main__":
    calibrate_from_probe()
