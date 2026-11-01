"""Generalized outbound delivery: chunking, expiry clamping, SDK fallback."""

from __future__ import annotations

from nostr_sdk import Keys, Timestamp, UnwrappedGift

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
