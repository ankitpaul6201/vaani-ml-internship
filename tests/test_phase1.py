"""tests/test_phase1.py - Phase 1 Verification Test Suite.

Verifies:
- FR-ING-02: Page-aware text extraction keeping section headings.
- FR-ING-03: Metadata on each chunk (paper, version, section, page, chunk_id, kind).
- FR-ING-04: Content tagging: tables, equations, captions, references, prose.
- FR-RET-01: Ranked passages with citation metadata.
- FR-RET-02: Zero provider tokens (pure local execution).
- FR-RET-03: Settings A, B, and C differing in size, fetch count, and re-ranking.
- FR-RET-04: Configuration switching.
- FR-RET-05: Exposes numeric scores for pipeline S3 decision.
- 10 hand-picked verification queries with page validation.
- Retriever trap verification (the "chunking" word collision).
"""

from pathlib import Path

import pytest

from src.retriever import Passage, Retriever


@pytest.fixture(scope="module")
def retriever():
    root = Path(__file__).resolve().parent.parent
    return Retriever(
        config_path=root / "config.yaml",
        data_dir=root / "data",
    )


def test_FR_ING_02_page_aware_and_headings(retriever):
    """Verify page awareness and section heading preservation."""
    chunks, _ = retriever._load_chunks("medium")
    assert len(chunks) > 0

    for c in chunks:
        assert isinstance(c.page, int)
        assert c.page >= 1
        assert len(c.section.strip()) > 0
        assert len(c.text.strip()) > 0


def test_FR_ING_03_chunk_metadata(retriever):
    """Verify all mandatory chunk metadata fields exist."""
    chunks, _ = retriever._load_chunks("medium")

    valid_papers = {"1706.03762", "2410.00037", "2507.07955"}
    for c in chunks:
        assert c.paper_id in valid_papers
        assert c.version in (7, 1, 2)
        assert c.chunk_id.startswith(f"{c.paper_id}v{c.version}-p{c.page}-")
        assert c.kind in ("prose", "table", "equation", "caption", "references")
        assert c.token_count > 0


def test_FR_ING_04_content_tagging_distribution(retriever):
    """Verify tables, equations, captions, and references are tagged."""
    chunks, _ = retriever._load_chunks("medium")
    kinds = {c.kind for c in chunks}

    # All five kinds must be represented
    assert "prose" in kinds
    assert "table" in kinds
    assert "equation" in kinds
    assert "caption" in kinds
    assert "references" in kinds


def test_FR_ING_04_specific_table_equation_caption(retriever):
    """Inspect specific table, equation, and caption chunks."""
    chunks, _ = retriever._load_chunks("medium")

    # Table example: Table in Attention Is All You Need (e.g. Table 3 Variations)
    table_chunk = next(
        (c for c in chunks if c.paper_id == "1706.03762" and c.kind == "table"),
        None,
    )
    assert table_chunk is not None
    assert table_chunk.page in (8, 9)

    # Equation example: Attention formula or math tokens
    eq_chunk = next(
        (
            c
            for c in chunks
            if c.kind == "equation" and ("softmax" in c.text.lower() or "=" in c.text)
        ),
        None,
    )
    assert eq_chunk is not None

    # Caption example: Figure or Table caption
    caption_chunk = next(
        (
            c
            for c in chunks
            if c.kind == "caption" and c.text.lower().startswith(("figure", "table"))
        ),
        None,
    )
    assert caption_chunk is not None


def test_FR_RET_01_ranked_passages_with_citations(retriever):
    """Verify search returns ranked passages with citations."""
    passages = retriever.search("Transformer attention mechanism", setting="B")
    assert len(passages) > 0

    prev_score = float("inf")
    for p in passages:
        assert isinstance(p, Passage)
        assert p.score <= prev_score
        prev_score = p.score
        # Verify citation format
        assert p.citation.startswith(f"[{p.paper_id}v{p.version}, p. {p.page}")


def test_FR_RET_02_zero_groq_tokens(retriever, monkeypatch):
    """Verify retrieval uses zero Groq tokens and makes no network calls."""
    # Monkeypatch socket to ensure zero outbound network calls occur
    import socket

    def forbidden_connect(*args, **kwargs):
        raise RuntimeError("Outbound network call detected during local retrieval!")

    monkeypatch.setattr(socket, "create_connection", forbidden_connect)

    passages = retriever.search("Moshi audio codec frame rate", setting="B")
    assert len(passages) > 0


def test_FR_RET_03_three_settings(retriever):
    """Verify Settings A, B, and C differ in piece size, fetch count, and re-ranking."""
    q = "How many parallel attention heads does the base Transformer use?"

    res_a = retriever.search(q, setting="A")
    res_b = retriever.search(q, setting="B")
    res_c = retriever.search(q, setting="C")

    # Setting A: small chunks (~200 tokens), top_k=8
    assert len(res_a) == 8
    assert all("-s" in p.chunk_id for p in res_a)

    # Setting B: medium chunks (~500 tokens), top_k=4
    assert len(res_b) == 4
    assert all("-m" in p.chunk_id for p in res_b)

    # Setting C: medium chunks (~500 tokens), top_k=4, re-ranked
    assert len(res_c) == 4
    assert all("-m" in p.chunk_id for p in res_c)

    # Re-ranking in C should yield normalized re-ranked scores <= 1.0
    for p in res_c:
        assert 0.0 <= p.score <= 1.0


def test_FR_RET_04_config_switching(retriever):
    """Verify retrieval setting can be switched via configuration and parameters."""
    res_default = retriever.search("encoder-decoder attention")
    assert len(res_default) == retriever.settings_cfg[retriever.default_setting]["top_k"]

    with pytest.raises(ValueError, match="Unknown retrieval setting"):
        retriever.search("test", setting="INVALID_SETTING")


def test_FR_RET_05_scores_exposed_for_decide(retriever):
    """Verify numeric scores are exposed and off-topic queries yield low scores."""
    relevant_res = retriever.search("scaled dot product attention formula", setting="B")
    assert len(relevant_res) > 0
    top_rel_score = relevant_res[0].score

    offtopic_res = retriever.search("quantum gravity entanglement in wormholes", setting="B")
    top_off_score = offtopic_res[0].score if offtopic_res else 0.0

    # Relevant query should score substantially higher than completely unrelated query
    assert top_rel_score > top_off_score * 1.5


def test_ten_hand_picked_queries(retriever):
    """Ten hand-picked queries checking returned pieces and confirming page numbers."""
    queries = [
        # 1. Base Transformer attention heads
        {
            "query": "How many parallel attention heads does the base Transformer use?",
            "expected_paper": "1706.03762",
            "expected_pages": [5, 9],  # Section 3.2.2 / Table 3
        },
        # 2. Transformer d_model
        {
            "query": "What is the dimensionality d_model used in the base Transformer?",
            "expected_paper": "1706.03762",
            "expected_pages": [3, 5, 9],
        },
        # 3. Transformer BLEU score on English-to-German
        {
            "query": "What BLEU score did the Transformer achieve on English-to-German?",
            "expected_paper": "1706.03762",
            "expected_pages": [8, 9],  # Table 2
        },
        # 4. Moshi audio codec frame rate
        {
            "query": "Mimi encoder projects 24kHz waveform to latent representation of how many frames per second?",
            "expected_paper": "2410.00037",
            "expected_pages": [10, 23],  # Section 3.3.1 / Table 4
        },
        # 5. Moshi audio codec latency
        {
            "query": "What is the theoretical frame latency of the Mimi codec in milliseconds?",
            "expected_paper": "2410.00037",
            "expected_pages": [3, 10, 23],  # 80ms stride / Table 4 codec comparison
        },
        # 6. Moshi text foundation model
        {
            "query": "What text foundation model is Moshi built upon?",
            "expected_paper": "2410.00037",
            "expected_pages": [5, 6, 7],  # Helium
        },
        # 7. Dynamic Chunking model architecture
        {
            "query": "What is H-Net in the context of hierarchical sequence modeling?",
            "expected_paper": "2507.07955",
            "expected_pages": [1, 2, 3, 4],
        },
        # 8. Dynamic Chunking boundary predictor
        {
            "query": "How does Dynamic Chunking determine chunk boundaries without fixed rules?",
            "expected_paper": "2507.07955",
            "expected_pages": [2, 5, 6, 7],
        },
        # 9. Multi-head attention equations
        {
            "query": "MultiHead(Q, K, V) parameter matrices Concat head_1 head_h",
            "expected_paper": "1706.03762",
            "expected_pages": [4, 5],
        },
        # 10. Inner monologue multi-stream modeling
        {
            "query": "Inner Monologue in Moshi modeling joint text and audio tokens",
            "expected_paper": "2410.00037",
            "expected_pages": [15, 16],
        },
    ]

    for item in queries:
        passages = retriever.search(item["query"], setting="B")
        assert len(passages) > 0, f"No passages found for '{item['query']}'"

        # Check that expected paper is among top results
        papers = [p.paper_id for p in passages]
        assert item["expected_paper"] in papers, (
            f"Expected paper {item['expected_paper']} not in top results for query '{item['query']}'"
        )

        # Check page number alignment for the matching paper
        matching_passages = [p for p in passages if p.paper_id == item["expected_paper"]]
        matching_pages = [p.page for p in matching_passages]
        has_page_overlap = any(pg in item["expected_pages"] for pg in matching_pages)
        assert has_page_overlap, (
            f"Query '{item['query']}' expected pages in {item['expected_pages']}, got {matching_pages}"
        )


def test_retrieval_mistake_chunking_trap(retriever):
    """Prove Retriever Trap 1: Word collision where Dynamic Chunking wins for RAG chunking."""
    query = "What is the optimal chunking size for RAG retrieval?"
    passages = retriever.search(query, setting="B")

    assert len(passages) > 0
    top_p = passages[0]

    # The paper that wins is Dynamic Chunking (2507.07955), which discusses neural
    # sequence chunking in H-Net, NOT document chunking in RAG pipelines!
    assert top_p.paper_id == "2507.07955", (
        f"Expected Dynamic Chunking (2507.07955) to win due to word collision, got {top_p.paper_id}"
    )
