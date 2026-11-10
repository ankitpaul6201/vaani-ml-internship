"""scripts/verify_papers.py - Validate the exact pinned arXiv papers and SHA-256 hashes.

Requirement: FR-ING-01 (pinned versions and file hashes).
"""

import hashlib
import sys
from pathlib import Path

# Pinned arXiv paper definitions
PINNED_PAPERS = {
    "1706.03762v7.pdf": {
        "title": "Attention Is All You Need",
        "arxiv_id": "1706.03762",
        "version": 7,
        "sha256": "bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697",
    },
    "2410.00037v1.pdf": {
        "title": "Moshi: a speech-text foundation model for real-time dialogue",
        "arxiv_id": "2410.00037",
        "version": 1,
        "sha256": "a687144cc77bea86b0c60ae800ccb1d3e0816033fa5c7960388fe29a0dbfcfac",
    },
    "2507.07955v2.pdf": {
        "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling",
        "arxiv_id": "2507.07955",
        "version": 2,
        "sha256": "04a2b32e6e82853cfc23c5db132750874a86cde99219ec6f664128f4aa83122f",
    },
}


def compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def verify_papers(papers_dir: Path) -> bool:
    """Verifies that only and exactly the pinned papers are present with exact hashes."""
    if not papers_dir.is_dir():
        print(f"[ERROR] Papers directory does not exist: {papers_dir}", file=sys.stderr)
        return False

    pdf_files = sorted(papers_dir.glob("*.pdf"))
    pdf_names = {f.name for f in pdf_files}
    expected_names = set(PINNED_PAPERS.keys())

    # Check for unapproved / unpinned files (e.g., 2410.00037v2.pdf)
    unexpected = pdf_names - expected_names
    if unexpected:
        print(
            f"[ERROR] Unapproved/unpinned PDFs found in {papers_dir}: {unexpected}", file=sys.stderr
        )
        print("Remove or move unpinned versions so they cannot be indexed.", file=sys.stderr)
        return False

    # Check for missing required files
    missing = expected_names - pdf_names
    if missing:
        print(f"[ERROR] Missing required pinned PDFs in {papers_dir}: {missing}", file=sys.stderr)
        return False

    # Verify SHA-256 hashes
    all_matched = True
    print(f"Verifying {len(PINNED_PAPERS)} pinned research papers in '{papers_dir}':")
    for filename, meta in PINNED_PAPERS.items():
        file_path = papers_dir / filename
        computed_hash = compute_sha256(file_path)
        expected_hash = meta["sha256"]

        if computed_hash == expected_hash:
            print(
                f"  [OK] {filename} (arXiv:{meta['arxiv_id']}v{meta['version']}) -> {computed_hash[:16]}..."
            )
        else:
            print(
                f"  [FAIL] {filename} hash mismatch!\n"
                f"    Expected: {expected_hash}\n"
                f"    Computed: {computed_hash}",
                file=sys.stderr,
            )
            all_matched = False

    return all_matched


def main():
    root = Path(__file__).resolve().parent.parent
    papers_dir = root / "data" / "papers"
    success = verify_papers(papers_dir)
    if not success:
        sys.exit(1)
    print("\nAll papers verified against pinned versions and hashes.")


if __name__ == "__main__":
    main()
