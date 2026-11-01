"""Per-personality runtime: one Nostr identity, one Hermes conversation.

The SDK owns relay plumbing, dedup, and NIP-17/04 protocol matching. This class
owns policy: the owner allowlist, local commands, ack scheduling, per-
personality turn serialization, steering of in-flight turns, Hermes error
handling, and chunked replies.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Any, Callable

from .chunking import truncate_message
from .commands import (
    MODEL_MENU_HINT,
    NO_MODELS_TEXT,
    Command,
    error_text,
    help_text,
    model_index_out_of_range_text,
    model_set_text,
    models_menu_text,
    new_text,
    parse_command,
    parse_model_index,
    parse_model_selection,
    status_text,
    unknown_text,
)
from .config import (
    DEFAULT_ACK_TEXT,
    DEFAULT_ACK_TTL_SECONDS,
    DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS,
    PersonalityConfig,
)
from .gateway_config import GatewayConfigError, GatewayModels, read_gateway_models
from .hermes import (
    STATUS_COMPLETED,
    HermesAuthError,
    HermesClient,
    HermesConflictError,
    HermesError,
    HermesRateLimitError,
    HermesSessionGone,
)
from .sessions import SessionMap

logger = logging.getLogger(__name__)

MIN_OUTBOUND_EXPIRY_SECONDS = 60


class DeliveryError(RuntimeError):
    """The bot reported a failed publish; the caller should retry."""


class PersonalityRuntime:
    def __init__(
        self,
        config: PersonalityConfig,
        *,
        allow_hex: str,
        hermes: HermesClient,
        sessions: SessionMap,
        ack_text: str = DEFAULT_ACK_TEXT,
        ack_ttl_seconds: int = DEFAULT_ACK_TTL_SECONDS,
        outbound_spread_seconds: int = 3600,
        gateway_config_path: str | None = None,
        watchdog_stall_seconds: float | None = None,
        outbound_expiry_max_seconds: int = DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS,
        bot: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.name = config.name
        self.allow_hex = allow_hex.lower()
        self.hermes = hermes
        self.sessions = sessions
        self.ack_text = ack_text
        self.ack_ttl_seconds = int(ack_ttl_seconds)
        self._outbound_spread_seconds = int(outbound_spread_seconds)
        #: The gateway's config.yaml path, re-read live on every /model,
        #: /new, and /status — never cached — so gateway-side model changes
        # need no proxy updates.
        self._gateway_config_path = gateway_config_path
        #: Last successful /model listing, for "/model #" resolution.
        self._model_menu: list[tuple[str | None, str]] = []
        self._watchdog_stall_seconds = watchdog_stall_seconds
        #: Outbound NIP-40 expiries are clamped into [60, this].
        self._outbound_expiry_max_seconds = int(outbound_expiry_max_seconds)
        self._clock = clock or time.time
        self._turn_lock = asyncio.Lock()
        #: Run id of the turn currently executing, or None when idle. Read
        #: without the lock so mid-turn DMs can steer immediately.
        self._run_id: str | None = None
        #: DMs pending on the turn lock (steer-refused fallbacks / queued).
        self._waiting = 0
        self._started = False
        self._watchdog_status: Callable[[], str] = lambda: "not running"
        self.bot = bot if bot is not None else self._build_bot()
        # Last call wins; registering here also covers injected fakes.
        self.bot.on_dm(self.handle_dm)

    # -- construction / lifecycle ---------------------------------------------

    def _build_bot(self) -> Any:
        from nostrbot_sdk import NostrBot, NostrBotConfig

        bot_config = NostrBotConfig(
            nsec=self.config.nsec,
            relays=list(self.config.relays),
            profile=self._profile_metadata(),
            # SDK-level stall detection (nostrbot-sdk >= 0.6.0): feeds
            # health_check(), which the process watchdog polls. None keeps
            # the SDK default (disabled) for injected fakes / older SDKs.
            watchdog_stall_seconds=self._watchdog_stall_seconds,
        )
        return NostrBot(bot_config)

    def _profile_metadata(self) -> dict[str, str]:
        """Kind-0 metadata: the internal name plus configured overrides."""
        metadata = {"name": self.config.name}
        metadata.update(self.config.profile)
        return metadata

    @property
    def relay_count(self) -> int:
        return len(self.config.relays)

    @property
    def queue_depth(self) -> int:
        return self._waiting

    def set_watchdog_status_provider(self, provider: Callable[[], str]) -> None:
        self._watchdog_status = provider

    @property
    def watchdog_state(self) -> str:
        return self._watchdog_status()

    def seed_preferences(self) -> None:
        self.sessions.seed_preferences(
            self.name, self.config.model, self.config.provider,
        )

    async def start(self) -> None:
        self.seed_preferences()
        await self.bot.start()
        self._started = True
        logger.info(
            "Personality %s started on %d relay(s)",
            self.name, self.relay_count,
        )

    async def stop(self) -> None:
        try:
            await self.bot.stop()
        finally:
            self._started = False

    # -- inbound handling ------------------------------------------------------

    async def handle_dm(self, ctx: Any, text: str) -> None:
        """SDK ``on_dm`` handler. Never raises: the bot task must survive."""
        if not self._is_allowed(ctx):
            logger.warning(
                "Dropped DM for personality %s from a non-allowlisted sender",
                self.name,
            )
            return
        try:
            command = parse_command(text)
            if command is not None:
                await self._handle_command(ctx, command)
                return
            # Ack on acceptance, before the turn lock (and before steering),
            # so every non-command DM gets exactly one 👀 immediately.
            await self._send_ack(ctx)
            await self._handle_turn(ctx, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected failure handling a DM for %s", self.name)
            try:
                await self._send_chunks(ctx, error_text())
            except Exception:
                logger.exception("Could not send the error DM for %s", self.name)

    def _is_allowed(self, ctx: Any) -> bool:
        sender = getattr(ctx, "sender_hex", "") or ""
        if not isinstance(sender, str):
            return False
        return sender.lower() == self.allow_hex

    # -- commands --------------------------------------------------------------

    async def _handle_command(self, ctx: Any, command: Command) -> None:
        name = command.name
        if name == "help":
            await self._send_chunks(ctx, help_text())
            return
        if name == "new":
            await self._command_new(ctx)
            return
        if name == "model":
            await self._command_model(ctx, command.args)
            return
        if name == "status":
            await self._send_chunks(ctx, self._status_text())
            return
        await self._send_chunks(ctx, unknown_text(name))

    async def _command_new(self, ctx: Any) -> None:
        previous = self.sessions.get(self.name)
        try:
            session_id = await self.hermes.create_session(self.name)
        except HermesError as exc:
            logger.warning("Could not create session for %s: %s", self.name, exc)
            await self._send_chunks(ctx, error_text(self._error_detail(exc)))
            return
        model, provider = self._resolved_model(previous)
        self.sessions.reset(self.name)
        self.sessions.set(
            self.name, session_id, model=model, provider=provider,
        )
        await self._send_chunks(
            ctx, new_text(model, provider, self._context_total(model, provider)),
        )

    async def _command_model(self, ctx: Any, args: str) -> None:
        if not args:
            menu, error_detail = await self._refresh_menu()
            if not menu:
                if error_detail is not None:
                    await self._send_chunks(ctx, error_text(error_detail))
                else:
                    await self._send_chunks(ctx, NO_MODELS_TEXT)
                return
            entry = self.sessions.get(self.name)
            await self._send_chunks(
                ctx,
                models_menu_text(
                    menu,
                    current_model=(
                        entry.model if entry is not None else self.config.model
                    ),
                    current_provider=(
                        entry.provider
                        if entry is not None
                        else self.config.provider
                    ),
                ),
            )
            return
        index = parse_model_index(args)
        if index is not None:
            # The menu lives in memory, so a daemon restart clears it —
            # rebuild it from the gateway config (or API fallback) on demand
            # rather than making the user list models again.
            if not self._model_menu:
                self._model_menu, _ = await self._refresh_menu()
            if not self._model_menu:
                await self._send_chunks(ctx, MODEL_MENU_HINT)
                return
            if index < 1 or index > len(self._model_menu):
                await self._send_chunks(
                    ctx, model_index_out_of_range_text(index),
                )
                return
            provider, model = self._model_menu[index - 1]
            self.sessions.update_model(self.name, model, provider)
            await self._send_chunks(
                ctx,
                model_set_text(
                    model, provider, self._context_total(model, provider),
                ),
            )
            return
        try:
            selection = parse_model_selection(args)
        except ValueError:
            await self._send_chunks(
                ctx,
                "Usage: /model #, /model provider/model "
                "(or /model with no arguments)",
            )
            return
        self.sessions.update_model(
            self.name, selection.model, selection.provider,
        )
        await self._send_chunks(
            ctx,
            model_set_text(
                selection.model,
                selection.provider,
                self._context_total(selection.model, selection.provider),
            ),
        )

    def _gateway_menu(self) -> list[tuple[str | None, str]] | None:
        """Fresh model menu from the gateway's config.yaml, or None.

        Returns None (and logs) when the file is unreadable/empty so the
        caller can fall back to the gateway API inventory. Reads happen on
        every command — never cached.
        """
        if not self._gateway_config_path:
            return None
        try:
            models: GatewayModels = read_gateway_models(
                self._gateway_config_path,
            )
        except GatewayConfigError as exc:
            logger.warning(
                "Could not read the gateway config for %s: %s", self.name, exc,
            )
            return None
        return list(models.menu)

    async def _refresh_menu(
        self,
    ) -> tuple[list[tuple[str | None, str]], str | None]:
        """Rebuild the model menu: gateway config.yaml first, API fallback.

        Returns (menu, error_detail). The config.yaml read is live (no
        caching); when it yields nothing the gateway API's user-defined
        inventory is used. error_detail is set only when the API fallback
        itself failed.
        """
        menu = self._gateway_menu()
        if menu:
            self._model_menu = menu
            return menu, None
        try:
            options = await self.hermes.list_models()
        except HermesError as exc:
            logger.warning("Could not list models for %s: %s", self.name, exc)
            self._model_menu = []
            return [], self._error_detail(exc)
        menu = [
            (option.provider, option.model)
            for option in options
            if getattr(option, "user_defined", False)
        ]
        self._model_menu = menu
        return menu, None

    def _resolved_model(
        self, entry: Any,
    ) -> tuple[str | None, str | None]:
        """The session's pinned model, or the gateway's default (fresh read)."""
        model = entry.model if entry is not None else None
        provider = entry.provider if entry is not None else None
        if model:
            return model, provider
        if not self._gateway_config_path:
            return None, None
        try:
            models: GatewayModels = read_gateway_models(
                self._gateway_config_path,
            )
        except GatewayConfigError:
            return None, None
        return models.default_model, models.default_provider

    def _context_total(
        self, model: str | None, provider: str | None = None,
    ) -> int | None:
        """Fresh context total for the model, or None when unknown."""
        if not model or not self._gateway_config_path:
            return None
        try:
            models: GatewayModels = read_gateway_models(
                self._gateway_config_path,
            )
        except GatewayConfigError:
            return None
        return models.context_length(provider, model)

    def _status_text(self) -> str:
        entry = self.sessions.get(self.name)
        age: float | None = None
        consumed: int | None = None
        if entry is not None:
            if entry.hermes_session_id and entry.created_at:
                age = max(0.0, self._clock() - entry.created_at)
            consumed = entry.last_input_tokens
        model, provider = self._resolved_model(entry)
        return status_text(
            name=self.name,
            model=model,
            provider=provider,
            session_age_seconds=age,
            queue_depth=self._waiting,
            relay_count=self.relay_count,
            watchdog_state=self._watchdog_status(),
            consumed_tokens=consumed,
            context_total=self._context_total(model, provider),
            busy=self._run_id is not None,
        )

    # -- turns -----------------------------------------------------------------

    async def _handle_turn(self, ctx: Any, text: str) -> None:
        """Run or steer one non-command DM.

        The ack has already been sent by :meth:`handle_dm` for this DM. A DM
        that arrives while a run is in flight is steered into it (no reply of
        its own — the running turn's reply covers it). If the steer is refused
        because the turn just became terminal, fall back to a normal turn,
        waiting on the per-personality lock.
        """
        if self._run_id is not None and await self._try_steer(text):
            return
        await self._run_as_turn(ctx, text)

    async def _try_steer(self, text: str) -> bool:
        run_id = self._run_id
        if not run_id:
            return False
        try:
            accepted = await self.hermes.steer(run_id, text)
        except Exception:
            logger.exception(
                "Steering run %s for %s failed", run_id, self.name,
            )
            return False
        if accepted:
            logger.info("Steered in-flight run %s for %s", run_id, self.name)
        else:
            logger.info(
                "Steer refused for %s (run %s just finished); queueing a turn",
                self.name, run_id,
            )
        return bool(accepted)

    async def _run_as_turn(self, ctx: Any, text: str) -> None:
        waiting = True
        self._waiting += 1
        try:
            async with self._turn_lock:
                self._waiting -= 1
                waiting = False
                submission = await self._submit_with_recovery(ctx, text)
                if submission.replayed:
                    logger.info(
                        "Duplicate DM for %s replayed run %s; dropping it "
                        "without a reply",
                        self.name, submission.run_id,
                    )
                    return
                self._run_id = submission.run_id
                try:
                    result = await self.hermes.stream_run(
                        submission.run_id,
                    )
                finally:
                    self._run_id = None
                if result.status == STATUS_COMPLETED:
                    self.sessions.update_usage(
                        self.name, result.input_tokens,
                    )
                if result.status != STATUS_COMPLETED:
                    await self._send_chunks(
                        ctx,
                        error_text(
                            result.error
                            or f"Hermes turn ended {result.status}."
                        ),
                    )
                    return
                final = result.final_text.strip()
                if not final:
                    await self._send_chunks(
                        ctx, error_text("Hermes returned an empty reply."),
                    )
                    return
                await self._send_chunks(ctx, final)
        except HermesError as exc:
            logger.warning("Hermes error for %s: %s", self.name, exc)
            await self._send_chunks(ctx, error_text(self._error_detail(exc)))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Turn failed for %s", self.name)
            await self._send_chunks(ctx, error_text())
        finally:
            if waiting:
                self._waiting -= 1

    async def _submit_with_recovery(self, ctx: Any, text: str):
        event_id = getattr(ctx, "event_id", None) or uuid.uuid4().hex
        await self.hermes.ensure_session(self.name)
        try:
            return await self.hermes.submit_run(
                self.name, text, str(event_id),
            )
        except HermesSessionGone:
            logger.warning(
                "Hermes session for %s vanished mid-turn; recreating",
                self.name,
            )
            await self.hermes.ensure_session(self.name)
            return await self.hermes.submit_run(
                self.name, text, str(event_id),
            )

    # -- outbound delivery -----------------------------------------------------

    async def deliver_dm(
        self,
        recipient_hex: str,
        text: str,
        expiry_seconds: int | None = None,
        protocol: str = "nip17",
    ) -> int:
        """Send ``text`` to ``recipient_hex``, chunked; returns chunk count.

        With an explicit expiry the NIP-17 gift wrap is built manually so the
        rumor *and* the wrap carry the same NIP-40 tag and the wrap targets
        the recipient's kind-10050 inbox relays (broadcast when none are
        known). Without one the SDK's ``send_dm`` applies its seven-day
        default. A NIP-04 correspondent gets kind-4 events, as before.
        """
        chunks = truncate_message(text)
        ttl = (
            None
            if expiry_seconds is None
            else self._clamp_expiry(expiry_seconds)
        )
        # NIP-59 randomizes wrap timestamps up to 2 days back, which scrambles
        # multi-chunk ordering in clients. Assign each chunk a random backdate
        # within the spread window, sorted ASCENDING: chunk 1 gets the smallest
        # backdate (the newest wrap), so clients show chunks in reading order
        # while every wrap still carries an individually random NIP-59-looking
        # timestamp.
        now = int(self._clock())
        backdates = sorted(
            (
                random.randint(0, self._outbound_spread_seconds)
                for _ in chunks
            ),
        )
        for index, (chunk, backdate) in enumerate(zip(chunks, backdates), start=1):
            await self._deliver_chunk(
                recipient_hex, chunk, ttl, protocol,
                wrap_created_at=now - backdate,
            )
            logger.info(
                "Outbound chunk %d/%d to %s (%d chars)",
                index, len(chunks), recipient_hex[:16], len(chunk),
            )
        return len(chunks)

    def _clamp_expiry(self, expiry_seconds: int) -> int:
        maximum = max(MIN_OUTBOUND_EXPIRY_SECONDS,
                      self._outbound_expiry_max_seconds)
        try:
            value = int(expiry_seconds)
        except (TypeError, ValueError):
            value = MIN_OUTBOUND_EXPIRY_SECONDS
        return max(MIN_OUTBOUND_EXPIRY_SECONDS, min(value, maximum))

    async def _deliver_chunk(
        self,
        recipient_hex: str,
        text: str,
        ttl: int | None,
        protocol: str,
        wrap_created_at: int | None = None,
    ) -> None:
        from nostr_sdk import (
            EventBuilder,
            Kind,
            KindStandard,
            PublicKey,
            RelayUrl,
            SendEventTarget,
            Tag,
        )
        from nostrbot_sdk import expiration_tag
        from nostrbot_sdk.gift_wrap import make_gift_wrap_async

        pk = PublicKey.parse(recipient_hex)

        if protocol == "nip04":
            ciphertext = self.bot.signer.nip04_encrypt(pk, text)
            tags: list[Any] = [Tag.public_key(pk)]
            if ttl is not None:
                tags.append(expiration_tag(ttl))
            event = await (
                EventBuilder(Kind(4), ciphertext)
                .tags(tags)
                .finalize_async(self.bot.signer)
            )
            await self.bot.client.send_event(event)
            return

        if ttl is None:
            ok = await self.bot.send_dm(recipient_hex, text)
            if ok is False:
                raise DeliveryError(
                    f"send_dm reported failure for {self.name}"
                )
            return

        exp = expiration_tag(ttl)
        rumor = (
            EventBuilder(
                Kind.from_std(KindStandard.PRIVATE_DIRECT_MESSAGE),
                text,
            )
            .tags([Tag.public_key(pk), exp])
            .finalize_unsigned(self.bot.keys.public_key())
        )
        wrap = await make_gift_wrap_async(
            self.bot.signer,
            pk,
            rumor,
            expiration=None,
            extra_tags=[exp],
            wrap_created_at=wrap_created_at,
        )
        relays = await self.bot.nip17_support.check(recipient_hex)
        if relays:
            relay_urls = [RelayUrl.parse(relay) for relay in relays]
            added: list[Any] = []
            for relay_url in relay_urls:
                if await self.bot.client.add_relay(relay_url):
                    added.append(relay_url)
            if added:
                await self.bot.client.connect()
            await self.bot.client.send_event(
                wrap, target=SendEventTarget.to(relay_urls),
            )
            return
        await self.bot.client.send_event(
            wrap, target=SendEventTarget.broadcast(),
        )

    # -- immediate ack ---------------------------------------------------------

    async def _send_ack(self, ctx: Any) -> None:
        """Best-effort 👀 DM; never lets an ack failure break the turn."""
        try:
            await self.deliver_dm(
                getattr(ctx, "sender_hex", "") or "",
                self.ack_text,
                self.ack_ttl_seconds,
                getattr(ctx, "protocol", "nip17") or "nip17",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Could not send the ack DM for %s", self.name, exc_info=True,
            )

    async def _send_chunks(self, ctx: Any, text: str) -> None:
        """Reply path: ordered, chunked delivery through ``deliver_dm``."""
        try:
            await self.deliver_dm(
                ctx.sender_hex,
                text,
                expiry_seconds=self._outbound_expiry_max_seconds,
                protocol=getattr(ctx, "protocol", None) or "nip17",
            )
        except Exception:
            logger.exception("Failed to send a reply DM for %s", self.name)

    @staticmethod
    def _error_detail(exc: HermesError) -> str:
        if isinstance(exc, HermesAuthError):
            return "Hermes rejected the proxy's API key."
        if isinstance(exc, HermesRateLimitError):
            return "Hermes is busy; try again shortly."
        if isinstance(exc, HermesSessionGone):
            return "Hermes lost this conversation; send /new to start over."
        if isinstance(exc, HermesConflictError):
            return "Hermes is busy with another turn; try again."
        # Generic Hermes errors carry the gateway's own message — surface it
        # (truncated) so failures like title collisions are actionable.
        detail = str(exc).strip()
        return detail[:200] if detail else ""


__all__ = ["DeliveryError", "MIN_OUTBOUND_EXPIRY_SECONDS", "PersonalityRuntime"]
