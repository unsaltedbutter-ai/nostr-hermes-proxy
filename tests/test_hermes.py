from __future__ import annotations

import json

import httpx
import pytest

from nostr_hermes_proxy.hermes import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_INTERRUPTED,
    STATUS_REPLAYED,
    HermesAuthError,
    HermesClient,
    HermesConflictError,
    HermesError,
    HermesRateLimitError,
    HermesSessionGone,
    RunSubmission,
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


def runs_handler(
    *,
    run_id: str = "run-1",
    submit_status: int = 202,
    submit_headers: dict[str, str] | None = None,
    submit_body: dict | None = None,
    events: bytes | None = None,
    poll: dict | None = None,
    seen: dict | None = None,
):
    """Route POST /v1/runs, GET events, and GET run status."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            if seen is not None:
                seen["submit_headers"] = request.headers
                seen["submit_body"] = json.loads(request.content)
            body = submit_body if submit_body is not None else {
                "run_id": run_id, "status": "started",
            }
            return httpx.Response(
                submit_status, json=body, headers=submit_headers or {},
            )
        if request.method == "GET" and path == f"/v1/runs/{run_id}/events":
            if seen is not None:
                seen["events_headers"] = request.headers
            return httpx.Response(
                200,
                content=events if events is not None else b"",
                headers={"content-type": "text/event-stream"},
            )
        if request.method == "GET" and path == f"/v1/runs/{run_id}":
            if seen is not None:
                seen["poll_calls"] = seen.get("poll_calls", 0) + 1
            return httpx.Response(
                200, json=poll if poll is not None else {"status": "running"},
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    return handler


# -- submit + events happy path ------------------------------------------------


async def test_submit_and_stream_happy_path(client_factory):
    seen: dict = {}
    body = sse(
        ("message.delta", {"delta": "Hel"}),
        ("message.delta", {"delta": "lo"}),
        ("message.interim", {"text": "thinking out loud"}),
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
        ("run.completed", {"output": "Hello final"}),
    )
    handler = runs_handler(events=body, seen=seen)

    result = await client_factory(handler).run_turn("atlas", "hi", "evt-1")

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "Hello final"
    assert result.ok
    assert not result.replayed
    assert len(result.tool_summary) == 2
    search = result.tool_summary[0]
    assert search.tool == "search"
    assert search.duration == 1.5
    assert search.preview == "found it"
    assert not search.error
    assert result.tool_summary[1].tool == "shell"
    assert result.tool_summary[1].error

    submit_headers = seen["submit_headers"]
    assert submit_headers["idempotency-key"] == "evt-1"
    assert submit_headers["x-hermes-session-key"] == "nostr-hermes-proxy:atlas"
    assert submit_headers["authorization"] == "Bearer test-key"
    assert seen["submit_body"] == {
        "input": "hi",
        "session_id": "sess-1",
        "model": "claude",
        "provider": "openrouter",
    }
    assert seen["events_headers"]["accept"] == "text/event-stream"


async def test_submit_omits_model_when_unset(client_factory, tmp_path):
    session_map = SessionMap(tmp_path / "plain.json")
    session_map.set("atlas", "sess-1")
    seen: dict = {}

    def handler(request):
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(202, json={"run_id": "run-1"})
        return httpx.Response(200, content=sse(("run.completed", {"output": "ok"})))

    client = HermesClient(
        "http://x", session_map, api_key="k",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.run_turn("atlas", "hi", "e")
    finally:
        await client.close()
    assert seen["body"] == {"input": "hi", "session_id": "sess-1"}
    assert result.final_text == "ok"


async def test_terminal_output_overrides_accumulated_deltas(client_factory):
    body = sse(
        ("message.delta", {"delta": "partial"}),
        ("run.completed", {"output": "authoritative final"}),
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.final_text == "authoritative final"
    assert result.status == STATUS_COMPLETED


async def test_deltas_survive_empty_terminal_output(client_factory):
    body = sse(
        ("message.delta", {"delta": "Hel"}),
        ("message.delta", {"delta": "lo"}),
        ("run.completed", {"output": ""}),
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.final_text == "Hello"
    assert result.status == STATUS_COMPLETED


async def test_run_completed_usage_is_captured(client_factory):
    body = sse(
        ("run.completed", {
            "output": "done",
            "usage": {"input_tokens": 45230, "output_tokens": 120},
        }),
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.input_tokens == 45230
    assert result.status == STATUS_COMPLETED


async def test_run_completed_without_usage_yields_none(client_factory):
    body = sse(("run.completed", {"output": "done"}))
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.input_tokens is None


async def test_poll_fallback_captures_usage(client_factory):
    body = sse(("message.delta", {"delta": "partial"}))
    handler = runs_handler(
        events=body,
        poll={
            "status": "completed",
            "output": "final",
            "usage": {"input_tokens": 777},
        },
    )
    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_COMPLETED
    assert result.input_tokens == 777


async def test_keepalive_comments_are_skipped(client_factory):
    body = (
        b": keepalive\n\n"
        b"event: message.delta\ndata: {\"delta\": \"a\"}\n\n"
        b": keepalive\n\n"
        b"event: message.delta\ndata: {\"delta\": \"b\"}\n\n"
        b"event: run.completed\ndata: {}\n\n"
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.final_text == "ab"
    assert result.status == STATUS_COMPLETED


async def test_data_type_fallback_without_event_line(client_factory):
    body = (
        b'data: {"event": "message.delta", "delta": "hi"}\n\n'
        b'data: {"event": "run.completed", "output": "hi there"}\n\n'
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.final_text == "hi there"
    assert result.status == STATUS_COMPLETED


async def test_subagent_events_are_ignored(client_factory):
    body = sse(
        ("subagent.start", {"name": "worker"}),
        ("subagent.complete", {"name": "worker"}),
        ("message.delta", {"delta": "ok"}),
        ("run.completed", {"output": "ok"}),
    )
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.final_text == "ok"
    assert result.tool_summary == []


# -- terminal failures ---------------------------------------------------------


async def test_run_failed_status(client_factory):
    body = sse(("run.failed", {"error": "model exploded"}))
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.status == STATUS_FAILED
    assert result.error == "model exploded"
    assert not result.ok


async def test_run_cancelled_status(client_factory):
    body = sse(("run.cancelled", {}))
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.status == STATUS_CANCELLED
    assert result.error == "run cancelled"


async def test_run_interrupted_status(client_factory):
    body = sse(("run.interrupted", {"message": "gateway restarted"}))
    result = await client_factory(runs_handler(events=body)).run_turn(
        "atlas", "x", "e",
    )
    assert result.status == STATUS_INTERRUPTED
    assert result.error == "gateway restarted"


# -- poll fallback -------------------------------------------------------------


async def test_stream_without_terminal_polls_for_output(client_factory):
    seen: dict = {}
    body = sse(("message.delta", {"delta": "partial"}))
    handler = runs_handler(
        events=body,
        poll={"status": "completed", "output": "final from poll"},
        seen=seen,
    )
    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert seen["poll_calls"] == 1
    assert result.final_text == "final from poll"
    assert result.status == STATUS_COMPLETED


async def test_poll_fallback_maps_failed_status(client_factory):
    body = sse(("message.delta", {"delta": "partial"}))
    handler = runs_handler(
        events=body,
        poll={"status": "failed", "error": "provider timeout"},
    )
    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_FAILED
    assert result.error == "provider timeout"
    assert result.final_text == "partial"


async def test_stream_without_terminal_and_running_poll_is_incomplete(
    client_factory,
):
    body = sse(("message.delta", {"delta": "partial"}))
    handler = runs_handler(events=body, poll={"status": "running"})
    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.status == STATUS_INCOMPLETE
    assert result.final_text == "partial"


# -- replay sentinel -----------------------------------------------------------


async def test_replayed_duplicate_returns_sentinel(client_factory):
    calls: list[str] = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(
            202,
            json={"run_id": "run-1", "status": "completed", "replayed": True},
            headers={"Idempotency-Replayed": "true"},
        )

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.replayed
    assert result.status == STATUS_REPLAYED
    assert not result.ok
    # No events stream / poll for a replay.
    assert calls == ["POST /v1/runs"]


async def test_replayed_header_alone_is_enough(client_factory):
    def handler(request):
        return httpx.Response(
            202,
            json={"run_id": "run-1", "status": "started"},
            headers={"Idempotency-Replayed": "true"},
        )

    result = await client_factory(handler).run_turn("atlas", "x", "e")
    assert result.replayed


async def test_fresh_202_is_not_a_replay(client_factory):
    """Gateway v0.22.5 returns 202 for fresh submissions too."""
    body = sse(("run.completed", {"output": "fresh reply"}))
    result = await client_factory(
        runs_handler(submit_status=202, events=body),
    ).run_turn("atlas", "x", "e")
    assert not result.replayed
    assert result.status == STATUS_COMPLETED
    assert result.final_text == "fresh reply"


# -- steering ------------------------------------------------------------------


async def test_steer_accepted(client_factory):
    seen: dict = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accepted": True})

    assert await client_factory(handler).steer("run-1", "more context") is True
    assert seen["path"] == "/v1/runs/run-1/steer"
    assert seen["body"] == {"input": "more context"}


@pytest.mark.parametrize(
    "status_code, body",
    [
        (409, {"error": "run already completed"}),
        (404, {"error": "unknown run"}),
        (200, {"accepted": False}),
        (200, {"accepted": "true"}),
        (200, ["not", "a", "dict"]),
    ],
)
async def test_steer_refused_returns_false(client_factory, status_code, body):
    def handler(request):
        return httpx.Response(status_code, json=body)

    assert await client_factory(handler).steer("run-1", "x") is False


async def test_steer_network_error_returns_false(client_factory):
    def handler(request):
        raise httpx.ConnectError("gateway down", request=request)

    assert await client_factory(handler).steer("run-1", "x") is False


# -- submission errors ---------------------------------------------------------


async def test_404_on_submit_raises_session_gone(client_factory):
    def handler(request):
        return httpx.Response(404, json={"error": "not_found"})

    with pytest.raises(HermesSessionGone):
        await client_factory(handler).run_turn("atlas", "x", "e")


async def test_429_on_submit_carries_retry_after(client_factory):
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


async def test_submit_without_run_id_is_an_error(client_factory):
    def handler(request):
        return httpx.Response(202, json={"status": "started"})

    with pytest.raises(HermesError) as excinfo:
        await client_factory(handler).run_turn("atlas", "x", "e")
    assert "run id" in str(excinfo.value)


async def test_submit_run_without_mapped_session(client_factory, tmp_path):
    empty = SessionMap(tmp_path / "empty.json")

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request expected")

    client = HermesClient(
        "http://x", empty, api_key="k",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(HermesSessionGone):
            await client.submit_run("atlas", "x", "e")
    finally:
        await client.close()


# -- sessions / models ---------------------------------------------------------


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
    seen: dict = {}

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
    assert all(not option.user_defined for option in options)


async def test_list_models_reads_slug_and_user_defined(client_factory):
    def handler(request):
        return httpx.Response(200, json={
            "providers": [
                {
                    "slug": "aspark",
                    "name": "Aspark",
                    "is_user_defined": True,
                    "models": ["glm-5.3-flash", "glm-5.3"],
                },
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "is_user_defined": False,
                    "models": ["claude"],
                },
            ],
            "model": "glm-5.3-flash",
            "provider": "aspark",
        })

    options = await client_factory(handler).list_models()
    assert [(o.provider, o.model, o.user_defined) for o in options] == [
        ("aspark", "glm-5.3-flash", True),
        ("aspark", "glm-5.3", True),
        ("openrouter", "claude", False),
    ]


async def test_missing_api_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    with pytest.raises(HermesAuthError):
        HermesClient("http://x", SessionMap(tmp_path / "s.json"))


def test_run_submission_defaults():
    submission = RunSubmission(run_id="run-1")
    assert submission.run_id == "run-1"
    assert submission.replayed is False
