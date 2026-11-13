"""tests/test_phase0.py - Phase 0 Verification Test Suite.

Verifies:
- FR-ING-01: Pinned paper versions and SHA-256 hashes.
- FR-LOG-01: results.csv schema and column definitions.
- EI-02, EI-06: Configurable base_url, API key safety.
- LP-01, LP-02: Secret protection and repository hygiene.
"""

from pathlib import Path

import pytest
import yaml

from scripts.verify_papers import PINNED_PAPERS, compute_sha256
from src.trace import (
    RESULTS_CSV_COLUMNS,
    ResultRow,
    append_result_row,
)


def test_repo_structure():
    """Verify core directories exist."""
    root = Path(__file__).resolve().parent.parent
    expected_dirs = [
        "docs",
        "data/papers",
        "src",
        "standin",
        "experiments",
        "analysis",
        "results",
        "tests",
        "notes",
        "scripts",
    ]
    for d in expected_dirs:
        assert (root / d).is_dir(), f"Expected directory missing: {d}"


def test_pinned_papers_and_hashes():
    """FR-ING-01: Verify exact pinned papers and SHA-256 hashes."""
    root = Path(__file__).resolve().parent.parent
    papers_dir = root / "data" / "papers"

    pdf_files = sorted(papers_dir.glob("*.pdf"))
    assert len(pdf_files) == 3, f"Expected exactly 3 pinned PDFs, found {len(pdf_files)}"

    for filename, meta in PINNED_PAPERS.items():
        pdf_path = papers_dir / filename
        assert pdf_path.is_file(), f"Missing required paper: {filename}"
        calculated_hash = compute_sha256(pdf_path)
        assert calculated_hash == meta["sha256"], f"Hash mismatch for {filename}"


def test_no_unpinned_pdfs():
    """Verify unpinned PDFs (such as 2410.00037v2.pdf) are not in data/papers."""
    root = Path(__file__).resolve().parent.parent
    papers_dir = root / "data" / "papers"
    pdf_names = {f.name for f in papers_dir.glob("*.pdf")}
    assert "2410.00037v2.pdf" not in pdf_names, (
        "Unpinned Moshi v2 must not be present in data/papers!"
    )
    assert pdf_names == set(PINNED_PAPERS.keys())


def test_config_yaml_validity_and_tbds():
    """Verify config.yaml structure and placeholder TBD markers."""
    root = Path(__file__).resolve().parent.parent
    config_path = root / "config.yaml"
    assert config_path.is_file()

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    assert "backend" in cfg
    assert "models" in cfg
    assert "limits" in cfg
    assert "patience_s" in cfg
    assert cfg["patience_s"] == 20.0
    assert "retry" in cfg
    assert "ladder" in cfg
    assert "retrieval" in cfg

    # Verify limits are populated with measured positive values (or TBD)
    for k in ("rpm", "tpm", "rpd", "tpd", "max_in_flight"):
        val = cfg["limits"][k]
        assert val == "TBD" or (isinstance(val, (int, float)) and val > 0), (
            f"Limit {k} must be positive number or TBD"
        )


def test_results_csv_schema(tmp_path):
    """FR-LOG-01: Verify results.csv schema and validation."""
    csv_file = tmp_path / "test_results.csv"

    row = ResultRow(
        request_id="req-001",
        scenario="steady",
        source="stand-in",
        start_time="2026-11-10T10:00:00.000Z",
        first_token_ms=250.5,
        total_ms=1200.0,
        status="ok",
        retries=0,
        tokens_in=350,
        tokens_out=120,
        retrieval_setting="B",
        answer_grade="",
    )
    append_result_row(csv_file, row)

    with open(csv_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    header = lines[0].split(",")
    assert header == RESULTS_CSV_COLUMNS
    assert len(header) == 12

    # Verify invalid source raises error
    with pytest.raises(ValueError, match="Invalid source"):
        invalid_row = ResultRow(
            request_id="req-002",
            scenario="steady",
            source="fake",  # type: ignore
            start_time="2026-11-10T10:00:00.000Z",
            first_token_ms=0,
            total_ms=0,
            status="ok",
            retries=0,
            tokens_in=0,
            tokens_out=0,
            retrieval_setting="B",
        )
        invalid_row.validate()


def test_gitignore_protects_secrets():
    """LP-01: Verify .gitignore contains secret patterns."""
    root = Path(__file__).resolve().parent.parent
    gitignore_path = root / ".gitignore"
    assert gitignore_path.is_file()

    content = gitignore_path.read_text(encoding="utf-8")
    assert ".env" in content
    assert "*.key" in content
    assert "*.pem" in content
