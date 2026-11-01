"""Immediate 👀 ack: sent on acceptance, short NIP-40 expiry, never fatal."""

from __future__ import annotations

import asyncio

from nostr_sdk import Keys, Timestamp, UnwrappedGift

from .fakes import FakeCtx, make_harness


def _unwrap(owner: Keys, event):
    return UnwrappedGift.from_gift_wrap(owner, event)


def _tag_value(tags, name: str) -> int | None:
    for tag in tags:
        values = tag.to_vec()
        if len(values) >= 2 and values[0] == name:
            return int(values[1])
    return None


async def test_non_command_dm_sends_one_ack_before_submit():
    owner = Keys.generate()
    harness = make_harness(allow_hex=owner.public_key().to_hex())
    await harness.runtime.handle_dm(harness.ctx, "hello")

    assert len(harness.bot.sent_events) == 1
    assert harness.activity == ["ack", "submit"]

    sent = harness.bot.sent_events[0]
    assert sent.kind == 1059  # NIP-17 gift wrap
    unwrapped = _unwrap(owner, sent.event)
    rumor = unwrapped.rumor()
    assert rumor.content() == "👀"

    owner_hex = owner.public_key().to_hex()
    rumor_tags = rumor.tags()
    assert any(
        tag.to_vec()[0] == "p" and tag.to_vec()[1] == owner_hex
        for tag in rumor_tags
    )
    rumor_exp = _tag_value(rumor_tags, "expiration")
    wrap_exp = sent.expiration()
    now = Timestamp.now().as_secs()
    assert rumor_exp is not None and wrap_exp is not None
    assert rumor_exp == wrap_exp
    assert 1190 <= wrap_exp - now <= 1210  # ~20 minutes


async def test_ack_is_sent_before_the_turn_lock_is_acquired(test_hex):
    """The ack must not wait for a queued turn behind an in-flight run."""
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    second_ctx = FakeCtx(sender_hex=test_hex, event_id="event-2")
    second = asyncio.create_task(
        harness.runtime.handle_dm(second_ctx, "two"),
    )
    # The steered DM's ack lands while the first turn is still in flight.
    for _ in range(50):
        if len(harness.bot.sent_events) == 2:
            break
        await asyncio.sleep(0)
    assert len(harness.bot.sent_events) == 2

    harness.hermes.gate.set()
    await asyncio.gather(first, second)
    # Two accepted non-command DMs, two acks; the second preceded its steer.
    assert harness.activity == ["ack", "submit", "ack", "steer"]


async def test_steered_dm_gets_an_ack():
    owner = Keys.generate()
    harness = make_harness(allow_hex=owner.public_key().to_hex())
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    second_ctx = FakeCtx(
        sender_hex=owner.public_key().to_hex(), event_id="event-2",
    )
    await harness.runtime.handle_dm(second_ctx, "two")

    assert harness.hermes.steer_calls == [("run-1", "two")]
    assert len(harness.bot.sent_events) == 2
    for sent in harness.bot.sent_events:
        assert _unwrap(owner, sent.event).rumor().content() == "👀"

    harness.hermes.gate.set()
    await first


async def test_commands_get_no_ack(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/status")
    await harness.runtime.handle_dm(harness.ctx, "/help")
    assert harness.bot.sent_events == []


async def test_ack_send_failure_does_not_break_the_turn(test_hex):
    harness = make_harness(
        allow_hex=test_hex, send_error=RuntimeError("relay down"),
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.hermes.submit_calls == [
        ("atlas", "hello", "event-1"),
    ]
    assert harness.bot.sent[-1][1] == "hello"
    assert harness.bot.sent_events == []


async def test_nip04_sender_gets_a_kind4_ack():
    owner = Keys.generate()
    harness = make_harness(
        allow_hex=owner.public_key().to_hex(), protocol="nip04",
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")

    assert len(harness.bot.sent_events) == 1
    sent = harness.bot.sent_events[0]
    assert sent.kind == 4
    plaintext = owner.nip04_decrypt(
        harness.bot.keys.public_key(), sent.event.content(),
    )
    assert plaintext == "👀"
    owner_hex = owner.public_key().to_hex()
    assert any(
        tag.to_vec()[0] == "p" and tag.to_vec()[1] == owner_hex
        for tag in sent.event.tags()
    )
    now = Timestamp.now().as_secs()
    exp = sent.expiration()
    assert exp is not None and 1190 <= exp - now <= 1210


async def test_config_knobs_are_honored():
    owner = Keys.generate()
    harness = make_harness(
        allow_hex=owner.public_key().to_hex(),
        ack_text="✅",
        ack_ttl_seconds=90,
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")

    sent = harness.bot.sent_events[0]
    assert _unwrap(owner, sent.event).rumor().content() == "✅"
    now = Timestamp.now().as_secs()
    exp = sent.expiration()
    assert exp is not None and 85 <= exp - now <= 95


async def test_ack_uses_sender_inbox_relays_when_published(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        nip17_relays=["wss://inbox.example", "wss://nos.lol"],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")

    assert len(harness.bot.client.added_relays) == 2
    assert harness.bot.client.connect_calls == 1
    # The relay url strings are what the kind-10050 lookup returned.
    urls = [str(relay) for relay in harness.bot.client.added_relays]
    assert all("inbox.example" in url or "nos.lol" in url for url in urls)


async def test_ack_without_inbox_relays_broadcasts(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "hello")

    sent = harness.bot.sent_events[0]
    assert harness.bot.client.added_relays == []
    assert harness.bot.client.connect_calls == 0
    assert sent.target is not None
