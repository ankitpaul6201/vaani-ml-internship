"""src/citecheck.py - Deterministic, zero-token citation verification engine.

Requirements:
- FR-PIP-06: Runs deterministic citation check on every draft.
- FR-PIP-07: Catches invalid chunk IDs, fabricated quotes, and uncited claims.
- Zero provider tokens (pure Python string and regex validation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

# Chunk ID regex pattern: e.g. 1706.03762v7-p5-m015 or 2410.00037v1-p10-s023
CHUNK_ID_PATTERN = re.compile(r"\b(\d{4}\.\d{4,5}v\d+\-p\d+\-[sm]\d{3})\b")

# Standard refusal phrases that do not require chunk citations
REFUSAL_PHRASES = [
    "do not contain",
    "does not contain",
    "do not mention",
    "does not mention",
    "not covered in the papers",
    "not mentioned in the provided",
    "papers do not provide",
    "papers do not say",
    "insufficient evidence",
    "no information",
]


@dataclass
class CiteCheckResult:
    """Result of deterministic citation verification."""

    passed: bool
    reason: str
    found_citations: list[str] = field(default_factory=list)
    invalid_chunk_ids: list[str] = field(default_factory=list)
    fabricated_quotes: list[str] = field(default_factory=list)
    uncited_sentences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "found_citations": self.found_citations,
            "invalid_chunk_ids": self.invalid_chunk_ids,
            "fabricated_quotes": self.fabricated_quotes,
            "uncited_sentences": self.uncited_sentences,
        }

    @property
    def is_valid(self) -> bool:
        return self.passed

    @property
    def is_honest_refusal(self) -> bool:
        return "honest refusal" in self.reason.lower()


def extract_chunk_ids(text: str) -> list[str]:
    """Finds all chunk IDs cited in text."""
    return CHUNK_ID_PATTERN.findall(text)


def extract_quoted_spans(text: str, min_length: int = 15) -> list[str]:
    """Extracts quoted strings of substantive length."""
    # Matches "...", “...”, or '...'
    patterns = [
        r'"([^"\n\r]{15,})"',
        r"“([^”\n\r]{15,})”",
    ]
    spans: list[str] = []
    for pat in patterns:
        spans.extend(re.findall(pat, text))
    return [s.strip() for s in spans if len(s.strip()) >= min_length]


def split_sentences(text: str) -> list[str]:
    """Splits text into sentences, stripping markdown headings, lists, and empty lines."""
    clean_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        clean_lines.append(line)
    clean_text = " ".join(clean_lines)

    # Split by standard sentence delimiters (. ! ?) followed by space or end
    raw_sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    sentences = [s.strip() for s in raw_sentences if len(s.strip().split()) >= 5]
    return sentences


def is_refusal(text: str) -> bool:
    """Checks whether the response is an explicit refusal."""
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in REFUSAL_PHRASES)


def verify_citations(
    draft_text: str,
    retrieved_chunks: Sequence[Any],
    require_sentence_citations: bool = True,
) -> CiteCheckResult:
    """Deterministically verifies a drafted answer against retrieved passages.

    Checks:
    1. If draft is an honest refusal, passes immediately.
    2. Every cited chunk ID must exist in the retrieved chunks set.
    3. At least one valid citation must be present for substantive answers.
    4. Every substantive quoted span must appear in the text of the cited chunks.
    5. Every substantive factual sentence must have at least one citation.
    """
    clean_draft = draft_text.strip()
    if not clean_draft:
        return CiteCheckResult(
            passed=False,
            reason="Draft is empty.",
        )

    # 1. Honest refusal exemption
    if is_refusal(clean_draft):
        return CiteCheckResult(
            passed=True,
            reason="Draft is a valid honest refusal.",
        )

    # Build lookup of valid retrieved chunk IDs and their normalized text
    valid_chunks_map: dict[str, str] = {}
    for c in retrieved_chunks:
        cid = getattr(c, "chunk_id", None) or (c["chunk_id"] if isinstance(c, dict) else None)
        ctext = getattr(c, "text", None) or (c["text"] if isinstance(c, dict) else "")
        if cid:
            # Normalize whitespace for quote matching
            valid_chunks_map[cid] = " ".join(ctext.lower().split())

    cited_ids = extract_chunk_ids(clean_draft)

    # 2. Check for missing/fabricated chunk IDs
    invalid_ids = [cid for cid in cited_ids if cid not in valid_chunks_map]
    if invalid_ids:
        return CiteCheckResult(
            passed=False,
            reason=f"Draft cited non-existent or unretrieved chunk IDs: {invalid_ids}",
            found_citations=cited_ids,
            invalid_chunk_ids=invalid_ids,
        )

    # 3. Require at least one citation for factual answers
    if not cited_ids:
        return CiteCheckResult(
            passed=False,
            reason="Draft contains factual claims but provides zero chunk citations.",
            found_citations=[],
        )

    # 4. Check for fabricated quotes
    # Combine text of all actually cited chunks
    cited_corpus = " ".join(valid_chunks_map[cid] for cid in cited_ids if cid in valid_chunks_map)
    quoted_spans = extract_quoted_spans(clean_draft)
    fabricated: list[str] = []

    for span in quoted_spans:
        norm_span = " ".join(span.lower().split())
        # Strip trailing punctuation
        norm_span = norm_span.strip(".,;:!?")
        if norm_span not in cited_corpus:
            fabricated.append(span)

    if fabricated:
        return CiteCheckResult(
            passed=False,
            reason=f"Draft includes fabricated or modified quotes not in cited chunks: {fabricated}",
            found_citations=cited_ids,
            fabricated_quotes=fabricated,
        )

    # 5. Check sentence citation coverage
    uncited: list[str] = []
    if require_sentence_citations:
        sentences = split_sentences(clean_draft)
        for sent in sentences:
            if is_refusal(sent):
                continue
            # Check if sentence contains a chunk ID
            sent_citations = extract_chunk_ids(sent)
            if not sent_citations:
                uncited.append(sent)

        # Allow at most 1 introductory/transitional uncited sentence (e.g. "Based on the papers:")
        if len(uncited) > 1:
            return CiteCheckResult(
                passed=False,
                reason=f"Found {len(uncited)} factual sentences lacking citations.",
                found_citations=cited_ids,
                uncited_sentences=uncited,
            )

    return CiteCheckResult(
        passed=True,
        reason="All citations valid, quotes grounded, and claims cited.",
        found_citations=cited_ids,
    )
