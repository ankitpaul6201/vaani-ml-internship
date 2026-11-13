"""src/backends.py - Unified backend interface for real Groq and local stand-in.

Requirements:
- EI-01: OpenAI-style chat completions interface with streaming.
- EI-02: Base URL selectable by configuration.
- EI-06: API key read from environment variable, never logged or committed.
- Accurate monotonic stage timings (t_enqueue, t_start, t_first_byte, t_end).
- Rate limit header extraction and usage token extraction.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()


@dataclass
class ModelRequest:
    """Request payload sent to chat completions API."""

    model: str
    messages: list[dict[str, str]]
    max_tokens: int = 512
    temperature: float = 0.0
    stream: bool = True


@dataclass
class ModelResult:
    """Standardized response from backend execution."""

    content: str
    reasoning: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    t_enqueue: float = 0.0  # monotonic seconds
    t_start: float = 0.0  # monotonic seconds
    t_first_byte: float = 0.0  # monotonic seconds
    t_end: float = 0.0  # monotonic seconds
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def latency_ms(self) -> float:
        if self.t_start > 0 and self.t_end > 0:
            return (self.t_end - self.t_start) * 1000.0
        return 0.0

    @property
    def ttft_ms(self) -> float:
        if self.t_start > 0 and self.t_first_byte > 0:
            return (self.t_first_byte - self.t_start) * 1000.0
        return 0.0


def extract_rate_limit_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
    """Extracts rate-limit and retry headers from response without leaking auth."""
    rate_keys = (
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "retry-after",
    )
    result = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in rate_keys or "ratelimit" in k_lower or "retry" in k_lower:
            result[k_lower] = v
    return result


def parse_retry_delay(headers: dict[str, str], default_s: float = 1.0) -> float:
    """Parses retry delay in seconds from retry-after or x-ratelimit-reset headers."""
    # 1. Standard retry-after
    if "retry-after" in headers:
        try:
            return max(0.1, float(headers["retry-after"]) + 0.25)
        except ValueError:
            pass

    # 2. Groq x-ratelimit-reset-tokens (e.g., '622ms', '1.5s', '11.137s')
    reset_tok = headers.get("x-ratelimit-reset-tokens", "")
    if reset_tok:
        try:
            if reset_tok.endswith("ms"):
                return max(0.1, (float(reset_tok[:-2]) / 1000.0) + 0.25)
            elif reset_tok.endswith("s"):
                return max(0.1, float(reset_tok[:-1]) + 0.25)
        except ValueError:
            pass

    return default_s


class BackendClient:
    """Async client communicating with OpenAI-compatible chat backends."""

    def __init__(
        self,
        base_url: str = "https://api.groq.com/openai/v1",
        api_key: str | None = None,
        timeout: float = 12.0,
    ):
        self.base_url = base_url.rstrip("/")
        # Key must never be hard-coded; fetch from env if not passed
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.timeout = timeout

    async def stream_chat(
        self,
        req: ModelRequest,
        t_enqueue: float | None = None,
        timeout: float | None = None,
    ) -> ModelResult:
        """Sends a streaming chat completion request and records performance metrics."""
        t_enq = t_enqueue if t_enqueue is not None else time.perf_counter()
        t_start = time.perf_counter()
        t_first_byte = 0.0

        effective_timeout = timeout if timeout is not None else self.timeout
        client_timeout = httpx.Timeout(effective_timeout, connect=min(5.0, effective_timeout))

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": req.model,
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": req.stream,
            "stream_options": {"include_usage": True},
        }

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        response_headers: dict[str, str] = {}
        status_code = 200

        try:
            async with httpx.AsyncClient(timeout=client_timeout) as client:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                    status_code = resp.status_code
                    response_headers = extract_rate_limit_headers(resp.headers)

                    if status_code != 200:
                        err_body = await resp.aread()
                        t_end = time.perf_counter()
                        return ModelResult(
                            content="",
                            tokens_in=0,
                            tokens_out=0,
                            t_enqueue=t_enq,
                            t_start=t_start,
                            t_first_byte=t_end,
                            t_end=t_end,
                            status_code=status_code,
                            headers=response_headers,
                            error=f"HTTP {status_code}: {err_body.decode('utf-8', errors='replace')}",
                        )

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue

                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break

                            try:
                                chunk_json = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            # Capture usage if provided in final chunk
                            usage = chunk_json.get("usage")
                            if usage:
                                tokens_in = usage.get("prompt_tokens", tokens_in)
                                tokens_out = usage.get("completion_tokens", tokens_out)

                            choices = chunk_json.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                if "content" in delta and delta["content"]:
                                    if t_first_byte == 0.0:
                                        t_first_byte = time.perf_counter()
                                    content_parts.append(delta["content"])
                                if "reasoning" in delta and delta["reasoning"]:
                                    if t_first_byte == 0.0:
                                        t_first_byte = time.perf_counter()
                                    reasoning_parts.append(delta["reasoning"])

            t_end = time.perf_counter()
            if t_first_byte == 0.0:
                t_first_byte = t_end

            full_content = "".join(content_parts).strip()
            # If provider didn't emit usage, estimate tokens from content
            if tokens_out == 0 and full_content:
                tokens_out = max(1, int(len(full_content.split()) * 1.3))
            if tokens_in == 0:
                prompt_text = " ".join(m.get("content", "") for m in req.messages)
                tokens_in = max(1, int(len(prompt_text.split()) * 1.3))

            return ModelResult(
                content=full_content,
                reasoning="".join(reasoning_parts).strip(),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                t_enqueue=t_enq,
                t_start=t_start,
                t_first_byte=t_first_byte,
                t_end=t_end,
                status_code=status_code,
                headers=response_headers,
            )

        except Exception as exc:
            t_end = time.perf_counter()
            return ModelResult(
                content="",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                t_enqueue=t_enq,
                t_start=t_start,
                t_first_byte=t_end,
                t_end=t_end,
                status_code=500,
                headers=response_headers,
                error=str(exc),
            )
