"""Outbox: idempotency ledger, rate cap, FIFO worker, retries, dropping."""

from __future__ import annotations

import asyncio
import stat

import pytest

from nostr_hermes_proxy.chunking import truncate_message
from nostr_hermes_proxy.outbox import (
    IDEMPOTENCY_TTL_SECONDS,
    Outbox,
    RateLimited,
)


async def test_enqueue_chunks_once_and_returns_count():
    outbox = Outbox(None)
    message_id, chunks = outbox.enqueue("atlas", "x" * 5000, 604800)
    assert len(message_id) == 32
    assert chunks == 2
    queued = outbox.queued("atlas")
    assert len(queued) == 1
    assert queued[0].chunks == truncate_message("x" * 5000)


async def test_idempotent_response_is_returned_and_expires_after_24h(clock):
    outbox = Outbox(None, clock=clock)
    response = {"accepted": True, "message_id": "abc", "chunks": 1,
                "duplicate": False}
    outbox.remember_response("cron-key", response)
    assert outbox.idempotent_response("cron-key") == response

    clock.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    assert outbox.idempotent_response("cron-key") is None


async def test_idempotency_map_persists_across_reload(tmp_path, clock):
    path = tmp_path / "outbox.json"
    outbox = Outbox(path, clock=clock)
    response = {"accepted": True, "message_id": "abc", "chunks": 1}
    outbox.remember_response("cron-key", response)
    outbox.enqueue("atlas", "hello", 90)

    reloaded = Outbox(path, clock=clock)
    assert reloaded.idempotent_response("cron-key") == response
    assert reloaded.pending_count() == 1
    assert reloaded.queued()[0].text == "hello"

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


async def test_rate_cap_is_a_sliding_minute(clock):
    outbox = Outbox(None, clock=clock, rate_limit_per_minute=2)
    outbox.enqueue("atlas", "one", None)
    outbox.enqueue("atlas", "two", None)
    with pytest.raises(RateLimited):
        outbox.enqueue("atlas", "three", None)

    # A different personality has its own window.
    outbox.enqueue("hermes", "other", None)

    clock.advance(61)
    outbox.enqueue("atlas", "three", None)
    assert outbox.pending_count("atlas") == 3


async def test_worker_sends_queued_messages_in_fifo_order():
    outbox = Outbox(None)
    outbox.enqueue("atlas", "one", None)
    outbox.enqueue("atlas", "two", None)
    delivered: list[str] = []

    async def deliver(personality, text, expiry):
        delivered.append(text)
        return True

    result = await outbox.drain_once(deliver)
    assert delivered == ["one", "two"]
    assert result == {"sent": 2, "retried": 0, "dropped": 0}
    assert outbox.pending_count() == 0


async def test_failed_send_requeues_then_succeeds():
    outbox = Outbox(None, retry_backoff_seconds=0)
    outbox.enqueue("atlas", "hello", None)
    calls: list[str] = []

    async def deliver(personality, text, expiry):
        calls.append(text)
        return len(calls) > 1

    first = await outbox.drain_once(deliver)
    assert first == {"sent": 0, "retried": 1, "dropped": 0}
    assert outbox.pending_count() == 1
    assert outbox.queued()[0].attempts == 1

    second = await outbox.drain_once(deliver)
    assert second == {"sent": 1, "retried": 0, "dropped": 0}
    assert outbox.pending_count() == 0
    assert calls == ["hello", "hello"]


async def test_connection_down_keeps_message_queued():
    outbox = Outbox(None, retry_backoff_seconds=0)
    outbox.enqueue("atlas", "hello", None)

    async def deliver(personality, text, expiry):
        return False

    result = await outbox.drain_once(deliver)
    assert result["retried"] == 1
    assert outbox.pending_count() == 1


async def test_delivery_is_dropped_after_five_failed_attempts():
    outbox = Outbox(None, retry_backoff_seconds=0)
    outbox.enqueue("atlas", "hello", None)

    async def deliver(personality, text, expiry):
        return False

    results = [await outbox.drain_once(deliver) for _ in range(5)]
    assert [result["dropped"] for result in results] == [0, 0, 0, 0, 1]
    assert outbox.pending_count() == 0


async def test_failed_head_blocks_later_messages_to_preserve_order():
    outbox = Outbox(None, retry_backoff_seconds=60)
    outbox.enqueue("atlas", "one", None)
    outbox.enqueue("atlas", "two", None)
    delivered: list[str] = []

    async def deliver(personality, text, expiry):
        delivered.append(text)
        return text != "one"

    await outbox.drain_once(deliver)
    assert delivered == ["one"]  # "two" waits behind the retry
    assert outbox.pending_count() == 2

    # Not ready until the backoff deadline passes.
    await outbox.drain_once(deliver)
    assert delivered == ["one"]


async def test_started_worker_drains_automatically():
    outbox = Outbox(None)
    delivered: list[str] = []
    done = asyncio.Event()

    async def deliver(personality, text, expiry):
        delivered.append(text)
        done.set()
        return True

    outbox.enqueue("atlas", "hello", None)
    outbox.start_worker(deliver, interval_seconds=0.05)
    try:
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        await outbox.stop_worker()

    assert delivered == ["hello"]
    assert outbox.pending_count() == 0
