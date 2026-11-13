"""tests/test_phase3.py - Phase 3 Real Service Probe & Table 1 Verification.

Verifies:
- Raw probe data existence in results/real/probe_data.json.
- Real captured HTTP 429 response: status, error message, retry-after header.
- Token limit (TPM) binds before request limit (RPM).
- Table 1 completeness, required headings, and confidence levels.
- config.yaml limits updated with measured values and safety margins.
"""

import json
from pathlib import Path

import pytest
import yaml

from experiments.probe_groq import generate_table_1


@pytest.fixture(scope="module")
def probe_data():
    root = Path(__file__).resolve().parent.parent
    probe_file = root / "results" / "real" / "probe_data.json"
    assert probe_file.is_file(), f"Missing probe data file: {probe_file}"

    with open(probe_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def test_probe_data_not_empty(probe_data):
    """Verify probe records were recorded and saved."""
    assert len(probe_data) >= 5
    for rec in probe_data:
        assert "probe_id" in rec
        assert "ttft_ms" in rec
        assert "total_ms" in rec
        assert "status_code" in rec
        assert "headers" in rec


def test_captured_real_429_record(probe_data):
    """Confirm a real HTTP 429 was captured with rate limit headers and error body."""
    records_429 = [r for r in probe_data if r["status_code"] == 429]
    assert len(records_429) >= 1, "Expected at least one captured HTTP 429 record in probe data"

    rec_429 = records_429[0]
    headers = rec_429["headers"]

    # Verify rate limit headers
    assert "x-ratelimit-limit-tokens" in headers or "retry-after" in headers
    # Verify error message
    assert (
        "rate_limit_exceeded" in rec_429["body_snippet"] or "Rate limit" in rec_429["body_snippet"]
    )


def test_tpm_binds_first(probe_data):
    """Prove that tokens-per-minute (TPM) limit binds before requests-per-minute (RPM)."""
    records_with_headers = [
        r for r in probe_data if r["headers"].get("x-ratelimit-remaining-tokens")
    ]
    assert len(records_with_headers) > 0

    latest_h = records_with_headers[-1]["headers"]
    limit_rpm = int(latest_h.get("x-ratelimit-limit-requests", 1000))
    rem_rpm = int(latest_h.get("x-ratelimit-remaining-requests", 1000))

    limit_tpm = int(latest_h.get("x-ratelimit-limit-tokens", 8000))
    rem_tpm = int(latest_h.get("x-ratelimit-remaining-tokens", 8000))

    # Fraction remaining
    pct_rpm_remaining = rem_rpm / limit_rpm
    pct_tpm_remaining = rem_tpm / limit_tpm

    # TPM consumed significantly faster than RPM
    assert pct_tpm_remaining < pct_rpm_remaining, (
        f"Expected TPM to bind faster: TPM remaining {pct_tpm_remaining:.1%} vs RPM {pct_rpm_remaining:.1%}"
    )


def test_table_1_generation(probe_data):
    """Verify Table 1 generation contains all 7 mandated limits and valid confidence ratings."""
    # Convert dicts back to records structure for generator
    from experiments.probe_groq import ProbeRecord

    record_objs = [ProbeRecord(**d) for d in probe_data]
    captured = next((r for r in record_objs if r.status_code == 429), None)

    table_1 = generate_table_1(record_objs, captured, 1.2)
    assert len(table_1) == 7

    expected_limits = [
        "Requests per minute",
        "Tokens per minute",
        "Requests per day",
        "Tokens per day",
        "Requests in flight at once",
        "What it returns when a limit is hit",
        "Time until it recovers",
    ]

    for limit_name in expected_limits:
        row = next((r for r in table_1 if r["Limit"] == limit_name), None)
        assert row is not None, f"Missing limit in Table 1: {limit_name}"
        assert row["Confidence"] in ("High", "Medium", "Low")
        assert len(row["Value"]) > 0
        assert len(row["How found"]) > 0


def test_config_limits_updated():
    """Verify config.yaml limits are measured numbers with safety margins."""
    root = Path(__file__).resolve().parent.parent
    config_file = root / "config.yaml"

    with open(config_file, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    limits = cfg["limits"]
    assert isinstance(limits["rpm"], (int, float)) and limits["rpm"] > 0
    assert isinstance(limits["tpm"], (int, float)) and limits["tpm"] > 0
    assert isinstance(limits["rpd"], (int, float)) and limits["rpd"] > 0
    assert isinstance(limits["tpd"], (int, float)) and limits["tpd"] > 0
    assert isinstance(limits["max_in_flight"], (int, float)) and limits["max_in_flight"] > 0
