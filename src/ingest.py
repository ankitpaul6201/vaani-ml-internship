"""src/ingest.py - Extract, chunk, tag, and index the three pinned arXiv papers.

Requirements:
- FR-ING-01: Pinned versions only (1706.03762v7, 2410.00037v1, 2507.07955v2).
- FR-ING-02: Page-aware text extraction keeping section headings.
- FR-ING-03: Metadata on each chunk: paper, version, section, page, chunk_id, kind.
- FR-ING-04: Content tagging: prose, table, equation, caption, references.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import pymupdf

# Ensure repo root is on sys.path for absolute imports
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.verify_papers import PINNED_PAPERS, verify_papers  # noqa: E402

ContentKind = Literal["prose", "table", "equation", "caption", "references"]


@dataclass
class Chunk:
    """Represents a passage extracted from a paper."""

    chunk_id: str
    paper_id: str
    version: int
    title: str
    section: str
    page: int  # 1-indexed page number
    kind: ContentKind
    text: str
    token_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Rough token estimation (approx 1.3 words per token, or words / 0.75)."""
    words = len(text.split())
    return max(1, int(words * 1.3))


def classify_content_kind(text: str, current_section: str) -> ContentKind:
    """Heuristic classifier for chunk content kind."""
    clean = text.strip()
    section_lower = current_section.lower()

    if (
        "reference" in section_lower
        or "bibliography" in section_lower
        or re.match(r"^\[\d+\]\s+[A-Z]", clean)
    ):
        return "references"

    # Figure or Table caption
    if re.match(r"^(Figure|Fig\.|Table)\s+\d+[:.]", clean, re.IGNORECASE):
        if clean.lower().startswith("table"):
            return "table" if len(clean.splitlines()) > 3 else "caption"
        return "caption"

    # Tabular structure (multiple columns with numbers / aligned separators)
    lines = clean.splitlines()
    if len(lines) >= 3:
        has_num_rows = sum(bool(re.search(r"\b\d+(\.\d+)?\b.*\b\d+(\.\d+)?\b", ln)) for ln in lines)
        if has_num_rows >= 2 and ("|" in clean or "\t" in clean or "  " in clean):
            return "table"

    # Math equations
    math_tokens = [
        "softmax(",
        "\\sum",
        "\\mathbb",
        "\\mathbf",
        "\\cdot",
        "\\approx",
        "argmax",
        "argmin",
        "qkt",
        "\\sqrt",
        "√",
        "d_k",
        "d_v",
        "d_{model}",
        "∈",
        "∑",
        "∏",
        "attention(",
        "multihead(",
        "ffn(",
        "pe(pos",
    ]
    clean_lower = clean.lower()
    has_math_token = any(tok in clean_lower for tok in math_tokens)
    has_eq_sign = "=" in clean or "≈" in clean

    # Short formula block with equation or function assignment
    if (
        (has_math_token and has_eq_sign)
        or re.search(r"\b\w+\s*\(.*\)\s*=\s*", clean)
        or re.search(r"=\s*.*\((\d+)\)$", clean)
    ):
        if len(clean.split()) < 40:  # equations are typically compact
            return "equation"

    # Multi-indicator math
    math_count = sum(tok in clean_lower for tok in math_tokens)
    if math_count >= 2 and len(clean.split()) < 50:
        return "equation"

    return "prose"


def extract_paper_chunks(
    pdf_path: Path,
    paper_meta: dict,
    target_token_size: int = 500,
    size_prefix: str = "m",
) -> list[Chunk]:
    """Extracts page-aware chunks from a single PDF document."""
    doc = pymupdf.open(pdf_path)
    toc = doc.get_toc()  # [[lvl, title, page], ...]
    toc_by_page = {}
    for entry in toc:
        lvl, title, p = entry[0], entry[1], entry[2]
        # Keep highest level section for that page
        if p not in toc_by_page or lvl < toc_by_page[p][0]:
            toc_by_page[p] = (lvl, title)

    chunks: list[Chunk] = []
    current_section = "Title and Abstract"
    chunk_counter = 1

    for page_idx in range(len(doc)):
        page_num = page_idx + 1  # 1-indexed

        # Check if TOC advances section on this page
        if page_num in toc_by_page:
            current_section = toc_by_page[page_num][1].strip()

        page = doc[page_idx]
        blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, block_no, block_type)

        buffer_text = ""
        buffer_kind: ContentKind = "prose"

        for b in blocks:
            text = b[4].strip()
            if not text:
                continue

            # Skip solitary page numbers / header artifacts
            if re.match(r"^\d+$", text) and len(text) <= 3:
                continue

            # Check if block looks like a section heading (numbered or standard unnumbered)
            heading_match = re.match(r"^(\d+(\.\d+)*\s+[A-Z][A-Za-z\s\-]+)$", text) or re.match(
                r"^(References|Bibliography|Acknowledgements|Appendix(\s+[A-Z0-9]+)?)$",
                text,
                re.IGNORECASE,
            )
            if heading_match and len(text) < 80:
                current_section = text
                continue

            block_kind = classify_content_kind(text, current_section)

            # If current block is special (table, equation, caption), keep it as its own unit
            if block_kind in ("table", "equation", "caption"):
                # Flush existing buffer first
                if buffer_text.strip():
                    cid = f"{paper_meta['arxiv_id']}v{paper_meta['version']}-p{page_num}-{size_prefix}{chunk_counter:03d}"
                    chunks.append(
                        Chunk(
                            chunk_id=cid,
                            paper_id=paper_meta["arxiv_id"],
                            version=paper_meta["version"],
                            title=paper_meta["title"],
                            section=current_section,
                            page=page_num,
                            kind=buffer_kind,
                            text=buffer_text.strip(),
                            token_count=estimate_tokens(buffer_text),
                        )
                    )
                    chunk_counter += 1
                    buffer_text = ""

                # Add special block as standalone chunk
                cid = f"{paper_meta['arxiv_id']}v{paper_meta['version']}-p{page_num}-{size_prefix}{chunk_counter:03d}"
                chunks.append(
                    Chunk(
                        chunk_id=cid,
                        paper_id=paper_meta["arxiv_id"],
                        version=paper_meta["version"],
                        title=paper_meta["title"],
                        section=current_section,
                        page=page_num,
                        kind=block_kind,
                        text=text,
                        token_count=estimate_tokens(text),
                    )
                )
                chunk_counter += 1
                continue

            # Standard prose accumulation
            combined = f"{buffer_text}\n\n{text}".strip() if buffer_text else text
            token_est = estimate_tokens(combined)

            if token_est >= target_token_size and buffer_text.strip():
                # Flush full chunk
                cid = f"{paper_meta['arxiv_id']}v{paper_meta['version']}-p{page_num}-{size_prefix}{chunk_counter:03d}"
                chunks.append(
                    Chunk(
                        chunk_id=cid,
                        paper_id=paper_meta["arxiv_id"],
                        version=paper_meta["version"],
                        title=paper_meta["title"],
                        section=current_section,
                        page=page_num,
                        kind="references" if "reference" in current_section.lower() else "prose",
                        text=buffer_text.strip(),
                        token_count=estimate_tokens(buffer_text),
                    )
                )
                chunk_counter += 1
                buffer_text = text
            else:
                buffer_text = combined
                buffer_kind = block_kind

        # Flush trailing text on page
        if buffer_text.strip():
            cid = f"{paper_meta['arxiv_id']}v{paper_meta['version']}-p{page_num}-{size_prefix}{chunk_counter:03d}"
            chunks.append(
                Chunk(
                    chunk_id=cid,
                    paper_id=paper_meta["arxiv_id"],
                    version=paper_meta["version"],
                    title=paper_meta["title"],
                    section=current_section,
                    page=page_num,
                    kind="references" if "reference" in current_section.lower() else buffer_kind,
                    text=buffer_text.strip(),
                    token_count=estimate_tokens(buffer_text),
                )
            )
            chunk_counter += 1

    return chunks


def build_library(papers_dir: Path, output_dir: Path) -> dict[str, list[Chunk]]:
    """Ingests all pinned papers and produces both small and medium chunk sets."""
    # Verify exact pinned versions first (hard fail on mismatch or extra unpinned PDFs)
    if not verify_papers(papers_dir):
        raise ValueError("Paper verification failed! Cannot index unpinned papers.")

    small_chunks: list[Chunk] = []
    medium_chunks: list[Chunk] = []

    for filename, meta in PINNED_PAPERS.items():
        pdf_path = papers_dir / filename
        # Setting A chunks (~200 tokens)
        s_chunks = extract_paper_chunks(
            pdf_path=pdf_path,
            paper_meta=meta,
            target_token_size=200,
            size_prefix="s",
        )
        small_chunks.extend(s_chunks)

        # Setting B/C chunks (~500 tokens)
        m_chunks = extract_paper_chunks(
            pdf_path=pdf_path,
            paper_meta=meta,
            target_token_size=500,
            size_prefix="m",
        )
        medium_chunks.extend(m_chunks)

    output_dir.mkdir(parents=True, exist_ok=True)
    small_file = output_dir / "chunks_small.json"
    medium_file = output_dir / "chunks_medium.json"

    with open(small_file, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in small_chunks], f, indent=2)

    with open(medium_file, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in medium_chunks], f, indent=2)

    print(f"Ingested {len(small_chunks)} small chunks (~200t) -> {small_file}")
    print(f"Ingested {len(medium_chunks)} medium chunks (~500t) -> {medium_file}")

    return {"small": small_chunks, "medium": medium_chunks}


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    papers_path = root / "data" / "papers"
    data_out = root / "data"
    build_library(papers_path, data_out)
