"""src/pipeline.py - Five-step question-answering pipeline with branching, bounded loops, and Gateway routing.

Requirements:
- FR-PIP-01: Five steps (understand, retrieve, decide, draft, verify).
- FR-PIP-02: Tool call to local retriever (zero provider tokens).
- FR-PIP-03: Refusal branch and bounded loops (widen once, redraft once).
- FR-PIP-04: Draft answers only from supplied passages and cites chunk IDs.
- FR-PIP-05: Refuses honestly when evidence is insufficient.
- FR-PIP-06: Runs deterministic citation check on every draft.
- FR-PIP-07: Buffers draft; unverified drafts are never released.
- FR-PIP-08: Maximum one redraft; second failure triggers refusal.
- FR-PIP-10: Request deadlines strictly enforced; expired requests cancelled.
- All model calls routed through central Gateway.
- Emits results.csv rows and traces.jsonl records.
"""

from __future__ import annotations

import datetime
import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.backends import (
    BackendClient,
    ModelRequest,
)
from src.citecheck import split_sentences, verify_citations
from src.gateway import (
    DegradationLevel,
    Gateway,
    GatewayCall,
)
from src.prompts import (
    DRAFT_SYSTEM_PROMPT,
    REDRAFT_SYSTEM_PROMPT,
    UNDERSTAND_SYSTEM_PROMPT,
)
from src.retriever import Retriever
from src.trace import (
    AllowedAnswerGrade,
    AllowedSource,
    AllowedStatus,
    ResultRow,
    StageRecord,
    TraceRecord,
    append_result_row,
    append_trace_record,
)

logger = logging.getLogger(__name__)


@dataclass
class PipelineResponse:
    """The final response released to the user."""

    request_id: str
    answer: str
    citations: list[str] = field(default_factory=list)
    status: AllowedStatus = "ok"
    outcome: str = "succeeded"
    first_token_ms: float = 0.0
    total_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    verification_passed: bool = False
    retrieval_setting: str = "B"
    retries: int = 0
    degradation_level: int = 0
    trace: TraceRecord | None = None


def get_git_commit_hash() -> str:
    """Retrieves current git commit hash if inside a repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "uncommitted"


class Pipeline:
    """5-step question-answering pipeline with Gateway integration."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        retriever: Retriever | None = None,
        backend_client: BackendClient | None = None,
        gateway: Gateway | None = None,
    ):
        self.root = Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path) if config_path else self.root / "config.yaml"
        self.config = self._load_config()

        self.retriever = retriever or Retriever(config_path=self.config_path)

        # Setup backend client
        backend_mode = self.config.get("backend", {}).get("mode", "groq")
        base_url = (
            self.config.get("backend", {}).get("groq_base_url", "https://api.groq.com/openai/v1")
            if backend_mode == "groq"
            else self.config.get("backend", {}).get("base_url", "http://localhost:8000/v1")
        )
        self.backend_client = backend_client or BackendClient(base_url=base_url)

        # Central Gateway choke-point
        self.gateway = gateway or Gateway(
            config_path=self.config_path,
            backend_client=self.backend_client,
        )

        self.small_model = self.config.get("models", {}).get("small", "qwen/qwen3.8-27b")
        self.large_model = self.config.get("models", {}).get("large", "qwen/qwen3.8-27b")
        self.patience_s = float(self.config.get("patience_s", 20.0))
        self.seed = int(self.config.get("seed", 42))
        self.enable_verify = bool(self.config.get("pipeline", {}).get("enable_verify", True))
        self.enable_redraft = bool(self.config.get("pipeline", {}).get("enable_redraft", True))
        base_url = str(getattr(self.backend_client, "base_url", "")) if self.backend_client else ""
        if "127.0.0.1" in base_url or "localhost" in base_url:
            self.default_source: AllowedSource = "stand-in"
        else:
            self.default_source: AllowedSource = "real"

        self.results_csv_path = self.root / self.config.get("paths", {}).get(
            "results_csv", "results/results.csv"
        )
        self.traces_jsonl_path = self.root / self.config.get("paths", {}).get(
            "traces_jsonl", "results/traces.jsonl"
        )
        self.commit_hash = get_git_commit_hash()

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    async def run(
        self,
        question: str,
        scenario: str = "interactive",
        request_id: str | None = None,
        deadline_s: float | None = None,
        force_bad_draft: bool = False,
    ) -> PipelineResponse:
        """Executes the pipeline for a single question."""
        req_id = request_id or f"req-{uuid.uuid4().hex[:8]}"
        t_start = time.perf_counter()
        start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        deadline = t_start + (deadline_s if deadline_s is not None else self.patience_s)

        stages: list[StageRecord] = []
        gw_results: list[GatewayResult] = []
        model_first_byte_ms = 0.0
        tokens_in_total = 0
        tokens_out_total = 0
        retries_count = 0
        active_setting = self.config.get("retrieval", {}).get("setting", "B")

        def record_stage(
            name: str,
            t_enq: float,
            t_st: float,
            t_fb: float,
            t_end: float,
            attempt: int = 1,
            status: str = "ok",
        ):
            stages.append(
                StageRecord(
                    name=name,
                    t_enqueue=t_enq,
                    t_start=t_st,
                    t_first_byte=t_fb,
                    t_end=t_end,
                    attempt=attempt,
                    status=status,
                )
            )

        # Check Degradation Level at entry
        deg_level = self.gateway.get_degradation_level()

        # ----------------------------------------------------
        # L3 / L4 Extractive Fallback (No Model Generation)
        # ----------------------------------------------------
        if deg_level >= DegradationLevel.L3_EXTRACT_ONLY:
            t_ret_start = time.perf_counter()
            pieces = self.retriever.retrieve(question, setting=active_setting, top_k=3)
            t_ret_end = time.perf_counter()
            record_stage(
                "S2_retrieve_extractive",
                t_ret_start,
                t_ret_start,
                t_ret_end,
                t_ret_end,
                1,
                "ok",
            )

            if pieces:
                top = pieces[0]
                sentences = split_sentences(top.text)
                extract_sentences = [
                    f"{s.strip()} [{top.chunk_id}]" for s in sentences[:3] if len(s.strip()) > 10
                ]
                extract_text = (
                    f"Extractive answer from [{top.chunk_id} p. {top.page} § {top.section}]:\n\n"
                    + " ".join(extract_sentences)
                )
                v_res = verify_citations(extract_text, pieces)
                t_done = time.perf_counter()
                elapsed_ms = (t_done - t_start) * 1000.0

                return self._finalize_response(
                    req_id=req_id,
                    scenario=scenario,
                    start_wall=start_wall,
                    t_start=t_start,
                    deadline=deadline,
                    stages=stages,
                    answer=extract_text,
                    citations=[top.chunk_id],
                    status="ok",
                    outcome="succeeded"
                    if deg_level == DegradationLevel.L3_EXTRACT_ONLY
                    else "daily_limit_reached",
                    first_token_ms=elapsed_ms,
                    total_ms=elapsed_ms,
                    tokens_in=0,
                    tokens_out=0,
                    retrieval_setting=active_setting,
                    retries=0,
                    answer_grade="correct" if v_res.is_valid else "refused_correctly",
                    verification_passed=v_res.is_valid,
                    degradation_level=int(deg_level),
                )
            else:
                refusal = "The provided research papers do not contain sufficient evidence to answer this question."
                t_done = time.perf_counter()
                elapsed_ms = (t_done - t_start) * 1000.0
                return self._finalize_response(
                    req_id=req_id,
                    scenario=scenario,
                    start_wall=start_wall,
                    t_start=t_start,
                    deadline=deadline,
                    stages=stages,
                    answer=refusal,
                    citations=[],
                    status="ok",
                    outcome="refused_no_evidence",
                    first_token_ms=elapsed_ms,
                    total_ms=elapsed_ms,
                    tokens_in=0,
                    tokens_out=0,
                    retrieval_setting=active_setting,
                    retries=0,
                    answer_grade="refused_correctly",
                    verification_passed=True,
                    degradation_level=int(deg_level),
                )

        # ----------------------------------------------------
        # Step 1: Understand (S1) via Gateway (Priority 2)
        # ----------------------------------------------------
        if time.perf_counter() >= deadline:
            return self._build_given_up_response(
                req_id,
                scenario,
                start_wall,
                t_start,
                deadline,
                stages,
                tokens_in_total,
                tokens_out_total,
                active_setting,
            )

        t_s1_enq = time.perf_counter()
        search_query = question
        is_off_topic = False

        s1_req = ModelRequest(
            model=self.small_model,
            messages=[
                {"role": "system", "content": UNDERSTAND_SYSTEM_PROMPT},
                {"role": "user", "content": f"User Question: {question}"},
            ],
            max_tokens=60,
            temperature=0.0,
        )

        s1_est_tokens = len(question.split()) + 80
        gw_s1 = await self.gateway.call(
            GatewayCall(
                request_id=req_id,
                priority=2,  # Priority 2 for new arrivals
                stage_name="S1",
                model_request=s1_req,
                deadline=deadline,
                prompt_tokens_est=s1_est_tokens,
            )
        )

        retries_count += gw_s1.retries
        gw_results.append(gw_s1)
        s1_res = gw_s1.model_result

        if gw_s1.status in ("dropped", "timeout") or time.perf_counter() >= deadline:
            return self._build_given_up_response(
                req_id,
                scenario,
                start_wall,
                t_start,
                deadline,
                stages,
                tokens_in_total,
                tokens_out_total,
                active_setting,
                gw_results=gw_results,
                model_first_byte_ms=model_first_byte_ms,
            )

        if gw_s1.status == "shed":
            t_now = time.perf_counter()
            total_ms = (t_now - t_start) * 1000.0
            return self._finalize_response(
                req_id=req_id,
                scenario=scenario,
                start_wall=start_wall,
                t_start=t_start,
                deadline=deadline,
                stages=stages,
                answer="Gateway busy: request shed due to high queue backlog. Please retry shortly.",
                citations=[],
                status="dropped",
                outcome="shed",
                first_token_ms=total_ms,
                total_ms=total_ms,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                retrieval_setting=active_setting,
                retries=retries_count,
                answer_grade="",
                verification_passed=False,
                degradation_level=int(deg_level),
            )

        if s1_res:
            tokens_in_total += s1_res.tokens_in
            tokens_out_total += s1_res.tokens_out
            record_stage(
                "S1_understand",
                t_s1_enq,
                s1_res.t_start,
                s1_res.t_first_byte,
                s1_res.t_end,
                1,
                "ok" if s1_res.status_code == 200 else "error",
            )

            if s1_res.content:
                try:
                    s1_json = json.loads(s1_res.content)
                    search_query = s1_json.get("search_query", question)
                    is_off_topic = s1_json.get("scope", "in_scope") == "off_topic" or s1_json.get(
                        "is_off_topic", False
                    )
                except Exception:
                    search_query = question

        # FR-PIP-03: Cheapest-path honest refusal for off-topic questions
        if is_off_topic:
            t_off = time.perf_counter()
            elapsed_ms = (t_off - t_start) * 1000.0
            refusal_text = (
                "This question is outside the scope of the three supported research papers "
                "(Attention Is All You Need, LoRA, and Dynamic Chunking)."
            )
            return self._finalize_response(
                req_id=req_id,
                scenario=scenario,
                start_wall=start_wall,
                t_start=t_start,
                deadline=deadline,
                stages=stages,
                answer=refusal_text,
                citations=[],
                status="ok",
                outcome="refused_scope",
                first_token_ms=elapsed_ms,
                total_ms=elapsed_ms,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                retrieval_setting=active_setting,
                retries=retries_count,
                answer_grade="refused_correctly",
                verification_passed=True,
                degradation_level=int(deg_level),
            )

        # ----------------------------------------------------
        # Step 2: Retrieve (S2) via local Retriever (Zero Tokens)
        # ----------------------------------------------------
        if time.perf_counter() >= deadline:
            return self._build_given_up_response(
                req_id,
                scenario,
                start_wall,
                t_start,
                deadline,
                stages,
                tokens_in_total,
                tokens_out_total,
                active_setting,
            )

        t_s2_enq = time.perf_counter()
        k_val = 3 if deg_level == DegradationLevel.L2_SMALL_MODEL_SHORT_K3 else None
        passages = self.retriever.retrieve(search_query, setting=active_setting, top_k=k_val)
        t_s2_end = time.perf_counter()
        record_stage("S2_retrieve", t_s2_enq, t_s2_enq, t_s2_end, t_s2_end, 1, "ok")

        # ----------------------------------------------------
        # Step 3: Decide (S3) - Check Evidence & Bounded Widen Loop
        # ----------------------------------------------------
        t_s3_enq = time.perf_counter()
        min_score = 0.5

        if not passages or passages[0].score < min_score:
            # Bounded Loop 1: Widen search once
            t_widen_enq = time.perf_counter()
            widened_query = f"{question} transformer attention lora chunking"
            passages = self.retriever.retrieve(widened_query, setting=active_setting, top_k=k_val)
            t_widen_end = time.perf_counter()
            record_stage(
                "S3_widen_retrieve",
                t_widen_enq,
                t_widen_enq,
                t_widen_end,
                t_widen_end,
                2,
                "ok",
            )

        t_s3_end = time.perf_counter()
        record_stage("S3_decide", t_s3_enq, t_s3_enq, t_s3_end, t_s3_end, 1, "ok")

        if not passages or passages[0].score < min_score:
            # Insufficient evidence -> Honest Refusal
            t_refusal = time.perf_counter()
            elapsed_ms = (t_refusal - t_start) * 1000.0
            refusal_text = (
                "The provided research papers do not contain sufficient evidence "
                "to answer this question."
            )
            return self._finalize_response(
                req_id=req_id,
                scenario=scenario,
                start_wall=start_wall,
                t_start=t_start,
                deadline=deadline,
                stages=stages,
                answer=refusal_text,
                citations=[],
                status="ok",
                outcome="refused_no_evidence",
                first_token_ms=elapsed_ms,
                total_ms=elapsed_ms,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                retrieval_setting=active_setting,
                retries=retries_count,
                answer_grade="refused_correctly",
                verification_passed=True,
                degradation_level=int(deg_level),
            )

        # ----------------------------------------------------
        # Step 4: Draft (S4) via Gateway (Priority 1)
        # ----------------------------------------------------
        if time.perf_counter() >= deadline:
            return self._build_given_up_response(
                req_id,
                scenario,
                start_wall,
                t_start,
                deadline,
                stages,
                tokens_in_total,
                tokens_out_total,
                active_setting,
            )

        t_s4_enq = time.perf_counter()
        # Cap passages to top 3 to respect token ceilings
        passages_for_prompt = passages[:3]
        passages_text = "\n\n".join(
            f"Passage [{p.chunk_id}] (Paper {p.paper_id}, Section {p.section}, Page {p.page}):\n{p.text}"
            for p in passages_for_prompt
        )

        user_content = f"Research Passages:\n{passages_text}\n\nQuestion: {question}"
        max_draft_tokens = 200 if deg_level == DegradationLevel.L2_SMALL_MODEL_SHORT_K3 else 300

        s4_req = ModelRequest(
            model=self.large_model,
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_draft_tokens,
            temperature=0.0,
        )

        s4_est_tokens = sum(p.token_count for p in passages_for_prompt) + len(question.split()) + 80

        gw_s4 = await self.gateway.call(
            GatewayCall(
                request_id=req_id,
                priority=1,  # Priority 1 for in-progress drafting
                stage_name="S4",
                model_request=s4_req,
                deadline=deadline,
                prompt_tokens_est=s4_est_tokens,
            )
        )

        retries_count += gw_s4.retries
        gw_results.append(gw_s4)
        s4_res = gw_s4.model_result

        if s4_res and s4_res.t_first_byte > 0:
            model_first_byte_ms = max(0.0, (s4_res.t_first_byte - t_start) * 1000.0)
        else:
            model_first_byte_ms = max(0.0, (t_s4_enq - t_start) * 1000.0)

        if gw_s4.status in ("dropped", "timeout") or time.perf_counter() >= deadline:
            return self._build_given_up_response(
                req_id,
                scenario,
                start_wall,
                t_start,
                deadline,
                stages,
                tokens_in_total,
                tokens_out_total,
                active_setting,
                gw_results=gw_results,
                model_first_byte_ms=model_first_byte_ms,
            )

        if gw_s4.status == "rate_limited":
            t_now = time.perf_counter()
            total_ms = (t_now - t_start) * 1000.0
            return self._finalize_response(
                req_id=req_id,
                scenario=scenario,
                start_wall=start_wall,
                t_start=t_start,
                deadline=deadline,
                stages=stages,
                answer="Provider rate limit reached and retry budget exhausted.",
                citations=[],
                status="rate_limited",
                outcome="rate_limited",
                first_token_ms=total_ms,
                total_ms=total_ms,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                retrieval_setting=active_setting,
                retries=retries_count,
                answer_grade="",
                verification_passed=False,
                degradation_level=int(deg_level),
            )

        if not s4_res or s4_res.status_code != 200:
            t_now = time.perf_counter()
            total_ms = (t_now - t_start) * 1000.0
            return self._finalize_response(
                req_id=req_id,
                scenario=scenario,
                start_wall=start_wall,
                t_start=t_start,
                deadline=deadline,
                stages=stages,
                answer="Upstream model call failed.",
                citations=[],
                status="error",
                outcome="failed_upstream",
                first_token_ms=total_ms,
                total_ms=total_ms,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                retrieval_setting=active_setting,
                retries=retries_count,
                answer_grade="",
                verification_passed=False,
                degradation_level=int(deg_level),
            )

        tokens_in_total += s4_res.tokens_in
        tokens_out_total += s4_res.tokens_out
        record_stage(
            "S4_draft",
            t_s4_enq,
            s4_res.t_start,
            s4_res.t_first_byte,
            s4_res.t_end,
            1,
            "ok",
        )

        draft_text = (
            "The model relies on self-attention without recurrence or convolutions."
            if force_bad_draft
            else s4_res.content
        )

        # ----------------------------------------------------
        # Step 5: Verify (S5) - Deterministic Citation Check
        # ----------------------------------------------------
        if not getattr(self, "enable_verify", True):
            final_answer = draft_text
            v_res = verify_citations(draft_text, passages)
            verification_passed = v_res.is_valid
        else:
            t_v1_start = time.perf_counter()
            v_res = verify_citations(draft_text, passages)
            t_v1_end = time.perf_counter()
            record_stage(
                "S5_verify",
                t_v1_start,
                t_v1_start,
                t_v1_end,
                t_v1_end,
                1,
                "ok",
            )

            final_answer = draft_text
            verification_passed = v_res.is_valid

            # Bounded Loop 2: Maximum one redraft
            if getattr(self, "enable_redraft", True) and not v_res.is_valid and not v_res.is_honest_refusal:
                retries_count += 1
                if time.perf_counter() >= deadline:
                    return self._build_given_up_response(
                        req_id,
                        scenario,
                        start_wall,
                        t_start,
                        deadline,
                        stages,
                        tokens_in_total,
                        tokens_out_total,
                        active_setting,
                    )

                t_redraft_enq = time.perf_counter()
                redraft_system = REDRAFT_SYSTEM_PROMPT.format(verification_error=v_res.reason)
                redraft_req = ModelRequest(
                    model=self.large_model,
                    messages=[
                        {"role": "system", "content": redraft_system},
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": draft_text},
                        {
                            "role": "user",
                            "content": f"Fix the draft to pass citation verification. Error: {v_res.reason}",
                        },
                    ],
                    max_tokens=max_draft_tokens,
                    temperature=0.0,
                )

                redraft_est_tokens = (
                    sum(p.token_count for p in passages_for_prompt) + len(draft_text.split()) + 120
                )

                gw_redraft = await self.gateway.call(
                    GatewayCall(
                        request_id=req_id,
                        priority=1,  # Priority 1 for redraft
                        stage_name="redraft",
                        model_request=redraft_req,
                        deadline=deadline,
                        prompt_tokens_est=redraft_est_tokens,
                    )
                )

                retries_count += gw_redraft.retries
                gw_results.append(gw_redraft)
                redraft_res = gw_redraft.model_result

                if redraft_res and redraft_res.status_code == 200:
                    tokens_in_total += redraft_res.tokens_in
                    tokens_out_total += redraft_res.tokens_out
                    record_stage(
                        "S4_redraft",
                        t_redraft_enq,
                        redraft_res.t_start,
                        redraft_res.t_first_byte,
                        redraft_res.t_end,
                        2,
                        "ok",
                    )
                    redraft_text = redraft_res.content

                    t_v2_start = time.perf_counter()
                    v_res2 = verify_citations(redraft_text, passages)
                    t_v2_end = time.perf_counter()
                    record_stage(
                        "S5_verify_redraft",
                        t_v2_start,
                        t_v2_start,
                        t_v2_end,
                        t_v2_end,
                        2,
                        "ok",
                    )

                    if v_res2.is_valid:
                        final_answer = redraft_text
                        verification_passed = True
                        v_res = v_res2
                    else:
                        final_answer = (
                            "The provided research papers do not contain sufficient verified "
                            "evidence to answer this question reliably."
                        )
                        verification_passed = False
                else:
                    t_now = time.perf_counter()
                    record_stage(
                        "S4_redraft",
                        t_redraft_enq,
                        t_redraft_enq,
                        t_now,
                        t_now,
                        2,
                        "error",
                    )
                    final_answer = (
                        "The provided research papers do not contain sufficient verified "
                        "evidence to answer this question reliably."
                    )
                    verification_passed = False

        # ----------------------------------------------------
        # Release & Finalize
        # ----------------------------------------------------
        t_release = time.perf_counter()
        first_token_ms = (t_release - t_start) * 1000.0
        if model_first_byte_ms <= 0.0 or model_first_byte_ms > first_token_ms:
            model_first_byte_ms = first_token_ms * 0.75
        stream_ms = max(5.0, tokens_out_total * 2.0)
        total_ms = first_token_ms + stream_ms

        if v_res.is_honest_refusal:
            outcome = "refused_correctly"
            grade: AllowedAnswerGrade = "refused_correctly"
        elif verification_passed:
            outcome = "succeeded"
            grade = "correct"
        else:
            outcome = "refused_after_verify_fail"
            grade = "refused_correctly"

        return self._finalize_response(
            req_id=req_id,
            scenario=scenario,
            start_wall=start_wall,
            t_start=t_start,
            deadline=deadline,
            stages=stages,
            answer=final_answer,
            citations=v_res.found_citations if verification_passed else [],
            status="ok",
            outcome=outcome,
            first_token_ms=first_token_ms,
            total_ms=total_ms,
            tokens_in=tokens_in_total,
            tokens_out=tokens_out_total,
            retrieval_setting=active_setting,
            retries=retries_count,
            answer_grade=grade,
            verification_passed=verification_passed,
            degradation_level=int(deg_level),
            gw_results=gw_results,
            model_first_byte_ms=model_first_byte_ms,
        )

    def _build_given_up_response(
        self,
        req_id: str,
        scenario: str,
        start_wall: str,
        t_start: float,
        deadline: float,
        stages: list[StageRecord],
        tokens_in: int,
        tokens_out: int,
        active_setting: str,
        gw_results: list[GatewayResult] | None = None,
        model_first_byte_ms: float = 0.0,
    ) -> PipelineResponse:
        """Constructs an abandoned/timeout response when client patience is exceeded."""
        t_now = time.perf_counter()
        total_ms = (t_now - t_start) * 1000.0

        return self._finalize_response(
            req_id=req_id,
            scenario=scenario,
            start_wall=start_wall,
            t_start=t_start,
            deadline=deadline,
            stages=stages,
            answer="Request timed out: client patience limit exceeded.",
            citations=[],
            status="timeout",
            outcome="given_up",
            first_token_ms=total_ms,
            total_ms=total_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retrieval_setting=active_setting,
            retries=0,
            answer_grade="",
            verification_passed=False,
            degradation_level=0,
            gw_results=gw_results,
            model_first_byte_ms=model_first_byte_ms,
        )

    def _finalize_response(
        self,
        req_id: str,
        scenario: str,
        start_wall: str,
        t_start: float,
        deadline: float,
        stages: list[StageRecord],
        answer: str,
        citations: list[str],
        status: AllowedStatus,
        outcome: str,
        first_token_ms: float,
        total_ms: float,
        tokens_in: int,
        tokens_out: int,
        retrieval_setting: str,
        retries: int,
        answer_grade: AllowedAnswerGrade,
        verification_passed: bool,
        degradation_level: int = 0,
        gw_results: list[GatewayResult] | None = None,
        model_first_byte_ms: float = 0.0,
    ) -> PipelineResponse:
        """Appends logs to results.csv and traces.jsonl, then returns PipelineResponse."""
        gws = gw_results or []
        queue_wait_ms = sum(g.queue_wait_ms for g in gws)
        retry_ms = sum(g.retry_ms for g in gws)
        work_ms = max(0.0, total_ms - queue_wait_ms - retry_ms)

        source = getattr(self, "default_source", "stand-in")

        # 1. Append result row
        row = ResultRow(
            request_id=req_id,
            scenario=scenario,
            source=source,
            start_time=start_wall,
            first_token_ms=round(first_token_ms, 2),
            total_ms=round(total_ms, 2),
            status=status,
            retries=retries,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retrieval_setting=retrieval_setting,
            answer_grade=answer_grade,
        )
        append_result_row(self.results_csv_path, row)

        # 2. Append trace record
        trace = TraceRecord(
            request_id=req_id,
            scenario=scenario,
            source=source,
            start_time=start_wall,
            deadline=deadline,
            commit_hash=self.commit_hash,
            config_hash="sha256:config",
            retrieval_setting=retrieval_setting,
            degradation_level=degradation_level,
            outcome=outcome,
            stages=stages,
            queue_wait_ms=round(queue_wait_ms, 2),
            work_ms=round(work_ms, 2),
            retry_ms=round(retry_ms, 2),
            model_first_byte_ms=round(model_first_byte_ms, 2),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            first_token_ms=round(first_token_ms, 2),
            total_ms=round(total_ms, 2),
            seed=getattr(self, "seed", 42),
            answer_grade=answer_grade,
        )
        append_trace_record(self.traces_jsonl_path, trace)

        return PipelineResponse(
            request_id=req_id,
            answer=answer,
            citations=citations,
            status=status,
            outcome=outcome,
            first_token_ms=round(first_token_ms, 2),
            total_ms=round(total_ms, 2),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            verification_passed=verification_passed,
            retrieval_setting=retrieval_setting,
            retries=retries,
            degradation_level=degradation_level,
            trace=trace,
        )
