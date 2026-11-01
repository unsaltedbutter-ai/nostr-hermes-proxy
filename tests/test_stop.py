"""``/stop``: gateway cancellation plus the cancelled-turn lifecycle."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nostr_hermes_proxy.hermes import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    HermesClient,
    TurnResult,
)
from nostr_hermes_proxy.sessions import SessionMap

from .fakes import make_harness


# -- HermesClient.stop_run -----------------------------------------------------


@pytest.fixture
async def client_factory(tmp_path):
    created: list[HermesClient] = []

    def make(handler):
        client = HermesClient(
            "http://127.0.0.1:8642",
            SessionMap(tmp_path / "sessions.json"),
            api_key="test-key",
            transport=httpx.MockTransport(handler),
        )
        created.append(client)
        return client

    yield make
    for client in created:
        await client.close()


async def test_stop_run_posts_to_stop_and_returns_status(client_factory):
    seen: dict = {}

    def handler(request):
        seen["method"] = request.method
        seen["raw_path"] = request.url.raw_path
        seen["body"] = request.content
        return httpx.Response(200, json={"status": "stopping"})

    status = await client_factory(handler).stop_run("run/1")
    assert status == "stopping"
    assert seen["method"] == "POST"
    assert seen["raw_path"] == b"/v1/runs/run%2F1/stop"
    assert seen["body"] == b""


async def test_stop_run_returns_completed_when_run_finished(client_factory):
    def handler(request):
        return httpx.Response(200, json={"status": "completed"})

    assert await client_factory(handler).stop_run("run-1") == "completed"


@pytest.mark.parametrize(
    "status_code, body",
    [
        (404, {"error": "unknown run"}),
        (409, {"error": "run already completed"}),
        (500, {"error": "boom"}),
        (200, {"status": 123}),
        (200, {"no_status": True}),
        (200, ["not", "a", "dict"]),
    ],
)
async def test_stop_run_failures_return_none(client_factory, status_code, body):
    def handler(request):
        return httpx.Response(status_code, json=body)

    assert await client_factory(handler).stop_run("run-1") is None


async def test_stop_run_bad_json_returns_none(client_factory):
    def handler(request):
        return httpx.Response(200, content=b"not json")

    assert await client_factory(handler).stop_run("run-1") is None


async def test_stop_run_network_error_returns_none(client_factory):
    def handler(request):
        raise httpx.ConnectError("gateway down", request=request)

    assert await client_factory(handler).stop_run("run-1") is None


# -- /stop command -------------------------------------------------------------


async def start_in_flight(harness, text: str = "one"):
    """Hold one turn inside ``stream_run`` so ``_run_id`` is live."""
    harness.hermes.gate.clear()
    task = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, text))
    await harness.hermes.entered.wait()
    assert harness.runtime._run_id == "run-1"
    return task


async def test_stop_during_turn_calls_gateway_and_suppresses_the_reply():
    harness = make_harness(results=[TurnResult("", status=STATUS_CANCELLED)])
    task = await start_in_flight(harness)

    # The turn lock is held; commands are dispatched before it, so this
    # returns immediately rather than queueing behind the stream.
    await asyncio.wait_for(
        harness.runtime.handle_dm(harness.ctx, "/stop"), timeout=1,
    )

    assert harness.hermes.stops == ["run-1"]
    assert harness.bot.sent[-1][1] == "🛑 Stopped."

    harness.hermes.gate.set()
    await asyncio.wait_for(task, timeout=1)

    assert harness.runtime._run_id is None
    assert harness.runtime.queue_depth == 0
    # Only the DM's 👀 ack and the /stop confirmation; the cancelled turn
    # publishes neither a reply chunk nor an error text.
    assert [text for _, text in harness.bot.sent] == ["👀", "🛑 Stopped."]


async def test_stop_with_no_run_says_nothing_is_running():
    harness = make_harness()

    await harness.runtime.handle_dm(harness.ctx, "/STOP")

    assert harness.hermes.stops == []
    assert harness.hermes.submit_calls == []
    assert harness.bot.sent[-1][1] == "Nothing is running."


async def test_stop_reports_too_late_when_run_already_completed():
    harness = make_harness(
        stop_result="completed",
        results=[TurnResult("", status=STATUS_CANCELLED)],
    )
    task = await start_in_flight(harness)

    await asyncio.wait_for(
        harness.runtime.handle_dm(harness.ctx, "/stop"), timeout=1,
    )
    assert harness.hermes.stops == ["run-1"]
    assert harness.bot.sent[-1][1] == "Too late — it already finished."

    harness.hermes.gate.set()
    await asyncio.wait_for(task, timeout=1)


async def test_stop_reports_unreachable_gateway():
    harness = make_harness(
        stop_results=[None],
        results=[TurnResult("", status=STATUS_CANCELLED)],
    )
    task = await start_in_flight(harness)

    await asyncio.wait_for(
        harness.runtime.handle_dm(harness.ctx, "/stop"), timeout=1,
    )
    assert harness.hermes.stops == ["run-1"]
    assert (
        harness.bot.sent[-1][1]
        == "Couldn't reach the gateway to stop the run."
    )

    harness.hermes.gate.set()
    await asyncio.wait_for(task, timeout=1)


async def test_stop_run_exception_reports_unreachable_gateway():
    harness = make_harness(
        stop_results=[RuntimeError("gateway hiccup")],
        results=[TurnResult("", status=STATUS_CANCELLED)],
    )
    task = await start_in_flight(harness)

    await asyncio.wait_for(
        harness.runtime.handle_dm(harness.ctx, "/stop"), timeout=1,
    )
    assert (
        harness.bot.sent[-1][1]
        == "Couldn't reach the gateway to stop the run."
    )

    harness.hermes.gate.set()
    await asyncio.wait_for(task, timeout=1)


# -- cancelled-turn lifecycle --------------------------------------------------


async def test_cancelled_turn_sends_no_reply_and_releases_the_lock():
    harness = make_harness(
        results=[
            TurnResult("partial answer", status=STATUS_CANCELLED),
            TurnResult("second reply", status=STATUS_COMPLETED),
        ],
    )

    await harness.runtime.handle_dm(harness.ctx, "one")

    # Only the immediate ack: no reply, no error text for the cancelled run.
    assert [text for _, text in harness.bot.sent] == ["👀"]
    assert harness.runtime._run_id is None
    assert harness.runtime.queue_depth == 0
    assert harness.sessions.get("atlas").last_input_tokens is None

    # The lock was released, so the next DM runs normally.
    await harness.runtime.handle_dm(harness.ctx, "two")
    assert [text for _, text in harness.bot.sent] == [
        "👀", "👀", "second reply",
    ]
    assert [call[1] for call in harness.hermes.submit_calls] == ["one", "two"]


async def test_cancelled_run_does_not_update_token_usage():
    harness = make_harness(
        results=[
            TurnResult(
                "partial", status=STATUS_CANCELLED, input_tokens=1234,
            ),
        ],
    )

    await harness.runtime.handle_dm(harness.ctx, "one")

    entry = harness.sessions.get("atlas")
    assert entry is not None
    assert entry.last_input_tokens is None
