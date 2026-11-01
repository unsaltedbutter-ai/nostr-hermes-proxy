"""Small async fakes: no relays, no sockets, no real gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nostr_hermes_proxy.hermes import ModelOption, RunSubmission, TurnResult
from nostr_hermes_proxy.sessions import SessionMap


@dataclass
class SentEvent:
    """One event handed to the fake relay client."""

    event: Any
    target: Any = None

    @property
    def kind(self) -> int:
        return self.event.kind().as_u16()

    def expiration(self) -> int | None:
        for tag in self.event.tags():
            values = tag.to_vec()
            if len(values) >= 2 and values[0] == "expiration":
                try:
                    return int(values[1])
                except ValueError:
                    return None
        return None


class FakeNostrClient:
    """Records what the runtime publishes, without any relay I/O."""

    def __init__(self, bot: "FakeBot") -> None:
        self.bot = bot
        self.added_relays: list[Any] = []
        self.connect_calls = 0
        self.sent_events: list[SentEvent] = []

    async def add_relay(self, relay_url: Any) -> bool:
        self.added_relays.append(relay_url)
        return True

    async def connect(self) -> None:
        self.connect_calls += 1

    async def send_event(self, event: Any, target: Any = None) -> None:
        if self.bot.send_error is not None:
            raise self.bot.send_error
        self.bot.client.sent_events.append(SentEvent(event, target))
        if self.bot.activity is not None:
            # Both acks and replies are published through send_event now;
            # callers filter by kind when they need to tell them apart.
            self.bot.activity.append("send")
        if event.kind().as_u16() != 1059:
            return
        # Decrypt gift wraps for any test key we hold so text assertions keep
        # working against ``bot.sent`` regardless of the delivery path.
        from nostr_sdk import UnwrappedGift

        for key in self.bot.decrypt_keys:
            try:
                unwrapped = UnwrappedGift.from_gift_wrap(key, event)
            except Exception:
                continue
            rumor = unwrapped.rumor()
            recipient = ""
            for tag in rumor.tags():
                values = tag.to_vec()
                if len(values) >= 2 and values[0] == "p":
                    recipient = values[1]
                    break
            self.bot.sent.append((recipient, rumor.content()))


class FakeNip17Support:
    def __init__(
        self,
        relays: list[str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.relays = relays
        self.error = error
        self.checked: list[str] = []

    async def check(self, pubkey_hex: str) -> list[str] | None:
        self.checked.append(pubkey_hex)
        if self.error is not None:
            raise self.error
        return self.relays


class FakeBot:
    """Duck-types the NostrBot surface the runtime touches.

    ``sent`` records decrypted DMs (both acks and replies, when a matching
    ``decrypt_keys`` entry was supplied); ``sent_events`` records every raw
    event published (NIP-17 gift wraps or kind-4 DMs). The signer is a real
    ephemeral key so gift-wrap construction is exercised for real; nothing
    leaves the process.
    """

    def __init__(
        self,
        *,
        send_result: bool = True,
        activity: list[str] | None = None,
        nip17_relays: list[str] | None = None,
        nip17_error: BaseException | None = None,
        send_error: BaseException | None = None,
        decrypt_keys: list | None = None,
    ) -> None:
        from nostr_sdk import Keys

        self.keys = Keys.generate()
        self.signer = self.keys
        self.decrypt_keys = list(decrypt_keys or [])
        self.sent: list[tuple[str, str]] = []
        self.dm_handler = None
        self.started = 0
        self.stopped = 0
        self.send_result = send_result
        self.activity = activity
        self.send_error = send_error
        self.nip17_support = FakeNip17Support(nip17_relays, nip17_error)
        self.client = FakeNostrClient(self)
        # Watchdog hooks (opt-in per test by setting attributes).
        self.health_value: Any = True
        self.restart_calls = 0
        self.last_activity_monotonic: float | None = None

    @property
    def sent_events(self) -> list[SentEvent]:
        return self.client.sent_events

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
    protocol: str = "nip17"


def provider_models(providers: list[Any]) -> list[ModelOption]:
    """Flatten gateway-shaped provider dicts into ModelOptions.

    Each item looks like ``{"slug": ..., "models": [...],
    "is_user_defined": true}``, matching GET /api/model/options.
    """
    options: list[ModelOption] = []
    for provider in providers:
        if isinstance(provider, ModelOption):
            options.append(provider)
            continue
        name = (
            provider.get("slug")
            or provider.get("id")
            or provider.get("provider")
            or provider.get("name")
        )
        user_defined = provider.get("is_user_defined") is True
        for model in provider.get("models") or []:
            if isinstance(model, dict):
                model = model.get("id") or model.get("model")
            if model:
                options.append(ModelOption(name, str(model), user_defined))
    return options


class FakeHermes:
    """Duck-types the parts of HermesClient the personality runtime uses.

    ``submissions`` feeds ``submit_run`` (``RunSubmission`` or an exception;
    ``None`` means a fresh auto-numbered run). ``results`` feeds
    ``stream_run`` (``TurnResult`` or an exception). The gate blocks inside
    ``stream_run`` so a test can deliver a second DM while a run is in flight.
    """

    def __init__(
        self,
        sessions: SessionMap,
        *,
        results: list[Any] | None = None,
        submissions: list[Any] | None = None,
        session_id: str = "sess-1",
        models: list[ModelOption] | None = None,
        providers: list[Any] | None = None,
        models_error: BaseException | None = None,
        create_error: BaseException | None = None,
        steer_result: bool = True,
        stop_result: str | None = "stopping",
        stop_results: list[Any] | None = None,
        activity: list[str] | None = None,
    ) -> None:
        self.sessions = sessions
        self.session_id = session_id
        self.models = list(models or [])
        self.models_calls = 0
        self.models_error = models_error
        self.create_error = create_error
        if providers is not None:
            self.models = provider_models(providers)
        self.activity = activity
        self.ensure_calls: list[str] = []
        self.create_calls: list[str] = []
        self.submit_calls: list[tuple[str, str, str]] = []
        self.submitted_prefs: list[tuple[str | None, str | None]] = []
        self.stream_calls: list[str] = []
        self.steer_calls: list[tuple[str, str]] = []
        self.submissions: list[Any] = (
            list(submissions) if submissions is not None else []
        )
        self.steer_result = steer_result
        self.steer_results: list[Any] = []
        self.stop_result = stop_result
        self.stop_results: list[Any] = (
            list(stop_results) if stop_results is not None else []
        )
        self.stops: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()
        self._run_seq = 0
        out = results if results is not None else [
            TurnResult("hello", status="completed"),
        ]
        self.results: list[Any] = list(out)
        self._last_result = out[-1] if out else TurnResult("", status="failed")

    @property
    def run_calls(self) -> list[tuple[str, str, str]]:
        """Backwards-compatible alias for tests written before the runs API."""
        return self.submit_calls

    async def ensure_session(self, name: str):
        self.ensure_calls.append(name)
        existing = self.sessions.get(name)
        if existing is not None and existing.hermes_session_id:
            return existing
        previous = self.sessions.get(name)
        # Mirror HermesClient.ensure_session: session recreation keeps the
        # personality's model/provider preference.
        self.sessions.reset(name)
        return self.sessions.set(
            name,
            self.session_id,
            model=previous.model if previous is not None else None,
            provider=previous.provider if previous is not None else None,
        )

    async def create_session(self, name: str) -> str:
        self.create_calls.append(name)
        if self.create_error is not None:
            raise self.create_error
        self.session_id = f"{self.session_id}-new" if self.session_id else "sess-new"
        return self.session_id

    async def list_models(self) -> list[ModelOption]:
        self.models_calls += 1
        if self.models_error is not None:
            raise self.models_error
        return list(self.models)

    def _next_submission(self) -> Any:
        if self.submissions:
            item = self.submissions.pop(0)
            if item is not None:
                return item
        self._run_seq += 1
        return RunSubmission(run_id=f"run-{self._run_seq}")

    async def submit_run(self, name: str, text: str, event_id: str):
        self.submit_calls.append((name, text, event_id))
        entry = self.sessions.get(name)
        self.submitted_prefs.append(
            (entry.model, entry.provider) if entry is not None else (None, None)
        )
        if self.activity is not None:
            self.activity.append("submit")
        item = self._next_submission()
        if isinstance(item, BaseException):
            raise item
        return item

    async def stream_run(self, run_id: str):
        self.stream_calls.append(run_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.gate.wait()
            result = (
                self.results.pop(0)
                if len(self.results) > 1
                else self._last_result
            )
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self.active -= 1

    async def steer(self, run_id: str, text: str) -> bool:
        self.steer_calls.append((run_id, text))
        if self.activity is not None:
            self.activity.append("steer")
        item = (
            self.steer_results.pop(0)
            if self.steer_results
            else self.steer_result
        )
        if isinstance(item, BaseException):
            raise item
        return bool(item)

    async def stop_run(self, run_id: str) -> str | None:
        self.stops.append(run_id)
        if self.activity is not None:
            self.activity.append("stop")
        item = (
            self.stop_results.pop(0)
            if self.stop_results
            else self.stop_result
        )
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class PersonalityHarness:
    runtime: Any
    bot: Any
    hermes: FakeHermes
    sessions: SessionMap
    ctx: FakeCtx
    config: Any
    activity: list[str]
    owner: Any | None = None


def make_harness(
    *,
    name: str = "atlas",
    allow_hex: str | None = None,
    send_result: bool = True,
    results: list[Any] | None = None,
    submissions: list[Any] | None = None,
    steer_result: bool = True,
    stop_result: str | None = "stopping",
    stop_results: list[Any] | None = None,
    models: list[ModelOption] | None = None,
    providers: list[Any] | None = None,
    models_error: BaseException | None = None,
    create_error: BaseException | None = None,
    bot: Any | None = None,
    tmp_path=None,
    config_model: str | None = None,
    config_provider: str | None = None,
    ack_text: str = "👀",
    ack_ttl_seconds: int = 1200,
    gateway_config_path: str | None = None,
    nip17_relays: list[str] | None = None,
    send_error: BaseException | None = None,
    protocol: str = "nip17",
    clock=None,
    decrypt_keys: list | None = None,
    profile: dict[str, str] | None = None,
) -> PersonalityHarness:
    from nostr_hermes_proxy.config import PersonalityConfig
    from nostr_hermes_proxy.personality import PersonalityRuntime

    activity: list[str] = []
    owner = None
    if allow_hex is None:
        # A real owner keypair lets the fake client decrypt what it publishes,
        # so tests can keep asserting on plaintext without a fixed test hex.
        from nostr_sdk import Keys

        owner = Keys.generate()
        allow_hex = owner.public_key().to_hex()
        if decrypt_keys is None:
            decrypt_keys = [owner]
    sessions = SessionMap(tmp_path / "sessions.json" if tmp_path else None)
    fake_bot = bot if bot is not None else FakeBot(
        send_result=send_result,
        activity=activity,
        nip17_relays=nip17_relays,
        send_error=send_error,
        decrypt_keys=decrypt_keys,
    )
    fake_hermes = FakeHermes(
        sessions,
        results=results,
        submissions=submissions,
        models=models,
        providers=providers,
        models_error=models_error,
        create_error=create_error,
        steer_result=steer_result,
        stop_result=stop_result,
        stop_results=stop_results,
        activity=activity,
    )
    config = PersonalityConfig(
        name=name,
        nsec="nsec1synthetictestkeynotreal",
        relays=("wss://nos.lol", "wss://relay.example"),
        model=config_model,
        provider=config_provider,
        profile=profile or {},
    )
    runtime = PersonalityRuntime(
        config,
        allow_hex=allow_hex,
        hermes=fake_hermes,
        sessions=sessions,
        ack_text=ack_text,
        ack_ttl_seconds=ack_ttl_seconds,
        gateway_config_path=gateway_config_path,
        bot=fake_bot,
        clock=clock,
    )
    ctx = FakeCtx(
        sender_hex=allow_hex,
        event_id="event-1",
        protocol=protocol,
    )
    return PersonalityHarness(
        runtime=runtime,
        bot=fake_bot,
        hermes=fake_hermes,
        sessions=sessions,
        ctx=ctx,
        config=config,
        activity=activity,
        owner=owner,
    )
