from __future__ import annotations

import asyncio

import pytest

from nostr_hermes_proxy.ack import AckPolicy, build_ack_text


class ControlledSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.started.set()
        await self.release.wait()


class Sender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


def test_ack_text_is_collapsed_and_truncated():
    text = "  hello\n\n  world " + "x" * 100
    ack = build_ack_text(text)
    assert ack.startswith("Working on hello world x")
    assert ack.endswith("\u2026")
    assert len(ack) <= len("Working on ") + 60 + 1


def test_ack_text_empty():
    assert build_ack_text("   ") == "Working on it\u2026"


async def test_immediate_ack_on_zero_delay():
    sender = Sender()
    policy = AckPolicy(0)
    pending = await policy.arm("do the thing", sender)
    assert sender.sent == ["Working on do the thing"]
    assert pending.sent
    assert pending.task is None


async def test_delayed_ack_not_sent_before_delay_and_sent_after():
    sleeper = ControlledSleeper()
    sender = Sender()
    policy = AckPolicy(3, sleep=sleeper)

    pending = await policy.arm("long task", sender)
    await sleeper.started.wait()
    assert sleeper.calls == [3.0]
    assert sender.sent == []
    assert not pending.sent

    sleeper.release.set()
    assert await pending.wait()
    assert sender.sent == ["Working on long task"]
    assert pending.sent


async def test_cancel_prevents_delayed_ack():
    sleeper = ControlledSleeper()
    sender = Sender()
    policy = AckPolicy(5, sleep=sleeper)

    pending = await policy.arm("long task", sender)
    await sleeper.started.wait()
    already_sent = await pending.cancel()
    assert already_sent is False
    assert sender.sent == []
    assert not pending.sent


async def test_cancel_after_send_reports_sent():
    sleeper = ControlledSleeper()
    sleeper.release.set()
    sender = Sender()
    policy = AckPolicy(1, sleep=sleeper)

    pending = await policy.arm("task", sender)
    assert await pending.wait()
    assert await pending.cancel() is True
    assert sender.sent == ["Working on task"]


async def test_send_failure_is_swallowed():
    sleeper = ControlledSleeper()
    sleeper.release.set()

    async def boom(text: str) -> None:
        raise RuntimeError("relay down")

    policy = AckPolicy(1, sleep=sleeper)
    pending = await policy.arm("task", boom)
    assert await pending.wait() is False


def test_negative_delay_rejected():
    with pytest.raises(ValueError):
        AckPolicy(-1)
