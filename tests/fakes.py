"""Small async fakes: no relays, no sockets, no real gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nostr_hermes_proxy.hermes import ModelOption, TurnResult
from nostr_hermes_proxy.sessions import SessionMap


@dataclass
class FakePendingAck:
    sent: bool = False
    owner: "FakeAckPolicy | None" = None

    async def cancel(self) -> bool:
        if self.owner is not None:
            self.owner.cancelled += 1
        return self.sent


class FakeAckPolicy:
    """Records arms/cancels without sending anything."""

    def __init__(self) -> None:
        self.armed: list[str] = []
        self.cancelled = 0
        self.pending: list[FakePendingAck] = []

    async def arm(self, text: str, send) -> FakePendingAck:
        self.armed.append(text)
        pending = FakePendingAck(owner=self)
        self.pending.append(pending)
        return pending


class FakeBot:
    def __init__(self, *, send_result: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.dm_handler = None
        self.started = 0
        self.stopped = 0
        self.send_result = send_result
        # Watchdog hooks (opt-in per test by setting attributes).
        self.health_value: Any = True
        self.restart_calls = 0
        self.last_activity_monotonic: float | None = None

    def on_dm(self, handler):
        self.dm_handler = handler
        return handler

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def send_dm(self, recipient: str, text: str) -> bool:
        self.sent.append((recipient, text))
        return self.send_result

    async def health_check(self):
        return self.health_value

    async def restart(self) -> None:
        self.restart_calls += 1


class NakedBot:
    """A bot with no health/restart/activity hooks at all (v0.5.2 today)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.dm_handler = None

    def on_dm(self, handler):
        self.dm_handler = handler
        return handler

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_dm(self, recipient: str, text: str) -> bool:
        self.sent.append((recipient, text))
        return True


@dataclass
class FakeCtx:
    sender_hex: str
    sender_npub: str = ""
    event_id: str | None = None


class FakeHermes:
    """Duck-types the parts of HermesClient the personality runtime uses."""

    def __init__(
        self,
        sessions: SessionMap,
        *,
        results: list[Any] | None = None,
        session_id: str = "sess-1",
        models: list[ModelOption] | None = None,
    ) -> None:
        self.sessions = sessions
        self.session_id = session_id
        self.models = models or []
        self.ensure_calls: list[str] = []
        self.run_calls: list[tuple[str, str, str]] = []
        self.create_calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()
        out = results if results is not None else [TurnResult("hello", status="completed")]
        self.results: list[Any] = list(out)
        self._last_result = out[-1] if out else TurnResult("", status="failed")

    async def ensure_session(self, name: str):
        self.ensure_calls.append(name)
        existing = self.sessions.get(name)
        if existing is not None and existing.hermes_session_id:
            return existing
        return self.sessions.set(name, self.session_id)

    async def create_session(self, name: str) -> str:
        self.create_calls.append(name)
        self.session_id = f"{self.session_id}-new" if self.session_id else "sess-new"
        return self.session_id

    async def list_models(self) -> list[ModelOption]:
        return list(self.models)

    async def run_turn(self, name: str, text: str, event_id: str):
        self.run_calls.append((name, text, event_id))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.gate.wait()
            result = self.results.pop(0) if len(self.results) > 1 else self._last_result
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self.active -= 1


@dataclass
class PersonalityHarness:
    runtime: Any
    bot: Any
    hermes: FakeHermes
    sessions: SessionMap
    ack: Any
    ctx: FakeCtx
    config: Any


def make_harness(
    *,
    name: str = "atlas",
    allow_hex: str,
    send_result: bool = True,
    results: list[Any] | None = None,
    models: list[ModelOption] | None = None,
    bot: Any | None = None,
    ack: Any | None = None,
    tmp_path=None,
    config_model: str | None = None,
    config_provider: str | None = None,
    clock=None,
) -> PersonalityHarness:
    from nostr_hermes_proxy.config import PersonalityConfig
    from nostr_hermes_proxy.personality import PersonalityRuntime

    sessions = SessionMap(tmp_path / "sessions.json" if tmp_path else None)
    fake_bot = bot if bot is not None else FakeBot(send_result=send_result)
    fake_hermes = FakeHermes(
        sessions, results=results, models=models,
    )
    fake_ack = ack if ack is not None else FakeAckPolicy()
    config = PersonalityConfig(
        name=name,
        nsec="nsec1synthetictestkeynotreal",
        relays=("wss://nos.lol", "wss://relay.example"),
        model=config_model,
        provider=config_provider,
    )
    runtime = PersonalityRuntime(
        config,
        allow_hex=allow_hex,
        hermes=fake_hermes,
        sessions=sessions,
        ack_policy=fake_ack,
        bot=fake_bot,
        clock=clock,
    )
    ctx = FakeCtx(
        sender_hex=allow_hex,
        event_id="event-1",
    )
    return PersonalityHarness(
        runtime=runtime,
        bot=fake_bot,
        hermes=fake_hermes,
        sessions=sessions,
        ack=fake_ack,
        ctx=ctx,
        config=config,
    )
