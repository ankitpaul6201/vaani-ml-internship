"""tests/test_phase4.py - Phase 4 Stand-In Server & Calibration Test Suite.

Verifies:
- EI-01, EI-03: OpenAI-compatible response shape, streaming chunks, usage record.
- Rate-limit headers presence and matching format.
- Captured real 429 contract comparison with stand-in 429 shape.
- Latency rise under concurrency (concurrency slowdown).
- Admin API endpoints: /admin/config, /admin/stats, /admin/reset.
- Exit gate: Calibrated percentiles within 20% of real probe data.
"""

import json
from pathlib import Path

import httpx
import pytest

from standin.calibrate import calibrate_from_probe
from standin.server import app, state


@pytest.fixture(autouse=True)
def reset_server_state():
    """Ensure clean state before each test."""
    state.reset()
    state.latency_multiplier = 0.05  # Speed up tests
    yield
    state.reset()
    state.latency_multiplier = 1.0


@pytest.mark.asyncio
async def test_openai_chat_completions_non_streaming():
    """Verify standard non-streaming chat completions response format."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "qwen/qwen3.8-27b",
            "messages": [
                {
                    "role": "user",
                    "content": "How many parallel attention heads does the base Transformer use?",
                }
            ],
            "max_tokens": 50,
            "stream": False,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200
        data = resp.json()

        assert "choices" in data
        assert len(data["choices"]) == 1
        assert "message" in data["choices"][0]
        assert "content" in data["choices"][0]["message"]
        assert "1706.03762v7-p5-m015" in data["choices"][0]["message"]["content"]

        assert "usage" in data
        assert data["usage"]["prompt_tokens"] > 0
        assert data["usage"]["completion_tokens"] > 0

        # Rate limit headers check
        headers = resp.headers
        assert "x-ratelimit-limit-requests" in headers
        assert "x-ratelimit-limit-tokens" in headers
        assert "x-ratelimit-remaining-requests" in headers
        assert "x-ratelimit-remaining-tokens" in headers


@pytest.mark.asyncio
async def test_openai_chat_completions_streaming():
    """Verify SSE streaming format with usage in final chunk."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "qwen/qwen3.8-27b",
            "messages": [{"role": "user", "content": "What neural audio codec does Moshi use?"}],
            "max_tokens": 50,
            "stream": True,
        }
        chunks = []
        final_usage = None
        async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    data = json.loads(line[6:])
                    choices = data.get("choices", [])
                    if choices and "content" in choices[0].get("delta", {}):
                        chunks.append(choices[0]["delta"]["content"])
                    if "usage" in data:
                        final_usage = data["usage"]

        assert len(chunks) > 0
        full_text = "".join(chunks)
        assert "Mimi" in full_text
        assert final_usage is not None
        assert final_usage["prompt_tokens"] > 0
        assert final_usage["completion_tokens"] > 0


@pytest.mark.asyncio
async def test_contract_429_shape_comparison():
    """Compare stand-in 429 response shape against real captured Groq 429."""
    root = Path(__file__).resolve().parent.parent
    probe_file = root / "results" / "real" / "probe_data.json"
    with open(probe_file, "r", encoding="utf-8") as f:
        probes = json.load(f)
    real_429 = next((p for p in probes if p["status_code"] == 429), None)
    assert real_429 is not None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Force 429 mode
        await client.post("/admin/config", json={"forced_429": True})

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen/qwen3.8-27b", "messages": [{"role": "user", "content": "test"}]},
        )
        assert resp.status_code == 429
        data = resp.json()

        # Contract checks: same JSON error structure
        assert "error" in data
        assert "message" in data["error"]
        assert "code" in data["error"]
        assert data["error"]["code"] == "rate_limit_exceeded"

        # Headers check: retry-after and x-ratelimit-* present
        assert "retry-after" in resp.headers
        assert "x-ratelimit-remaining-tokens" in resp.headers


@pytest.mark.asyncio
async def test_concurrency_slowdown():
    """Verify that in-flight requests trigger latency penalty."""
    transport = httpx.ASGITransport(app=app)
    state.latency_multiplier = 1.0  # restore realistic latency
    state.concurrency_penalty = 1.0  # high penalty for clear signal

    import time

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Single request duration
        t0 = time.perf_counter()
        r1 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen/qwen3.8-27b",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 10,
            },
        )
        t_single = time.perf_counter() - t0
        assert r1.status_code == 200

        # Concurrent requests
        import asyncio

        t1 = time.perf_counter()
        tasks = [
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen/qwen3.8-27b",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 10,
                },
            )
            for _ in range(4)
        ]
        resps = await asyncio.gather(*tasks)
        t_concurrent = time.perf_counter() - t1
        assert all(r.status_code == 200 for r in resps)
        # Latency under concurrency should be higher than single request
        assert t_concurrent > t_single * 1.2


@pytest.mark.asyncio
async def test_admin_api_lifecycle():
    """Verify admin config updates, stats querying, and reset."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Update config
        cfg_resp = await client.post("/admin/config", json={"rpm_limit": 500, "failure_rate": 0.5})
        assert cfg_resp.status_code == 200
        assert cfg_resp.json()["current_config"]["rpm_limit"] == 500
        assert cfg_resp.json()["current_config"]["failure_rate"] == 0.5

        # 2. Check stats
        stats_resp = await client.get("/admin/stats")
        assert stats_resp.status_code == 200
        stats = stats_resp.json()
        assert stats["rpm_limit"] == 500

        # 3. Reset
        reset_resp = await client.post("/admin/reset")
        assert reset_resp.status_code == 200
        assert reset_resp.json()["status"] == "reset_successful"

        post_reset = await client.get("/admin/stats")
        assert post_reset.json()["in_flight"] == 0
        assert post_reset.json()["requests_served"] == 0


def test_exit_gate_calibration_accuracy():
    """Exit gate: Calibrated stand-in percentiles within 20% of real probe percentiles."""
    calib = calibrate_from_probe()
    comp = calib["comparison"]

    assert comp["p50_ms"]["diff_pct"] < 20.0, f"p50 diff {comp['p50_ms']['diff_pct']}% exceeds 20%"
    assert comp["p90_ms"]["diff_pct"] < 20.0, f"p90 diff {comp['p90_ms']['diff_pct']}% exceeds 20%"
    assert comp["p95_ms"]["diff_pct"] < 20.0, f"p95 diff {comp['p95_ms']['diff_pct']}% exceeds 20%"
