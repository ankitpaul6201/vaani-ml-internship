"""src/trace.py - Schemas and serialization for results.csv and traces.jsonl.

Requirements:
- FR-LOG-01: Exactly the 12 columns mandated by the brief for results.csv.
- FR-LOG-02: Trace records with stage timings and code commit hash.
- FR-LOG-03: Monotonic clock for durations, wall clock for start_time.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Strict schema definitions from the assignment brief & AGENTS.md
RESULTS_CSV_COLUMNS = [
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

AllowedSource = Literal["real", "stand-in"]
AllowedStatus = Literal["ok", "rate_limited", "timeout", "error"]
AllowedAnswerGrade = Literal["correct", "wrong", "refused_correctly", "refused_wrongly", ""]


@dataclass
class ResultRow:
    """Represents exactly one row in results.csv."""

    request_id: str
    scenario: str
    source: AllowedSource
    start_time: str
    first_token_ms: float
    total_ms: float
    status: AllowedStatus
    retries: int
    tokens_in: int
    tokens_out: int
    retrieval_setting: str
    answer_grade: AllowedAnswerGrade = ""

    def validate(self) -> None:
        """Validates all fields against the specification."""
        if self.source not in ("real", "stand-in"):
            raise ValueError(f"Invalid source: {self.source}. Must be 'real' or 'stand-in'.")
        if self.status not in ("ok", "rate_limited", "timeout", "error"):
            raise ValueError(
                f"Invalid status: {self.status}. Must be 'ok', 'rate_limited', 'timeout', or 'error'."
            )
        if self.answer_grade not in (
            "correct",
            "wrong",
            "refused_correctly",
            "refused_wrongly",
            "",
        ):
            raise ValueError(
                f"Invalid answer_grade: '{self.answer_grade}'. "
                "Must be 'correct', 'wrong', 'refused_correctly', 'refused_wrongly', or empty string."
            )
        if self.retries < 0:
            raise ValueError(f"Retries cannot be negative: {self.retries}")
        if self.tokens_in < 0 or self.tokens_out < 0:
            raise ValueError("Token counts cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class StageRecord:
    name: str
    t_enqueue: float  # monotonic seconds
    t_start: float  # monotonic seconds
    t_first_byte: float  # monotonic seconds
    t_end: float  # monotonic seconds
    attempt: int
    status: str


@dataclass
class TraceRecord:
    """Rich per-request trace for traces.jsonl."""

    request_id: str
    scenario: str
    source: AllowedSource
    start_time: str  # ISO timestamp (wall-clock)
    deadline: float  # monotonic seconds
    commit_hash: str
    config_hash: str
    retrieval_setting: str
    degradation_level: int
    outcome: str  # e.g., 'succeeded', 'given_up', 'rate_limited', 'failed'
    stages: list[StageRecord] = field(default_factory=list)
    queue_wait_ms: float = 0.0
    work_ms: float = 0.0
    retry_ms: float = 0.0
    model_first_byte_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    first_token_ms: float = 0.0
    total_ms: float = 0.0
    seed: int = 42
    answer_grade: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def init_results_csv(file_path: Path) -> None:
    """Initializes results.csv with headers if it does not exist."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists() or file_path.stat().st_size == 0:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(RESULTS_CSV_COLUMNS)


def append_result_row(file_path: Path, row: ResultRow) -> None:
    """Appends one validated row to results.csv incrementally."""
    row.validate()
    file_exists = file_path.exists() and file_path.stat().st_size > 0
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row.to_dict())


def append_trace_record(file_path: Path, trace: TraceRecord) -> None:
    """Appends one trace record to traces.jsonl incrementally."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(trace.to_json() + "\n")


def read_results_csv(file_path: Path) -> list[ResultRow]:
    """Reads all rows from results.csv into validated ResultRow objects."""
    if not file_path.exists():
        return []
    rows: list[ResultRow] = []
    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                ResultRow(
                    request_id=r["request_id"],
                    scenario=r["scenario"],
                    source=r["source"],  # type: ignore
                    start_time=r["start_time"],
                    first_token_ms=float(r["first_token_ms"]),
                    total_ms=float(r["total_ms"]),
                    status=r["status"],  # type: ignore
                    retries=int(r["retries"]),
                    tokens_in=int(r["tokens_in"]),
                    tokens_out=int(r["tokens_out"]),
                    retrieval_setting=r["retrieval_setting"],
                    answer_grade=r.get("answer_grade", ""),  # type: ignore
                )
            )
    return rows


def read_traces_jsonl(file_path: Path) -> list[dict]:
    """Reads all traces from traces.jsonl into dictionaries."""
    if not file_path.exists():
        return []
    traces: list[dict] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                traces.append(json.loads(line))
    return traces
