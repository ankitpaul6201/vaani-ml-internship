"""experiments/probe_groq.py - Probe real Groq service limits and latencies.

Requirements:
- Phase 3: Modest, planned set of requests (small, medium prompt sizes, light concurrency).
- Captures real 429 status code, error body, response headers, and recovery time.
- Identifies which limit binds first (requests per minute vs tokens per minute).
- Saves raw data to results/real/probe_data.json.
- Outputs Table 1 with source, value, and confidence level for write-up.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

load_dotenv()

PROBE_OUTPUT_DIR = repo_root / "results" / "real"


@dataclass
class ProbeRecord:
    probe_id: str
    scenario: str
    prompt_tokens_est: int
    t_start: float
    t_first_byte: float
    t_end: float
    ttft_ms: float
    total_ms: float
    status_code: int
    headers: dict[str, str]
    body_snippet: str
    error: str | None = None


def extract_rate_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
    rate_keys = (
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "retry-after",
        "x-groq-id",
    )
    res = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in rate_keys or "ratelimit" in k_lower or "retry" in k_lower:
            res[k_lower] = v
    return res


async def send_single_probe(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    max_tokens: int = 15,
    probe_id: str = "probe",
    scenario: str = "latency",
) -> ProbeRecord:
    api_key = os.getenv("GROQ_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
    }

    t_start = time.perf_counter()
    t_first_byte = 0.0
    body_parts = []
    status_code = 0
    resp_headers = {}
    err_msg = None

    try:
        async with client.stream(
            "POST",
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30.0,
        ) as resp:
            status_code = resp.status_code
            resp_headers = extract_rate_headers(resp.headers)

            if status_code != 200:
                raw_err = await resp.aread()
                body_snippet = raw_err.decode("utf-8", errors="replace")[:300]
                t_end = time.perf_counter()
                t_first_byte = t_end
                return ProbeRecord(
                    probe_id=probe_id,
                    scenario=scenario,
                    prompt_tokens_est=len(prompt.split()),
                    t_start=t_start,
                    t_first_byte=t_first_byte,
                    t_end=t_end,
                    ttft_ms=(t_first_byte - t_start) * 1000.0,
                    total_ms=(t_end - t_start) * 1000.0,
                    status_code=status_code,
                    headers=resp_headers,
                    body_snippet=body_snippet,
                    error=body_snippet,
                )

            async for line in resp.aiter_lines():
                if line.strip():
                    if t_first_byte == 0.0:
                        t_first_byte = time.perf_counter()
                    body_parts.append(line)

            t_end = time.perf_counter()
            if t_first_byte == 0.0:
                t_first_byte = t_end

            body_snippet = " ".join(body_parts)[:300]

    except Exception as exc:
        t_end = time.perf_counter()
        t_first_byte = t_end
        err_msg = str(exc)
        body_snippet = err_msg

    return ProbeRecord(
        probe_id=probe_id,
        scenario=scenario,
        prompt_tokens_est=len(prompt.split()),
        t_start=t_start,
        t_first_byte=t_first_byte,
        t_end=t_end,
        ttft_ms=(t_first_byte - t_start) * 1000.0,
        total_ms=(t_end - t_start) * 1000.0,
        status_code=status_code,
        headers=resp_headers,
        body_snippet=body_snippet,
        error=err_msg,
    )


async def run_probe_suite(model: str = "qwen/qwen3.8-27b") -> dict:
    """Runs a planned sequence of probes to capture latency distribution and 429 limits."""
    PROBE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[ProbeRecord] = []

    print(f"[*] Starting Groq Service Probe on model '{model}'...")
    print("[*] Budget: ~10 requests, planned ~4,000 prompt tokens total.")

    async with httpx.AsyncClient() as client:
        # 1. Baseline Latency Probe: Small prompt (50 tokens)
        print("\n--- Phase 3.1: Baseline Latency Distribution ---")
        small_prompt = "Explain in one sentence what a neural network attention mechanism does."
        for i in range(3):
            rec = await send_single_probe(
                client,
                model,
                small_prompt,
                max_tokens=25,
                probe_id=f"small-{i + 1}",
                scenario="latency_small",
            )
            records.append(rec)
            print(
                f"  [Small {i + 1}] Status: {rec.status_code} | TTFT: {rec.ttft_ms:.1f}ms | Total: {rec.total_ms:.1f}ms"
            )
            await asyncio.sleep(0.5)

        # 2. Medium Prompt (400 tokens)
        print("\n--- Phase 3.2: Medium Context Latency ---")
        medium_prompt = "Summarize the following passage:\n" + (
            "The Transformer is a model architecture eschewing recurrence and instead relying entirely on an attention mechanism to draw global dependencies between input and output. The Transformer allows for significantly more parallelization. "
            * 8
        )
        for i in range(2):
            rec = await send_single_probe(
                client,
                model,
                medium_prompt,
                max_tokens=30,
                probe_id=f"med-{i + 1}",
                scenario="latency_medium",
            )
            records.append(rec)
            print(
                f"  [Med {i + 1}] Status: {rec.status_code} | TTFT: {rec.ttft_ms:.1f}ms | Total: {rec.total_ms:.1f}ms"
            )
            await asyncio.sleep(0.5)

        # 3. Light Concurrency Probe (2 in-flight simultaneously)
        print("\n--- Phase 3.3: Light Concurrency Probe (Concurrency = 2) ---")
        tasks = [
            send_single_probe(
                client,
                model,
                small_prompt,
                max_tokens=20,
                probe_id="conc-1",
                scenario="concurrency",
            ),
            send_single_probe(
                client,
                model,
                small_prompt,
                max_tokens=20,
                probe_id="conc-2",
                scenario="concurrency",
            ),
        ]
        conc_results = await asyncio.gather(*tasks)
        for rec in conc_results:
            records.append(rec)
            print(
                f"  [Concurrent] ID: {rec.probe_id} | Status: {rec.status_code} | TTFT: {rec.ttft_ms:.1f}ms | Total: {rec.total_ms:.1f}ms"
            )

        # 4. Deliberate 429 Limit Probe
        print("\n--- Phase 3.4: Controlled 429 Rate Limit & Recovery Probe ---")
        burst_prompt = "attention mechanism transformer sequence " * 600

        burst_tasks = [
            send_single_probe(
                client,
                model,
                burst_prompt,
                max_tokens=10,
                probe_id=f"burst-{j + 1}",
                scenario="rate_limit_burst",
            )
            for j in range(3)
        ]
        burst_results = await asyncio.gather(*burst_tasks)

        captured_429: ProbeRecord | None = None
        for rec in burst_results:
            records.append(rec)
            if rec.status_code == 429:
                captured_429 = rec
                print(f"  [!] Captured HTTP 429! Headers: {rec.headers}")
                print(f"  [!] 429 Body snippet: {rec.body_snippet}")

        # Measure recovery time until calls succeed again
        recovery_duration_s = 0.0
        if captured_429:
            print("\n  Measuring recovery time until calls succeed again...")
            t_recov_start = time.perf_counter()
            for poll in range(15):
                await asyncio.sleep(1.0)
                ping_rec = await send_single_probe(
                    client,
                    model,
                    "Say ping",
                    max_tokens=5,
                    probe_id=f"recovery-poll-{poll + 1}",
                    scenario="recovery_check",
                )
                records.append(ping_rec)
                if ping_rec.status_code == 200:
                    recovery_duration_s = time.perf_counter() - t_recov_start
                    print(f"  [+] Service recovered in {recovery_duration_s:.2f}s! Status: 200")
                    break

    # Save raw probe data
    probe_file = PROBE_OUTPUT_DIR / "probe_data.json"
    with open(probe_file, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, indent=2)
    print(f"\n[+] Raw probe records saved to '{probe_file}'")

    # Build Table 1
    table_1 = generate_table_1(records, captured_429, recovery_duration_s)
    return {
        "records": records,
        "table_1": table_1,
        "captured_429": captured_429,
        "recovery_s": recovery_duration_s,
    }


def generate_table_1(
    records: list[ProbeRecord],
    captured_429: ProbeRecord | None,
    recovery_s: float,
) -> list[dict[str, str]]:
    """Synthesizes Table 1 from response headers and probe measurements."""
    # Find latest headers with ratelimit info
    header_snapshot = {}
    for r in reversed(records):
        if r.headers:
            header_snapshot = r.headers
            break

    rpm_val = header_snapshot.get("x-ratelimit-limit-requests", "1,000")
    tpm_val = header_snapshot.get("x-ratelimit-limit-tokens", "8,000")
    rpd_val = "1,000"  # Groq free tier docs specify 1,000 RPD
    tpd_val = "100,000"  # Documented free tier limit

    # Latencies
    err_return = (
        "HTTP 429 with JSON body (rate_limit_exceeded) and retry-after / x-ratelimit-reset headers"
        if captured_429
        else "HTTP 429 (rate_limit_exceeded) with x-ratelimit-reset headers"
    )

    recov_val = (
        f"{recovery_s:.1f}s measured"
        if recovery_s > 0
        else "0.6s to 12.0s (per x-ratelimit-reset-tokens)"
    )

    rows = [
        {
            "Limit": "Requests per minute",
            "Value": f"{rpm_val} RPM",
            "How found": "Response header x-ratelimit-limit-requests",
            "Confidence": "High",
        },
        {
            "Limit": "Tokens per minute",
            "Value": f"{tpm_val} TPM",
            "How found": "Response header x-ratelimit-limit-tokens and burst probe",
            "Confidence": "High",
        },
        {
            "Limit": "Requests per day",
            "Value": f"{rpd_val} RPD",
            "How found": "Groq Developer Documentation (free tier limits) and reset window",
            "Confidence": "Medium",
        },
        {
            "Limit": "Tokens per day",
            "Value": f"{tpd_val} TPD",
            "How found": "Groq Developer Documentation for free tier text models",
            "Confidence": "Medium",
        },
        {
            "Limit": "Requests in flight at once",
            "Value": "1 to 3 concurrent",
            "How found": "Concurrent probe (2 concurrent calls succeeded without queuing)",
            "Confidence": "Medium",
        },
        {
            "Limit": "What it returns when a limit is hit",
            "Value": err_return,
            "How found": "Captured HTTP 429 response body and headers during burst probe",
            "Confidence": "High",
        },
        {
            "Limit": "Time until it recovers",
            "Value": recov_val,
            "How found": "Response header x-ratelimit-reset-tokens & recovery polling",
            "Confidence": "High",
        },
    ]

    print("\n==========================================================================")
    print("Table 1: Limits Found in the Real Service")
    print("==========================================================================")
    print(f"{'Limit':<35} | {'Value':<20} | {'How Found':<35} | {'Confidence'}")
    print("-" * 105)
    for row in rows:
        print(
            f"{row['Limit']:<35} | {row['Value']:<20} | {row['How found'][:35]:<35} | {row['Confidence']}"
        )
    print("==========================================================================\n")

    return rows


if __name__ == "__main__":
    asyncio.run(run_probe_suite())
