from __future__ import annotations

import asyncio

import pytest

from nostr_hermes_proxy.hermes import (
    HermesError,
    HermesSessionGone,
    ModelOption,
    TurnResult,
)
from nostr_hermes_proxy.sessions import SessionMap

from .fakes import FakeAckPolicy, FakeBot, FakeCtx, make_harness


async def wait_for(predicate, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def test_non_allowlisted_sender_is_dropped(test_hex):
    harness = make_harness(allow_hex=test_hex)
    intruder = FakeCtx(sender_hex="ab" * 32, event_id="e")
    await harness.runtime.handle_dm(intruder, "hello")
    assert harness.bot.sent == []
    assert harness.hermes.run_calls == []
    assert harness.hermes.ensure_calls == []


async def test_non_command_is_forwarded_and_replied(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "  hello world  ")
    assert harness.hermes.run_calls == [
        ("atlas", "  hello world  ", "event-1"),
    ]
    assert harness.bot.sent[-1][1] == "hello"


async def test_missing_event_id_generates_one(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.ctx.event_id = None
    await harness.runtime.handle_dm(harness.ctx, "hello")
    generated = harness.hermes.run_calls[0][2]
    assert isinstance(generated, str) and len(generated) == 32


async def test_ack_is_armed_and_cancelled_for_turn(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.ack.armed == ["hello"]
    assert harness.ack.cancelled >= 1


async def test_help_command_is_local(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/HELP")
    assert harness.hermes.run_calls == []
    assert harness.ack.armed == []
    assert "Commands:" in harness.bot.sent[-1][1]


async def test_unknown_command_is_local(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/deploy now")
    assert harness.hermes.run_calls == []
    assert "Unknown command /deploy" in harness.bot.sent[-1][1]


async def test_model_list_command(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        models=[ModelOption("openrouter", "claude"), ModelOption(None, "llama")],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "- openrouter/claude" in reply
    assert "- llama" in reply
    assert harness.hermes.run_calls == []


async def test_model_set_command_persists(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/model openrouter/claude")
    entry = harness.sessions.get("atlas")
    assert entry.model == "claude"
    assert entry.provider == "openrouter"
    assert "Model set to openrouter/claude." in harness.bot.sent[-1][1]
    assert harness.hermes.run_calls == []


async def test_model_set_bad_usage(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/model /bad")
    assert "Usage:" in harness.bot.sent[-1][1]


async def test_new_command_swaps_session(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.sessions.set("atlas", "old-session", model="m", provider="p")
    await harness.runtime.handle_dm(harness.ctx, "/new")
    entry = harness.sessions.get("atlas")
    assert entry.hermes_session_id == "sess-1-new"
    assert entry.model == "m"
    assert entry.provider == "p"
    assert harness.hermes.create_calls == ["atlas"]
    assert "New conversation started." in harness.bot.sent[-1][1]


async def test_status_command(test_hex, clock):
    harness = make_harness(allow_hex=test_hex, clock=clock)
    harness.sessions.set("atlas", "sess-1", model="claude", provider="openrouter")
    harness.runtime.set_watchdog_status_provider(lambda: "ok")
    await harness.runtime.handle_dm(harness.ctx, "/status")
    reply = harness.bot.sent[-1][1]
    assert "Personality: atlas" in reply
    assert "Model: openrouter/claude" in reply
    assert "Session: not started" not in reply
    assert "Queue: 0 waiting" in reply
    assert "Relays: 2" in reply
    assert "Watchdog: ok" in reply
    assert harness.hermes.run_calls == []


async def test_turns_are_serialized_per_personality(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()
    assert harness.hermes.active == 1

    second_ctx = FakeCtx(sender_hex=test_hex, event_id="event-2")
    second = asyncio.create_task(
        harness.runtime.handle_dm(second_ctx, "two"),
    )
    await wait_for(lambda: harness.runtime.queue_depth == 1)
    assert len(harness.hermes.run_calls) == 1
    assert harness.hermes.max_active == 1

    harness.hermes.gate.set()
    await asyncio.gather(first, second)
    assert [call[1] for call in harness.hermes.run_calls] == ["one", "two"]
    assert harness.hermes.max_active == 1
    assert len(harness.bot.sent) == 2


async def test_hermes_error_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[HermesError("gateway down")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    reply = harness.bot.sent[-1][1]
    assert "Something went wrong talking to Hermes." in reply
    assert harness.ack.cancelled >= 1


async def test_empty_reply_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("   ", status="completed")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert "empty reply" in harness.bot.sent[-1][1]


async def test_failed_run_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("", status="failed", error="model exploded")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    reply = harness.bot.sent[-1][1]
    assert "model exploded" in reply


async def test_long_reply_is_chunked(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("x" * 5000, status="completed")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert len(harness.bot.sent) == 2
    assert harness.bot.sent[0][1].endswith(" (1/2)")
    assert harness.bot.sent[1][1].endswith(" (2/2)")


async def test_session_gone_is_recreated_and_retried(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[
            HermesSessionGone("gone"),
            TurnResult("recovered", status="completed"),
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert len(harness.hermes.run_calls) == 2
    assert harness.bot.sent[-1][1] == "recovered"
    assert harness.sessions.get("atlas").hermes_session_id is not None


async def test_command_reply_is_chunked_if_long(test_hex):
    options = [ModelOption("p", f"model-{index}") for index in range(500)]
    harness = make_harness(allow_hex=test_hex, models=options)
    await harness.runtime.handle_dm(harness.ctx, "/model")
    assert len(harness.bot.sent) >= 1
    assert all(len(text) <= 4096 for _, text in harness.bot.sent)


async def test_start_and_stop_are_delegated(test_hex):
    bot = FakeBot()
    harness = make_harness(allow_hex=test_hex, bot=bot)
    await harness.runtime.start()
    assert bot.started == 1
    assert bot.dm_handler == harness.runtime.handle_dm
    await harness.runtime.stop()
    assert bot.stopped == 1
