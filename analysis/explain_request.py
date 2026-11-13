"""analysis/explain_request.py - Explains where the time went for any request ID.

Usage:
    python analysis/explain_request.py --request-id <ID> [--json]

Extracts traces from results/traces.jsonl (or results/results.csv) and prints:
- Waiting vs working vs retrying time breakdown
- Before vs after first token split (TTFT vs generation streaming)
- Stage-by-stage latency (S1 query, S2 retrieve, S3 decide, S4 draft, S5 verify, S6 redraft)
- Root-cause bottleneck diagnosis (queue saturation, model generation, citation failure, retry backoff)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.trace import read_results_csv, read_traces_jsonl


def explain_request(request_id: str, as_json: bool = False) -> None:
    traces_path = ROOT_DIR / "results" / "traces.jsonl"
    csv_path = ROOT_DIR / "results" / "results.csv"

    matched_trace: dict[str, Any] | None = None
    if traces_path.is_file():
        traces = read_traces_jsonl(traces_path)
        for t in reversed(traces):  # Most recent first
            if t.get("request_id") == request_id:
                matched_trace = t
                break

    if not matched_trace:
        # Fall back to CSV
        if csv_path.is_file():
            csv_rows = read_results_csv(csv_path)
            for r in reversed(csv_rows):
                if r.get("request_id") == request_id:
                    matched_trace = {
                        "request_id": r["request_id"],
                        "scenario": r.get("scenario", "unknown"),
                        "total_ms": float(r.get("total_ms", 0.0)),
                        "first_token_ms": float(r.get("first_token_ms", 0.0)),
                        "tokens_in": int(r.get("tokens_in", 0)),
                        "tokens_out": int(r.get("tokens_out", 0)),
                        "status": r.get("status", "unknown"),
                        "outcome": r.get("outcome", "unknown"),
                        "stages": [],
                    }
                    break

    if not matched_trace:
        print(
            f"Error: Request ID '{request_id}' not found in traces or results.csv", file=sys.stderr
        )
        sys.exit(1)

    if as_json:
        print(json.dumps(matched_trace, indent=2))
        return

    # Formatted terminal explanation
    total_ms = float(matched_trace.get("total_ms", 0.0))
    first_token_ms = float(matched_trace.get("first_token_ms", 0.0))
    queue_wait_ms = float(matched_trace.get("queue_wait_ms", 0.0))
    work_ms = float(matched_trace.get("work_ms", 0.0))
    retry_ms = float(matched_trace.get("retry_ms", 0.0))
    tokens_in = int(matched_trace.get("tokens_in", 0))
    tokens_out = int(matched_trace.get("tokens_out", 0))
    scenario = matched_trace.get("scenario", "unknown")
    outcome = matched_trace.get("outcome", "unknown")
    retrieval_setting = matched_trace.get("retrieval_setting", "B")
    degradation_lvl = matched_trace.get("degradation_level", 0)

    # Calculate generation streaming time after first token
    streaming_ms = max(0.0, total_ms - first_token_ms) if first_token_ms > 0 else 0.0

    print("=" * 72)
    print(f"TIMELINE EXPLANATION: {request_id}")
    print("=" * 72)
    print(f"Scenario:           {scenario}")
    print(f"Outcome:            {outcome}")
    print(f"Retrieval Setting:  {retrieval_setting}")
    print(f"Degradation Level:  L{degradation_lvl}")
    print(
        f"Tokens:             {tokens_in} prompt in / {tokens_out} completion out (Total: {tokens_in + tokens_out})"
    )
    print("-" * 72)

    # 1. High-Level Time Breakdown
    print("TIME BREAKDOWN (WAITING vs WORKING vs RETRYING):")
    if total_ms > 0:
        q_pct = (queue_wait_ms / total_ms) * 100
        w_pct = (work_ms / total_ms) * 100
        r_pct = (retry_ms / total_ms) * 100
        print(
            f"  • Queue Wait:     {queue_wait_ms:8.1f} ms  ({q_pct:5.1f}%) [Admission control & rate bucket wait]"
        )
        print(
            f"  • Execution Work: {work_ms:8.1f} ms  ({w_pct:5.1f}%) [Retrieval, LLM inference, verification]"
        )
        print(f"  • Retry Delays:   {retry_ms:8.1f} ms  ({r_pct:5.1f}%) [Backoff & jitter delays]")
        print("  -------------------------------------")
        print(f"  • Total Duration: {total_ms:8.1f} ms  (100.0%)")
    else:
        print(f"  Total Duration: {total_ms:.1f} ms")

    print("-" * 72)
    # 2. First Token Split
    print("FIRST TOKEN SPLIT (TTFT vs STREAMING):")
    if total_ms > 0 and first_token_ms > 0:
        ttft_pct = (first_token_ms / total_ms) * 100
        stream_pct = (streaming_ms / total_ms) * 100
        print(f"  • Time to First Token (TTFT): {first_token_ms:8.1f} ms  ({ttft_pct:5.1f}%)")
        print(f"  • Token Generation Stream:   {streaming_ms:8.1f} ms  ({stream_pct:5.1f}%)")
    else:
        print(f"  • TTFT: {first_token_ms:.1f} ms | Total: {total_ms:.1f} ms")

    print("-" * 72)
    # 3. Stage-by-Stage Latency
    stages = matched_trace.get("stages", [])
    if stages:
        print("STAGE-BY-STAGE BREAKDOWN:")
        for st in stages:
            st_name = st.get("stage_name", "unknown")
            st_dur = float(st.get("duration_ms", 0.0))
            st_status = st.get("status", "ok")
            st_tin = st.get("tokens_in", 0)
            st_tout = st.get("tokens_out", 0)
            st_pct = (st_dur / total_ms * 100) if total_ms > 0 else 0.0
            tok_info = f"({st_tin}in/{st_tout}out)" if (st_tin or st_tout) else ""
            print(
                f"  [{st_name:18s}] {st_dur:7.1f} ms ({st_pct:5.1f}%) | status: {st_status:4s} {tok_info}"
            )

    print("-" * 72)
    # 4. Bottleneck Analysis & Diagnosis
    print("BOTTLENECK DIAGNOSIS:")
    if degradation_lvl == 4:
        print(
            "  -> L4 Daily Budget Exhausted: Immediate local extractive reply; zero token latency."
        )
    elif retry_ms > (total_ms * 0.4):
        print(
            f"  -> RETRY STORM / RATE-LIMIT: {retry_ms:.1f}ms spent in backoff waiting for provider quota."
        )
    elif queue_wait_ms > (total_ms * 0.5):
        print(
            f"  -> QUEUE SATURATION: Request spent {queue_wait_ms:.1f}ms waiting in priority queue for token refill."
        )
    elif streaming_ms > (total_ms * 0.5):
        print(
            f"  -> GENERATION BOUND: Model generation took {streaming_ms:.1f}ms ({tokens_out} tokens)."
        )
    else:
        print(
            "  -> BALANCED PIPELINE: Normal execution through retrieval, draft, and citation verification."
        )
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="Explain where the time went for a request.")
    parser.add_argument("--request-id", "-r", required=True, help="Request ID to explain")
    parser.add_argument("--json", action="store_true", help="Output raw trace JSON")
    args = parser.parse_args()

    explain_request(args.request_id, as_json=args.json)


if __name__ == "__main__":
    main()
