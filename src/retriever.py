"""src/retriever.py - Zero-token local retrieval tool across settings A, B, and C.

Requirements:
- FR-RET-01: Returns ranked passages with citation metadata.
- FR-RET-02: Zero provider tokens (pure local BM25 + local re-ranking).
- FR-RET-03: Supports settings A, B, and C differing in piece size, fetch count, and re-ranking.
- FR-RET-04: Retrieval setting switchable by configuration or runtime override.
- FR-RET-05: Exposes numeric scores so the pipeline (S3 Decide) can evaluate evidence sufficiency.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml
from rank_bm25 import BM25Okapi

# Ensure repo root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.ingest import Chunk, ContentKind  # noqa: E402


@dataclass
class Passage:
    """Represents a retrieved passage with citation metadata and relevance score."""

    chunk_id: str
    paper_id: str
    version: int
    title: str
    section: str
    page: int
    kind: ContentKind
    text: str
    token_count: int
    score: float

    @property
    def citation(self) -> str:
        """Formatted citation string."""
        return f"[{self.paper_id}v{self.version}, p. {self.page}, §{self.section}, {self.chunk_id}]"

    def to_dict(self) -> dict:
        return asdict(self)


def tokenize_query_or_doc(text: str) -> list[str]:
    """Tokenize text into lowercased alphanumeric terms for BM25 indexing."""
    tokens = re.findall(r"\b[a-zA-Z0-9_\-\.]+\b", text.lower())
    # Clean trailing dots or punctuation
    cleaned = [t.strip(".-") for t in tokens if len(t.strip(".-")) > 0]
    return cleaned


class Retriever:
    """Zero-token local retriever supporting settings A, B, and C."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        data_dir: Path | str | None = None,
    ):
        self.root = Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path) if config_path else self.root / "config.yaml"
        self.data_dir = Path(data_dir) if data_dir else self.root / "data"

        self.config = self._load_config()
        self.default_setting = self.config.get("retrieval", {}).get("setting", "B")
        self.settings_cfg = self.config.get("retrieval", {}).get("settings", {})

        # Lazy loaded indices
        self._small_chunks: list[Chunk] | None = None
        self._medium_chunks: list[Chunk] | None = None
        self._bm25_small: BM25Okapi | None = None
        self._bm25_medium: BM25Okapi | None = None

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _load_chunks(self, size_type: Literal["small", "medium"]) -> tuple[list[Chunk], BM25Okapi]:
        if size_type == "small":
            if self._small_chunks is not None and self._bm25_small is not None:
                return self._small_chunks, self._bm25_small
            file_path = self.data_dir / "chunks_small.json"
        else:
            if self._medium_chunks is not None and self._bm25_medium is not None:
                return self._medium_chunks, self._bm25_medium
            file_path = self.data_dir / "chunks_medium.json"

        if not file_path.is_file():
            raise FileNotFoundError(
                f"Missing chunk index at '{file_path}'. Run src/ingest.py first."
            )

        with open(file_path, "r", encoding="utf-8") as f:
            raw_list = json.load(f)

        chunks = [Chunk(**item) for item in raw_list]
        tokenized_corpus = [tokenize_query_or_doc(c.text + " " + c.section) for c in chunks]
        bm25 = BM25Okapi(tokenized_corpus)

        if size_type == "small":
            self._small_chunks = chunks
            self._bm25_small = bm25
            return self._small_chunks, self._bm25_small
        else:
            self._medium_chunks = chunks
            self._bm25_medium = bm25
            return self._medium_chunks, self._bm25_medium

    def _rerank_candidates(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[Passage]:
        """Local zero-token re-ranker for Setting C.

        Combines normalized BM25 score, exact n-gram phrase matching,
        section heading alignment, and down-weights references.
        """
        if not candidates:
            return []

        query_tokens = tokenize_query_or_doc(query)
        clean_query = query.strip().lower()
        max_bm25 = max(score for _, score in candidates) if candidates else 1.0
        if max_bm25 <= 0:
            max_bm25 = 1.0

        scored_passages: list[Passage] = []

        for chunk, bm25_score in candidates:
            norm_bm25 = max(0.0, bm25_score / max_bm25)
            text_lower = chunk.text.lower()
            section_lower = chunk.section.lower()

            # 1. Exact phrase match bonus
            phrase_bonus = 0.0
            if clean_query and clean_query in text_lower:
                phrase_bonus += 0.35
            elif len(query_tokens) >= 2:
                # Bigram match bonus
                bigrams = [
                    f"{query_tokens[i]} {query_tokens[i + 1]}" for i in range(len(query_tokens) - 1)
                ]
                matched_bigrams = sum(1 for bg in bigrams if bg in text_lower)
                phrase_bonus += 0.15 * (matched_bigrams / len(bigrams))

            # 2. Section / title alignment bonus
            section_bonus = 0.0
            section_terms = tokenize_query_or_doc(chunk.section)
            if query_tokens and section_terms:
                shared = set(query_tokens).intersection(set(section_terms))
                if shared:
                    section_bonus += 0.20 * (len(shared) / len(query_tokens))

            # 3. Reference down-weighting (TRD: drop or down-weight references for retrieval)
            ref_multiplier = 1.0
            if (
                chunk.kind == "references"
                or "reference" in section_lower
                or "bibliography" in section_lower
            ):
                ref_multiplier = 0.25

            # Combined score
            final_score = (
                0.50 * norm_bm25 + 0.30 * phrase_bonus + 0.20 * section_bonus
            ) * ref_multiplier
            final_score = round(final_score, 4)

            scored_passages.append(
                Passage(
                    chunk_id=chunk.chunk_id,
                    paper_id=chunk.paper_id,
                    version=chunk.version,
                    title=chunk.title,
                    section=chunk.section,
                    page=chunk.page,
                    kind=chunk.kind,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    score=final_score,
                )
            )

        scored_passages.sort(key=lambda p: p.score, reverse=True)
        return scored_passages[:top_k]

    def search(
        self,
        query: str,
        setting: str | None = None,
        top_k: int | None = None,
    ) -> list[Passage]:
        """Search the indexed papers using the specified retrieval setting (A, B, or C).

        Returns top passages with citation metadata and relevance scores.
        """
        setting_key = setting.upper() if setting else self.default_setting
        if setting_key not in self.settings_cfg:
            raise ValueError(f"Unknown retrieval setting '{setting_key}'. Expected A, B, or C.")

        cfg = self.settings_cfg[setting_key]
        is_small = cfg.get("chunk_size_tokens", 500) <= 250
        chunks, bm25_index = self._load_chunks("small" if is_small else "medium")

        tokenized_q = tokenize_query_or_doc(query)
        if not tokenized_q:
            return []

        doc_scores = bm25_index.get_scores(tokenized_q)

        # Apply reference downweighting to raw BM25 scores
        adjusted_scores = []
        for idx, s in enumerate(doc_scores):
            chunk = chunks[idx]
            score = float(s)
            if chunk.kind == "references" or "reference" in chunk.section.lower():
                score *= 0.25
            adjusted_scores.append(score)

        rerank = cfg.get("rerank", False)
        k_needed = top_k if top_k is not None else cfg.get("top_k", 4)

        if rerank:
            fetch_k = cfg.get("fetch_k", 10)
            top_indices = sorted(
                range(len(adjusted_scores)),
                key=lambda i: adjusted_scores[i],
                reverse=True,
            )[:fetch_k]
            candidates = [
                (chunks[i], adjusted_scores[i]) for i in top_indices if adjusted_scores[i] > 0
            ]
            return self._rerank_candidates(query, candidates, top_k=k_needed)

        # Standard BM25 (Settings A and B)
        top_indices = sorted(
            range(len(adjusted_scores)),
            key=lambda i: adjusted_scores[i],
            reverse=True,
        )[:k_needed]

        passages: list[Passage] = []
        for i in top_indices:
            raw_score = float(adjusted_scores[i])
            if raw_score <= 0.0:
                continue
            chunk = chunks[i]
            passages.append(
                Passage(
                    chunk_id=chunk.chunk_id,
                    paper_id=chunk.paper_id,
                    version=chunk.version,
                    title=chunk.title,
                    section=chunk.section,
                    page=chunk.page,
                    kind=chunk.kind,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    score=round(raw_score, 4),
                )
            )

        return passages

    retrieve = search


if __name__ == "__main__":
    retriever = Retriever()
    sample_q = "How many parallel attention heads does the base Transformer use?"
    print(f"Query: {sample_q}\n")
    for s_name in ["A", "B", "C"]:
        results = retriever.search(sample_q, setting=s_name)
        print(f"--- Setting {s_name} (Top {len(results)} results) ---")
        for rank, p in enumerate(results, 1):
            print(f"[{rank}] Score: {p.score:.3f} | {p.citation}")
            print(f"    Excerpt: {p.text[:120].strip()}...\n")
