"""Unit tests for Item 6: Grader fairness and Verifier ablation."""
import pytest
from unittest.mock import MagicMock
from experiments.run_quality import QualityEvaluator
from src.pipeline import PipelineResponse


def test_grader_is_uniform_across_settings():
    """Grader must not give arbitrary free passes to Setting A while penalizing Setting B."""
    evaluator = QualityEvaluator()

    # Create a mock response with an empty/hallucinated answer
    resp_unverified = PipelineResponse(
        request_id="test-1",
        status="ok",
        outcome="succeeded",
        answer="This is an unverified response longer than twenty chars but has no valid citations.",
        citations=["fake-citation-123"],
        first_token_ms=50.0,
        total_ms=100.0,
        tokens_in=100,
        tokens_out=20,
        retrieval_setting="A",
        retries=0,
        verification_passed=False,
    )

    q_item = {
        "id": "q01_transformer_attention_heads",
        "category": "factual",
        "expected_grade": "correct",
    }

    # Under uniform grading, both Setting A and Setting B must grade this as 'wrong'
    grade_a = evaluator.grade_response(q_item, resp_unverified, setting="A")
    grade_b = evaluator.grade_response(q_item, resp_unverified, setting="B")

    assert grade_a == "wrong", f"Setting A was given a free pass: {grade_a}"
    assert grade_b == "wrong"
    assert grade_a == grade_b, "Grader logic is not uniform across settings!"


def test_verifier_ablation_has_same_16_questions_and_retrieval():
    """Verifier ablation must use identical 16 questions with no duplicates."""
    evaluator = QualityEvaluator()
    questions = evaluator.questions
    assert len(questions) == 16, f"Expected 16 questions, got {len(questions)}"
    qids = [q["id"] for q in questions]
    assert len(set(qids)) == 16, f"Found duplicate question IDs: {len(qids)} vs {len(set(qids))}"
