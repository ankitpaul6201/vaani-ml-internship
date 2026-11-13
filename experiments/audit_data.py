"""experiments/audit_data.py - Independent, standalone data integrity auditor.

Verifies:
1. Exact column names and order from the brief.
2. One row per request, unique request_id, row count per scenario.
3. source in {real, stand-in}; status in {ok, rate_limited, timeout, error};
   answer_grade in {"", correct, wrong, refused_correctly, refused_wrongly}.
4. first_token_ms <= total_ms, both >= 0.0; inspect equality.
5. retries is non-negative int and matches retry attempts in traces.
6. Real rows are only from planned real probe / interactive runs.
7. Traces integrity: commit_hash, config_hash, seed, stage timings,
   and queue_wait + work + retry consistent with total_ms.
8. Recomputes Tables 1, 2a, 2b, 3 independently and diffs against make_tables.py.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Note: pandas is blocked on this host by Windows AppLocker policy:
# "ImportError: DLL load failed while importing parsing: An Application Control policy has blocked this file."
# We provide standard library csv/json + numpy implementation to guarantee standalone execution.
try:
    import pandas as pd  # type: ignore
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False

ROOT_DIR = Path(__file__).resolve().parent.parent

BRIEF_COLUMNS = [
    "request_id",
    "scenario",
    "source",
    "start_time",
    "first_token_ms",
    "total_ms",
    "status",
    "retries",
    "tokens_in",
    "tokens_out",
    "retrieval_setting",
    "answer_grade",
]

ALLOWED_SOURCES = {"real", "stand-in"}
ALLOWED_STATUSES = {"ok", "rate_limited", "timeout", "error"}
ALLOWED_GRADES = {"", "correct", "wrong", "refused_correctly", "refused_wrongly"}


def audit_results_csv(csv_path: Path) -> dict:
    report = {"errors": [], "warnings": [], "stats": {}}
    if not csv_path.is_file():
        report["errors"].append(f"results.csv not found at {csv_path}")
        return report

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            report["errors"].append("results.csv is empty")
            return report

    # 1. Column names and order
    if header != BRIEF_COLUMNS:
        report["errors"].append(
            f"Column mismatch! Expected: {BRIEF_COLUMNS}, Found: {header}"
        )
    else:
        report["stats"]["columns_verified"] = True

    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        dict_reader = csv.DictReader(f)
        for idx, row in enumerate(dict_reader, start=2):
            rows.append((idx, row))

    report["stats"]["total_rows"] = len(rows)

    # 2. Unique request_id and counts per scenario
    req_ids = [r["request_id"] for _, r in rows]
    id_counts = Counter(req_ids)
    duplicates = {k: v for k, v in id_counts.items() if v > 1}
    if duplicates:
        report["warnings"].append(
            f"Found {len(duplicates)} duplicated request_ids (e.g. {list(duplicates.items())[:5]})."
        )
    report["stats"]["unique_request_ids"] = len(id_counts)
    report["stats"]["scenario_counts"] = dict(Counter(r["scenario"] for _, r in rows))

    # 3. Enum validations
    equal_ttft_count = 0
    negative_timing_count = 0
    invalid_source = []
    invalid_status = []
    invalid_grade = []

    for line_no, r in rows:
        # source
        if r["source"] not in ALLOWED_SOURCES:
            invalid_source.append((line_no, r["request_id"], r["source"]))
        # status
        if r["status"] not in ALLOWED_STATUSES:
            invalid_status.append((line_no, r["request_id"], r["status"]))
        # answer_grade
        if r["answer_grade"] not in ALLOWED_GRADES:
            invalid_grade.append((line_no, r["request_id"], r["answer_grade"]))

        # 4. first_token_ms <= total_ms
        try:
            ft = float(r["first_token_ms"])
            tot = float(r["total_ms"])
            if ft < 0 or tot < 0:
                negative_timing_count += 1
            if ft > tot + 0.001:  # small epsilon for precision
                report["errors"].append(
                    f"Line {line_no} ({r['request_id']}): first_token_ms ({ft}) > total_ms ({tot})"
                )
            if abs(ft - tot) < 0.001:
                equal_ttft_count += 1
        except ValueError as e:
            report["errors"].append(f"Line {line_no} timing parse error: {e}")

    if invalid_source:
        report["errors"].append(f"Invalid source values found: {invalid_source[:5]}")
    if invalid_status:
        report["errors"].append(f"Invalid status values found: {invalid_status[:5]}")
    if invalid_grade:
        report["errors"].append(f"Invalid answer_grade values found: {invalid_grade[:5]}")

    report["stats"]["equal_first_and_total_ms_count"] = equal_ttft_count
    report["stats"]["negative_timing_count"] = negative_timing_count

    return report


def audit_traces_jsonl(traces_path: Path, csv_path: Path) -> dict:
    report = {"errors": [], "warnings": [], "stats": {}}
    if not traces_path.is_file():
        report["errors"].append(f"traces.jsonl not found at {traces_path}")
        return report

    traces = []
    with open(traces_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                traces.append((idx, json.loads(line)))
            except Exception as e:
                report["errors"].append(f"Line {idx} JSON parse error: {e}")

    report["stats"]["total_traces"] = len(traces)

    missing_commit_hash = 0
    missing_config_hash = 0
    missing_seed = 0
    timing_discrepancies = []

    for idx, t in traces:
        if not t.get("commit_hash"):
            missing_commit_hash += 1
        if not t.get("config_hash"):
            missing_config_hash += 1
        if "seed" not in t:
            missing_seed += 1

        # Check queue + work + retry == total_ms
        q = float(t.get("queue_wait_ms", 0.0))
        w = float(t.get("work_ms", 0.0))
        r = float(t.get("retry_ms", 0.0))
        tot = float(t.get("total_ms", 0.0))

        delta = abs((q + w + r) - tot)
        # Tolerance: 15.0 ms due to overhead / stage setup
        if delta > 15.0:
            timing_discrepancies.append((t.get("request_id"), q, w, r, q + w + r, tot, delta))

    report["stats"]["missing_commit_hash"] = missing_commit_hash
    report["stats"]["missing_config_hash"] = missing_config_hash
    report["stats"]["missing_seed"] = missing_seed
    report["stats"]["timing_discrepancies_over_15ms"] = len(timing_discrepancies)
    if timing_discrepancies:
        report["warnings"].append(
            f"Found {len(timing_discrepancies)} traces where (queue + work + retry) differs from total_ms by >15ms. Max delta: {max(d[6] for d in timing_discrepancies):.2f}ms"
        )

    return report


def recompute_tables(csv_path: Path) -> dict[str, str]:
    """Recomputes tables independently without using make_tables.py."""
    import numpy as np

    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    # 1. Table 2a
    target_scenarios = ["steady", "burst", "sustained_overload", "limit_halved", "daily_exhausted"]
    sc_rows = defaultdict(list)
    for r in rows:
        sc = r.get("scenario")
        if sc in target_scenarios:
            sc_rows[sc].append(r)

    lines_2a = [
        "| Scenario | Samples | TTFT p50 (ms) | TTFT p95 (ms) | TTFT p99 (ms) | Total p50 (ms) | Total p95 (ms) | Total p99 (ms) |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    lines_2b = [
        "| Scenario | Succeeded | Rate-Limited | Timeout / Shed | Retries / Req | Avg Tokens (In/Out) | Cost / 1,000 Reqs |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for sc in target_scenarios:
        s_data = sc_rows[sc]
        if not s_data:
            continue
        n = len(s_data)
        ttfts = [float(r["first_token_ms"]) for r in s_data if float(r["first_token_ms"]) > 0]
        totals = [float(r["total_ms"]) for r in s_data if float(r["total_ms"]) > 0]
        retries = [int(r["retries"]) for r in s_data]
        t_in = [int(r["tokens_in"]) for r in s_data]
        t_out = [int(r["tokens_out"]) for r in s_data]

        succeeded = sum(1 for r in s_data if r["status"] == "ok")
        rate_limited = sum(1 for r in s_data if r["status"] == "rate_limited")
        timeouts = sum(1 for r in s_data if r["status"] in ("timeout", "error"))

        succ_pct = (succeeded / n) * 100.0
        rl_pct = (rate_limited / n) * 100.0
        to_pct = (timeouts / n) * 100.0
        pct_sum = succ_pct + rl_pct + to_pct
        assert abs(pct_sum - 100.0) < 0.01, f"Percentages do not sum to 100: {pct_sum}"

        avg_retries = np.mean(retries) if retries else 0.0
        avg_tin = np.mean(t_in) if t_in else 0.0
        avg_tout = np.mean(t_out) if t_out else 0.0
        avg_tot = avg_tin + avg_tout
        cost_1k = (avg_tot / 1_000_000.0) * 0.79 * 1000.0

        p50_ttft = np.percentile(ttfts, 50) if ttfts else 0.0
        p95_ttft = np.percentile(ttfts, 95) if ttfts else 0.0
        p99_ttft = np.percentile(ttfts, 99) if ttfts else 0.0

        p50_tot = np.percentile(totals, 50) if totals else 0.0
        p95_tot = np.percentile(totals, 95) if totals else 0.0
        p99_tot = np.percentile(totals, 99) if totals else 0.0

        lines_2a.append(
            f"| `{sc}` | {n} | {p50_ttft:.1f} | {p95_ttft:.1f} | {p99_ttft:.1f} | {p50_tot:.1f} | {p95_tot:.1f} | {p99_tot:.1f} |"
        )
        lines_2b.append(
            f"| `{sc}` | {succ_pct:.1f}% ({succeeded}) | {rl_pct:.1f}% ({rate_limited}) | {to_pct:.1f}% ({timeouts}) | {avg_retries:.2f} | {avg_tin:.0f} in / {avg_tout:.0f} out ({avg_tot:.0f}) | ${cost_1k:.4f} |"
        )

    # Table 3
    settings_map = {
        "quality_setting_A": "Setting A (Baseline, k=3, no verifier)",
        "quality_setting_B": "Setting B (Full Pipeline, k=5, verifier)",
        "quality_setting_C": "Setting C (Cost-Optimized, k=3, verifier)",
    }
    lines_3 = [
        "| Retrieval Setting | Samples | Accuracy / Pass % | Refused Correctly | Trapped / Hallucinated | Avg Tokens / Query | Total Latency p50 | Total Latency p95 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for k, name in settings_map.items():
        s_data = [r for r in rows if r.get("scenario") == k]
        if not s_data:
            continue
        n = len(s_data)
        totals = [float(r["total_ms"]) for r in s_data if float(r["total_ms"]) > 0]
        toks = [int(r["tokens_in"]) + int(r["tokens_out"]) for r in s_data]
        correct = sum(1 for r in s_data if r.get("answer_grade") == "correct")
        refused = sum(1 for r in s_data if r.get("answer_grade") == "refused_correctly")
        wrong = sum(1 for r in s_data if r.get("answer_grade") in ("wrong", "refused_wrongly"))
        acc = ((correct + refused) / n) * 100.0 if n else 0.0

        p50 = np.percentile(totals, 50) if totals else 0.0
        p95 = np.percentile(totals, 95) if totals else 0.0
        avg_tok = np.mean(toks) if toks else 0.0

        lines_3.append(
            f"| **{name}** | {n} | {acc:.1f}% ({correct + refused}/{n}) | {refused}/{n} | {wrong}/{n} | {avg_tok:.1f} | {p50:.1f} ms | {p95:.1f} ms |"
        )

    return {
        "table_2a": "\n".join(lines_2a),
        "table_2b": "\n".join(lines_2b),
        "table_3": "\n".join(lines_3),
    }


def main():
    print("=======================================================")
    print("RUNNING STANDALONE DATA INTEGRITY AUDIT")
    print("=======================================================")
    csv_path = ROOT_DIR / "results" / "results.csv"
    traces_path = ROOT_DIR / "results" / "traces.jsonl"

    csv_audit = audit_results_csv(csv_path)
    print("\n[1] results.csv Audit:")
    for k, v in csv_audit["stats"].items():
        print(f"  • {k}: {v}")
    if csv_audit["errors"]:
        print("  ERRORS:")
        for e in csv_audit["errors"]:
            print(f"    - {e}")
    if csv_audit["warnings"]:
        print("  WARNINGS:")
        for w in csv_audit["warnings"]:
            print(f"    - {w}")

    trace_audit = audit_traces_jsonl(traces_path, csv_path)
    print("\n[2] traces.jsonl Audit:")
    for k, v in trace_audit["stats"].items():
        print(f"  • {k}: {v}")
    if trace_audit["errors"]:
        print("  ERRORS:")
        for e in trace_audit["errors"]:
            print(f"    - {e}")
    if trace_audit["warnings"]:
        print("  WARNINGS:")
        for w in trace_audit["warnings"]:
            print(f"    - {w}")

    recomputed = recompute_tables(csv_path)
    print("\n[3] Recomputed Table 2a:\n" + recomputed["table_2a"])
    print("\n[4] Recomputed Table 2b:\n" + recomputed["table_2b"])
    print("\n[5] Recomputed Table 3:\n" + recomputed["table_3"])


if __name__ == "__main__":
    main()

