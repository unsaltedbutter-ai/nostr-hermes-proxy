from __future__ import annotations

import json

import httpx
import pytest

from nostr_hermes_proxy.hermes import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    HermesAuthError,
    HermesClient,
    HermesConflictError,
    HermesRateLimitError,
    HermesSessionGone,
)
from nostr_hermes_proxy.sessions import SessionMap


def sse(*events: tuple[str, object] | tuple[str, None]) -> bytes:
    parts: list[str] = []
    for name, data in events:
        parts.append(f"event: {name}\n")
        if data is not None:
            parts.append(f"data: {json.dumps(data)}\n")
        parts.append("\n")
    return "".join(parts).encode()


@pytest.fixture
def sessions(tmp_path):
    session_map = SessionMap(tmp_path / "sessions.json")
    session_map.set("atlas", "sess-1", model="claude", provider="openrouter")
    return session_map


@pytest.fixture
async def client_factory(sessions):
    created: list[HermesClient] = []

    def make(handler, *, key: str = "test-key"):
        client = HermesClient(
            "http://127.0.0.1:8642",
            sessions,
            api_key=key,
            transport=httpx.MockTransport(handler),
        )
        created.append(client)
        return client

    yield make
    for client in created:
        await client.close()


async def test_stream_parses_deltas_tools_and_terminal(client_factory, sessions):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            content=sse(
                ("assistant.delta", {"text": "Hel"}),
                ("assistant.delta", {"text": "lo"}),
                ("tool.started", {"id": "t1", "tool": "search"}),
                (
                    "tool.completed",
                    {
                        "id": "t1",
                        "tool": "search",
                        "duration": 1.5,
                        "preview": "found it",
                    },
                ),
                ("tool.failed", {"tool": "shell", "error": "boom"}),
                ("run.completed", {"output": ""}),
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = client_factory(handler)
    result = await client.run_turn("atlas", "hi", "evt-1")

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "Hello"
    assert result.ok
    assert len(result.tool_summary) == 2
    search = result.tool_summary[0]
    assert search.tool == "search"
    assert search.duration == 1.5
    assert search.preview == "found it"
    assert not search.error
    assert result.tool_summary[1].tool == "shell"
    assert result.tool_summary[1].error

    headers = captured["headers"]
    assert headers["idempotency-key"] == "evt-1"
    assert headers["x-hermes-session-key"] == "nostr-hermes-proxy:atlas"
    assert headers["authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "input": "hi", "model": "claude", "provider": "openrouter",
    }
    assert captured["url"].endswith("/api/sessions/sess-1/chat/stream")


async def test_keepalive_comments_are_skipped(client_factory):
    body = (
        b": keepalive\n\n"
        b"event: assistant.delta\ndata: {\"text\": \"a\"}\n\n"
        b": keepalive\n\n"
        b"event: assistant.delta\ndata: {\"text\": \"b\"}\n\n"
        b"event: run.completed\ndata: {}\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=body)

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.final_text == "ab"
    assert result.status == STATUS_COMPLETED


async def test_data_type_fallback_without_event_line(client_factory):
    body = (
        b"data: {\"type\": \"assistant.delta\", \"text\": \"hi\"}\n\n"
        b"data: {\"type\": \"run.completed\"}\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=body)

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.final_text == "hi"
    assert result.status == STATUS_COMPLETED


async def test_run_failed_status(client_factory):
    def handler(request):
        return httpx.Response(
            200,
            content=sse(("run.failed", {"message": "model exploded"})),
        )

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_FAILED
    assert result.error == "model exploded"
    assert not result.ok


async def test_run_cancelled_status(client_factory):
    def handler(request):
        return httpx.Response(200, content=sse(("run.cancelled", {})))

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_CANCELLED
    assert result.error == "run cancelled"


async def test_missing_terminal_is_incomplete(client_factory):
    def handler(request):
        return httpx.Response(
            200, content=sse(("assistant.delta", {"text": "partial"})),
        )

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_INCOMPLETE
    assert result.final_text == "partial"


async def test_404_raises_session_gone(client_factory):
    def handler(request):
        return httpx.Response(404, json={"error": "not_found"})

    with pytest.raises(HermesSessionGone):
        await client_factory(handler).run_turn("atlas", "x", "e")


async def test_429_carries_retry_after(client_factory):
    def handler(request):
        return httpx.Response(
            429, json={"error": "busy"}, headers={"Retry-After": "7"},
        )

    with pytest.raises(HermesRateLimitError) as excinfo:
        await client_factory(handler).run_turn("atlas", "x", "e")
    assert excinfo.value.retry_after == 7.0


async def test_401_is_auth_error(client_factory):
    def handler(request):
        return httpx.Response(401, text="bad key")

    with pytest.raises(HermesAuthError):
        await client_factory(handler).run_turn("atlas", "x", "e")


async def test_409_is_conflict(client_factory):
    def handler(request):
        return httpx.Response(409, text="conflict")

    with pytest.raises(HermesConflictError):
        await client_factory(handler).run_turn("atlas", "x", "e")


async def test_run_turn_without_mapped_session(client_factory, tmp_path):
    empty = SessionMap(tmp_path / "empty.json")
    client = HermesClient(
        "http://x", empty, api_key="k",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    try:
        with pytest.raises(HermesSessionGone):
            await client.run_turn("atlas", "x", "e")
    finally:
        await client.close()


async def test_ensure_session_existing_is_reused(client_factory):
    calls: list[str] = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"id": "sess-1"})

    entry = await client_factory(handler).ensure_session("atlas")
    assert entry.hermes_session_id == "sess-1"
    assert calls == ["GET /api/sessions/sess-1"]


async def test_ensure_session_recreates_on_404(client_factory, sessions):
    calls: list[str] = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(201, json={"session": {"id": "sess-new"}})

    entry = await client_factory(handler).ensure_session("atlas")
    assert entry.hermes_session_id == "sess-new"
    # Preferences survive the /new-equivalent recreation.
    assert entry.model == "claude"
    assert entry.provider == "openrouter"
    assert sessions.get("atlas").hermes_session_id == "sess-new"
    assert calls == ["GET /api/sessions/sess-1", "POST /api/sessions"]


async def test_create_session_title(client_factory):
    seen: dict[str, object] = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sess-new"})

    assert await client_factory(handler).create_session("atlas") == "sess-new"
    assert seen["body"] == {"title": "nostr:atlas"}


async def test_list_models_normalizes(client_factory):
    def handler(request):
        return httpx.Response(200, json={
            "providers": [
                {
                    "id": "openrouter",
                    "models": [{"id": "claude"}, {"id": "gpt"}],
                },
                {"name": "local", "models": ["llama"]},
            ],
            "models": [{"provider": "p", "model": "m"}],
        })

    options = await client_factory(handler).list_models()
    assert [(o.provider, o.model) for o in options] == [
        ("openrouter", "claude"),
        ("openrouter", "gpt"),
        ("local", "llama"),
        ("p", "m"),
    ]
    assert options[0].display == "openrouter/claude"


async def test_missing_api_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    with pytest.raises(HermesAuthError):
        HermesClient("http://x", SessionMap(tmp_path / "s.json"))
