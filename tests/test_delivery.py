"""Generalized outbound delivery: chunking, expiry clamping, SDK fallback."""

from __future__ import annotations

import re

from nostr_sdk import Keys, Timestamp, UnwrappedGift

from nostr_hermes_proxy.hermes import TurnResult

from .fakes import make_harness


def _expiration(event) -> int | None:
    for tag in event.tags():
        values = tag.to_vec()
        if len(values) >= 2 and values[0] == "expiration":
            return int(values[1])
    return None


async def test_deliver_dm_chunks_and_clamps_minimum_expiry():
    owner = Keys.generate()
    harness = make_harness(allow_hex=owner.public_key().to_hex())

    chunks = await harness.runtime.deliver_dm(
        owner.public_key().to_hex(), "x" * 5000, expiry_seconds=1,
    )

    assert chunks == 2
    assert len(harness.bot.sent_events) == 2
    now = Timestamp.now().as_secs()
    for sent in harness.bot.sent_events:
        exp = _expiration(sent.event)
        assert exp is not None and 55 <= exp - now <= 65
        rumor = UnwrappedGift.from_gift_wrap(owner, sent.event).rumor()
        assert rumor.content().startswith("x")
        assert _expiration(rumor) == exp


async def test_deliver_dm_clamps_maximum_expiry():
    owner = Keys.generate()
    harness = make_harness(allow_hex=owner.public_key().to_hex())

    await harness.runtime.deliver_dm(
        owner.public_key().to_hex(), "hello", expiry_seconds=10**9,
    )

    now = Timestamp.now().as_secs()
    exp = _expiration(harness.bot.sent_events[0].event)
    assert exp is not None and 604790 <= exp - now <= 604810


async def test_deliver_dm_without_expiry_uses_sdk_default():
    owner = Keys.generate()
    harness = make_harness(allow_hex=owner.public_key().to_hex())

    chunks = await harness.runtime.deliver_dm(
        owner.public_key().to_hex(), "hello",
    )

    assert chunks == 1
    assert harness.bot.sent == [(owner.public_key().to_hex(), "hello")]
    assert harness.bot.sent_events == []


async def test_reply_chunks_are_ordered_newest_first(clock, monkeypatch):
    """Chunk 1 must carry the newest wrap timestamp despite NIP-59 jitter."""
    # One backdate for the ack, then one per reply chunk.
    backdates = iter([1200, 2400, 600])
    monkeypatch.setattr(
        "nostr_hermes_proxy.personality.random.randint",
        lambda _low, _high: next(backdates),
    )
    original = "line 0:" + "x" * 3000 + " " + "y" * 2000  # two chunks
    harness = make_harness(
        results=[TurnResult(original, status="completed")],
        clock=clock,
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")

    wraps = [sent for sent in harness.bot.sent_events if sent.kind == 1059]
    assert len(wraps) == 3  # one ack + two reply chunks
    ack = UnwrappedGift.from_gift_wrap(harness.owner, wraps[0].event).rumor()
    assert ack.content() == "👀"

    chunks = wraps[1:]
    now = int(clock())
    times = [sent.event.created_at().as_secs() for sent in chunks]
    assert times == [now - 600, now - 2400]
    assert times == sorted(times, reverse=True)
    assert times[0] > times[1]
    for created_at in times:
        assert 0 <= now - created_at <= 3600
    # Chunks never drift further apart than the configured spread window.
    assert times[0] - times[1] <= 3600

    texts = [
        UnwrappedGift.from_gift_wrap(
            harness.owner, sent.event,
        ).rumor().content()
        for sent in chunks
    ]
    assert texts[0].startswith("line 0:")
    stripped = [re.sub(r" \(\d+/\d+\)$", "", text) for text in texts]
    # The chunker splits at a space and lstrips it from the next chunk, so
    # compare with whitespace removed.
    assert "".join(stripped).replace(" ", "") == original.replace(" ", "")
