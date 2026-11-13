"""standin/server.py - FastAPI stand-in model server imitating Groq OpenAI-compatible API.

Requirements:
- EI-01, EI-03: OpenAI-compatible /v1/chat/completions endpoint supporting streaming and token usage.
- Controllable failure modes: 429 rate limits with retry-after and rate-limit headers, 5xx errors.
- Concurrency slowdown: Latency rises as in-flight requests increase.
- Calibrated latencies: Samples TTFT from fitted log-normal distribution.
- Admin endpoints (TRD 4.3):
  - POST /admin/config: Update parameters on the fly without restarting.
  - GET /admin/stats: Inspect in-flight requests, limits remaining, tokens served.
  - POST /admin/reset: Clear counters and reset state between runs.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

repo_root = Path(__file__).resolve().parent.parent
CALIBRATION_FILE = repo_root / "standin" / "calibration.json"


def load_calibration_params() -> dict[str, Any]:
    """Loads calibrated parameters from disk or provides defaults."""
    if CALIBRATION_FILE.is_file():
        try:
            with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "lognormal_mu": 5.27,
        "lognormal_sigma": 0.34,
        "ms_per_token": 3.94,
        "concurrency_penalty": 0.35,
        "rpm_limit": 1000,
        "tpm_limit": 8000,
        "rpd_limit": 1000,
        "tpd_limit": 100000,
    }


class StandinState:
    """Thread-safe mutable state for the stand-in server."""

    def __init__(self):
        self.calib = load_calibration_params()

        # Token buckets
        self.rpm_limit = float(self.calib.get("rpm_limit", 1000))
        self.tpm_limit = float(self.calib.get("tpm_limit", 8000))
        self.rpd_limit = float(self.calib.get("rpd_limit", 1000))
        self.tpd_limit = float(self.calib.get("tpd_limit", 100000))

        self.remaining_rpm = self.rpm_limit
        self.remaining_tpm = self.tpm_limit
        self.remaining_daily_requests = self.rpd_limit
        self.remaining_daily_tokens = self.tpd_limit
        self.last_refill = time.perf_counter()

        # In-flight concurrency
        self.in_flight = 0
        self.requests_served = 0
        self.tokens_served = 0

        # Controllable faults
        self.failure_rate = 0.0  # Fraction of requests that fail with 500
        self.forced_429 = False  # Immediately return 429
        self.concurrency_penalty = float(self.calib.get("concurrency_penalty", 0.35))
        self.mu = float(self.calib.get("lognormal_mu", 5.27))
        self.sigma = float(self.calib.get("lognormal_sigma", 0.34))
        self.ms_per_token = float(self.calib.get("ms_per_token", 3.94))
        self.latency_multiplier = 1.0

    def refill_buckets(self) -> None:
        """Refills per-minute token buckets proportionally to elapsed time."""
        now = time.perf_counter()
        elapsed = now - self.last_refill
        self.last_refill = now

        # RPM refill: rpm_limit / 60 tokens per second
        self.remaining_rpm = min(
            self.rpm_limit, self.remaining_rpm + elapsed * (self.rpm_limit / 60.0)
        )
        # TPM refill: tpm_limit / 60 tokens per second
        self.remaining_tpm = min(
            self.tpm_limit, self.remaining_tpm + elapsed * (self.tpm_limit / 60.0)
        )

    def reset(self) -> None:
        """Resets all metrics, buckets, and state."""
        self.remaining_rpm = self.rpm_limit
        self.remaining_tpm = self.tpm_limit
        self.remaining_daily_requests = self.rpd_limit
        self.remaining_daily_tokens = self.tpd_limit
        self.in_flight = 0
        self.requests_served = 0
        self.tokens_served = 0
        self.forced_429 = False
        self.failure_rate = 0.0
        self.last_refill = time.perf_counter()


state = StandinState()
app = FastAPI(title="The Relay Stand-in Model Server", version="1.0.0")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "qwen/qwen3.8-27b"
    messages: list[ChatMessage]
    max_tokens: int = 256
    temperature: float = 0.0
    stream: bool = False


class AdminConfigUpdate(BaseModel):
    rpm_limit: float | None = None
    tpm_limit: float | None = None
    failure_rate: float | None = None
    forced_429: bool | None = None
    concurrency_penalty: float | None = None
    latency_multiplier: float | None = None


def generate_synthetic_completion(messages: list[ChatMessage], max_tokens: int) -> tuple[str, int]:
    """Generates realistic responses for The Relay pipeline."""
    user_msg = next((m.content for m in reversed(messages) if m.role == "user"), "")
    sys_msg = next((m.content for m in messages if m.role == "system"), "")

    # 1. S1 Understand response
    if "query analysis module" in sys_msg.lower() or "search_query" in sys_msg:
        lower_u = user_msg.lower()
        if any(
            w in lower_u
            for w in [
                "resnet",
                "imagenet",
                "apple watch",
                "battery",
                "gpt-4",
                "ultrasound",
                "librispeech",
                "quantum",
            ]
        ):
            body = json.dumps({"search_query": "out of scope query", "scope": "off_topic"})
        else:
            clean_q = user_msg.replace("User Question:", "").strip()
            body = json.dumps({"search_query": clean_q, "scope": "in_scope"})
        tok_out = max(1, int(len(body.split()) * 1.3))
        return body, tok_out

    # 2. S4 Draft response with citation grounding
    lower_u = user_msg.lower()
    import re
    cids = re.findall(r"\[(\d{4}\.\d{4,5}v\d+\-p\d+\-[sm]\d{3})\]", user_msg)
    def pick_cid(prefix: str, fallback: str) -> str:
        for c in cids:
            if c.startswith(prefix):
                return c
        return cids[0] if cids else fallback

    if any(
        w in lower_u
        for w in [
            "resnet",
            "quantum",
            "apple watch",
            "battery",
            "gpt-4",
            "ultrasound",
            "librispeech",
        ]
    ):
        text = "The provided research papers do not contain sufficient information to answer this question."
    elif "attention heads" in lower_u:
        cid = pick_cid("1706.03762", "1706.03762v7-p5-m015")
        text = f"The base Transformer employs 8 parallel attention heads [{cid}]."
    elif "mimi" in lower_u or "audio codec" in lower_u:
        cid = pick_cid("2410.00037", "2410.00037v1-p7-m014")
        text = f"Moshi uses Mimi, a neural audio codec operating at 12.5 Hz [{cid}]."
    elif "h-net" in lower_u or "dynamic chunking" in lower_u:
        cid = pick_cid("2507.07955", "2507.07955v2-p2-m003")
        text = f"Dynamic Chunking introduces H-Net, a hierarchical sequence modeling architecture [{cid}]."
    else:
        cid = cids[0] if cids else "1706.03762v7-p5-m015"
        text = f"According to the research paper, the documented architecture satisfies these requirements [{cid}]."

    tok_out = max(1, int(len(text.split()) * 1.3))
    return text, tok_out


def build_rate_limit_headers() -> dict[str, str]:
    """Generates standard Groq rate limit headers from current state."""
    state.refill_buckets()
    return {
        "x-ratelimit-limit-requests": str(int(state.rpm_limit)),
        "x-ratelimit-limit-tokens": str(int(state.tpm_limit)),
        "x-ratelimit-remaining-requests": str(max(0, int(state.remaining_rpm))),
        "x-ratelimit-remaining-tokens": str(max(0, int(state.remaining_tpm))),
        "x-ratelimit-reset-requests": "1m0.0s",
        "x-ratelimit-reset-tokens": f"{max(0.1, (state.tpm_limit - state.remaining_tpm) / (state.tpm_limit / 60.0)):.3f}s",
    }


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint."""
    state.refill_buckets()

    # Estimate input tokens
    prompt_text = " ".join(m.content for m in req.messages)
    prompt_tokens = max(1, int(len(prompt_text.split()) * 1.3))

    rate_headers = build_rate_limit_headers()

    # 1. Rate Limit Checks (429)
    if state.forced_429 or state.remaining_rpm < 1.0 or state.remaining_tpm < prompt_tokens:
        rate_headers["retry-after"] = "2"
        err_payload = {
            "error": {
                "message": (
                    f"Rate limit reached for model `{req.model}` on input tokens per minute (ITPM): "
                    f"Limit {int(state.tpm_limit)}, Used {int(state.tpm_limit - state.remaining_tpm)}, "
                    f"Requested {prompt_tokens}. Please try again in 2.0s."
                ),
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, content=err_payload, headers=rate_headers
        )

    # 2. Configurable 500 Server Error
    if state.failure_rate > 0.0 and random.random() < state.failure_rate:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"message": "Internal stand-in server error", "type": "server_error"}
            },
            headers=rate_headers,
        )

    # Reserve tokens
    state.remaining_rpm -= 1.0
    state.remaining_tpm -= prompt_tokens

    # Concurrency tracking
    state.in_flight += 1
    current_in_flight = state.in_flight

    try:
        # Sample TTFT from fitted log-normal distribution with concurrency slowdown
        raw_ttft = float(np.random.lognormal(state.mu, state.sigma))
        # Concurrency penalty
        concurrency_mult = 1.0 + state.concurrency_penalty * max(0, current_in_flight - 1)
        ttft_ms = raw_ttft * concurrency_mult * state.latency_multiplier
        ttft_s = ttft_ms / 1000.0

        content_text, completion_tokens = generate_synthetic_completion(
            req.messages, req.max_tokens
        )
        total_tokens = prompt_tokens + completion_tokens

        # Generation duration
        gen_duration_s = (completion_tokens * state.ms_per_token) / 1000.0

        # Update stats
        state.requests_served += 1
        state.tokens_served += total_tokens

        if not req.stream:
            # Non-streaming response
            await asyncio.sleep(ttft_s + gen_duration_s)
            resp_body = {
                "id": f"chatcmpl-standin-{time.time_ns()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
            return JSONResponse(content=resp_body, headers=rate_headers)

        # Streaming response (SSE)
        async def sse_generator() -> AsyncIterator[str]:
            # Initial TTFT delay before first byte
            await asyncio.sleep(ttft_s)

            # Split into words for incremental streaming
            words = content_text.split()
            per_word_delay = gen_duration_s / max(1, len(words))

            for idx, word in enumerate(words):
                chunk_delta = word + " " if idx < len(words) - 1 else word
                chunk_payload = {
                    "id": f"chatcmpl-standin-{time.time_ns()}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk_delta},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk_payload)}\n\n"
                await asyncio.sleep(per_word_delay)

            # Final usage chunk
            final_payload = {
                "id": f"chatcmpl-standin-{time.time_ns()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
            yield f"data: {json.dumps(final_payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers=rate_headers,
        )

    finally:
        state.in_flight -= 1


# ---------------------------------------------------------------------------
# Admin API Endpoints (TRD 4.3)
# ---------------------------------------------------------------------------


@app.post("/admin/config")
async def update_admin_config(cfg: AdminConfigUpdate):
    """Updates latency and fault parameters without restarting server."""
    if cfg.rpm_limit is not None:
        state.rpm_limit = cfg.rpm_limit
    if cfg.tpm_limit is not None:
        state.tpm_limit = cfg.tpm_limit
    if cfg.failure_rate is not None:
        state.failure_rate = max(0.0, min(1.0, cfg.failure_rate))
    if cfg.forced_429 is not None:
        state.forced_429 = cfg.forced_429
    if cfg.concurrency_penalty is not None:
        state.concurrency_penalty = max(0.0, cfg.concurrency_penalty)
    if cfg.latency_multiplier is not None:
        state.latency_multiplier = max(0.1, cfg.latency_multiplier)

    return {
        "status": "updated",
        "current_config": {
            "rpm_limit": state.rpm_limit,
            "tpm_limit": state.tpm_limit,
            "failure_rate": state.failure_rate,
            "forced_429": state.forced_429,
            "concurrency_penalty": state.concurrency_penalty,
            "latency_multiplier": state.latency_multiplier,
        },
    }


@app.get("/admin/stats")
async def get_admin_stats():
    """Returns real-time server health and limit counters."""
    state.refill_buckets()
    return {
        "in_flight": state.in_flight,
        "requests_served": state.requests_served,
        "tokens_served": state.tokens_served,
        "remaining_rpm": round(state.remaining_rpm, 1),
        "remaining_tpm": round(state.remaining_tpm, 1),
        "rpm_limit": state.rpm_limit,
        "tpm_limit": state.tpm_limit,
    }


@app.post("/admin/reset")
async def reset_admin_state():
    """Resets counters, in-flight count, and token buckets between test runs."""
    state.reset()
    return {"status": "reset_successful", "in_flight": 0, "requests_served": 0}
