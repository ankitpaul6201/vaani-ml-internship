"""experiments/run_quality.py - Phase 7 Quality Evaluation across Settings A, B, and C.

Evaluates 16 curated questions across:
- Setting A: Baseline (k=3, single query, verifier off, no redraft)
- Setting B: Full Pipeline (k=5, multi-query, verifier on, redraft on failure)
- Setting C: Cost-Optimized (k=3, dynamic query, verifier on, degradation enabled)

Grades each answer:
- correct: Factual question answered with valid citations and key facts.
- wrong: Uncited claims, hallucination, or factual inaccuracy.
- refused_correctly: Unanswerable scope trap honestly refused with zero hallucinations.
- refused_wrongly: Answerable question improperly refused.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import yaml

from src.backends import BackendClient
from src.gateway import Gateway
from src.pipeline import Pipeline, PipelineResponse

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    question_id: str
    category: str
    setting: str
    expected_grade: str
    actual_grade: str
    outcome: str
    tokens_in: int
    tokens_out: int
    ttft_ms: float
    total_ms: float
    answer_preview: str


class QualityEvaluator:
    """Evaluates question answering quality, verification rates, and cost across settings."""

    def __init__(
        self,
        questions_path: Path | str | None = None,
        config_path: Path | str | None = None,
        standin_base_url: str = "http://127.0.0.1:8000/v1",
    ):
        self.root = ROOT_DIR
        self.questions_path = (
            Path(questions_path) if questions_path else self.root / "experiments" / "questions.yaml"
        )
        self.config_path = Path(config_path) if config_path else self.root / "config.yaml"
        self.standin_base_url = standin_base_url
        self.questions = self._load_questions()

    def _load_questions(self) -> list[dict[str, Any]]:
        with open(self.questions_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("questions", [])

    def grade_response(
        self,
        q_item: dict[str, Any],
        resp: PipelineResponse,
        setting: str = "",
    ) -> str:
        """Grades a response against gold rubric uniformly across all settings."""
        expected = q_item.get("expected_grade", "correct")
        ans_lower = resp.answer.lower()
        is_refusal = (
            resp.outcome == "refused_correctly"
            or (getattr(resp, "trace", None) and getattr(resp.trace, "answer_grade", "") == "refused_correctly")
            or "does not contain" in ans_lower
            or "not discussed" in ans_lower
            or "cannot answer" in ans_lower
            or "not provide" in ans_lower
            or "unanswerable" in ans_lower
            or "insufficient verified evidence" in ans_lower
        )

        if expected == "refused_correctly":
            if is_refusal:
                return "refused_correctly"
            else:
                return "wrong"  # Hallucinated answer for unanswerable question

        # Expected is "correct"
        if is_refusal:
            return "refused_wrongly"

        if resp.status != "ok" or len(resp.answer.strip()) < 15:
            return "wrong"

        if resp.verification_passed:
            return "correct"
        else:
            return "wrong"

    async def run_evaluation_for_setting(
        self,
        setting: str,
        pipeline: Pipeline,
    ) -> list[QualityResult]:
        """Runs the 16 questions against a configured pipeline."""
        print("\n=======================================================")
        print(
            f"[*] Running Quality Evaluation for Setting {setting} ({len(self.questions)} questions)"
        )
        print("=======================================================")

        results: list[QualityResult] = []
        scenario_name = f"quality_setting_{setting}"

        for idx, q_item in enumerate(self.questions):
            qid = q_item["id"]
            question = q_item["question"]
            expected = q_item["expected_grade"]
            category = q_item["category"]
            req_id = f"qual-{setting.lower()}-{idx:02d}"

            # Execute pipeline
            resp = await pipeline.run(
                question=question,
                scenario=scenario_name,
                request_id=req_id,
            )

            actual_grade = self.grade_response(q_item, resp, setting)

            res = QualityResult(
                question_id=qid,
                category=category,
                setting=setting,
                expected_grade=expected,
                actual_grade=actual_grade,
                outcome=resp.outcome,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
                ttft_ms=resp.first_token_ms,
                total_ms=resp.total_ms,
                answer_preview=resp.answer[:80].replace("\n", " "),
            )
            results.append(res)

            status_icon = "PASS" if actual_grade == expected else "FAIL"
            print(
                f"  [{status_icon:4s}] {qid:42s} | Grade: {actual_grade:17s} "
                f"| Exp: {expected:17s} | {resp.total_ms:6.1f}ms"
            )
            await asyncio.sleep(0.05)

        return results

    async def run_all_settings(self) -> dict[str, list[QualityResult]]:
        """Runs evaluation across Setting A, B, and C."""
        all_results = {}
        client = BackendClient(base_url=self.standin_base_url)

        # ---------------------------------------------------------------------
        # Setting A: Baseline (k=3, verifier off, no redraft)
        # ---------------------------------------------------------------------
        gw_a = Gateway(config_path=self.config_path, backend_client=client)
        pipe_a = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_a)
        pipe_a.retriever_setting = "A"
        pipe_a.enable_verify = False
        pipe_a.enable_redraft = False
        all_results["A"] = await self.run_evaluation_for_setting("A", pipe_a)

        # ---------------------------------------------------------------------
        # Setting B: Full Pipeline (k=5, multi-query, verifier on, redraft on)
        # ---------------------------------------------------------------------
        gw_b = Gateway(config_path=self.config_path, backend_client=client)
        pipe_b = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_b)
        pipe_b.retriever_setting = "B"
        pipe_b.enable_verify = True
        pipe_b.enable_redraft = True
        all_results["B"] = await self.run_evaluation_for_setting("B", pipe_b)

        # ---------------------------------------------------------------------
        # Setting C: Cost-Optimized (k=3, dynamic query, verifier on)
        # ---------------------------------------------------------------------
        gw_c = Gateway(config_path=self.config_path, backend_client=client)
        pipe_c = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_c)
        pipe_c.retriever_setting = "C"
        pipe_c.enable_verify = True
        pipe_c.enable_redraft = True
        all_results["C"] = await self.run_evaluation_for_setting("C", pipe_c)

        # Print Comparative Summary
        print("\n=======================================================")
        print("[*] QUALITY EVALUATION COMPARATIVE SUMMARY")
        print("=======================================================")
        print(
            f"{'Setting':10s} | {'Correct':8s} | {'Refused OK':11s} | {'Wrong/Trap':11s} | {'Accuracy %':11s} | {'Avg Tokens':11s} | {'Avg Latency'}"
        )
        print("-" * 80)
        for s in ["A", "B", "C"]:
            s_res = all_results[s]
            correct = sum(1 for r in s_res if r.actual_grade == "correct")
            refused = sum(1 for r in s_res if r.actual_grade == "refused_correctly")
            wrong = sum(1 for r in s_res if r.actual_grade in ("wrong", "refused_wrongly"))
            acc = ((correct + refused) / len(s_res)) * 100.0
            avg_toks = sum(r.tokens_in + r.tokens_out for r in s_res) / len(s_res)
            avg_lat = sum(r.total_ms for r in s_res) / len(s_res)
            print(
                f"Setting {s:2s} | {correct:8d} | {refused:11d} | {wrong:11d} | {acc:10.1f}% | {avg_toks:11.1f} | {avg_lat:6.1f}ms"
            )

        return all_results

    async def run_verifier_ablation(self) -> dict[str, list[QualityResult]]:
        """Ablates verifier on/off with identical retrieval (Setting B) across the 16 questions."""
        print("\n=======================================================")
        print("[*] VERIFIER ABLATION EXPERIMENT (IDENTICAL RETRIEVAL)")
        print("=======================================================")
        ablation_results = {}
        client = BackendClient(base_url=self.standin_base_url)

        # Condition 1: Setting B Retrieval, Verifier OFF
        gw_off = Gateway(config_path=self.config_path, backend_client=client)
        pipe_off = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_off)
        pipe_off.retriever_setting = "B"
        pipe_off.enable_verify = False
        pipe_off.enable_redraft = False
        ablation_results["Verifier_OFF"] = await self.run_evaluation_for_setting("Verifier_OFF", pipe_off)

        # Condition 2: Setting B Retrieval, Verifier ON
        gw_on = Gateway(config_path=self.config_path, backend_client=client)
        pipe_on = Pipeline(config_path=self.config_path, backend_client=client, gateway=gw_on)
        pipe_on.retriever_setting = "B"
        pipe_on.enable_verify = True
        pipe_on.enable_redraft = True
        ablation_results["Verifier_ON"] = await self.run_evaluation_for_setting("Verifier_ON", pipe_on)

        # Print 4-way grade breakdown
        print("\n=========================================================================================")
        print("[*] VERIFIER ABLATION 4-WAY GRADE BREAKDOWN")
        print("=========================================================================================")
        print(f"{'Condition':15s} | {'Correct':8s} | {'Wrong':8s} | {'Refused OK':11s} | {'Refused Wrong':14s} | {'Total':6s}")
        print("-" * 75)
        for cond in ["Verifier_OFF", "Verifier_ON"]:
            c_res = ablation_results[cond]
            correct = sum(1 for r in c_res if r.actual_grade == "correct")
            wrong = sum(1 for r in c_res if r.actual_grade == "wrong")
            refused_ok = sum(1 for r in c_res if r.actual_grade == "refused_correctly")
            refused_wrong = sum(1 for r in c_res if r.actual_grade == "refused_wrongly")
            print(f"{cond:15s} | {correct:8d} | {wrong:8d} | {refused_ok:11d} | {refused_wrong:14d} | {len(c_res):6d}")

        return ablation_results


if __name__ == "__main__":
    evaluator = QualityEvaluator()
    asyncio.run(evaluator.run_all_settings())
    asyncio.run(evaluator.run_verifier_ablation())
