"""Per-personality runtime: one Nostr identity, one Hermes conversation.

The SDK owns relay plumbing, dedup, and NIP-17/04 protocol matching. This class
owns policy: the owner allowlist, local commands, ack scheduling, per-
personality turn serialization, Hermes error handling, and chunked replies.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from .ack import AckPolicy, PendingAck
from .chunking import truncate_message
from .commands import (
    Command,
    error_text,
    help_text,
    model_set_text,
    models_text,
    new_text,
    parse_command,
    parse_model_selection,
    status_text,
    unknown_text,
)
from .config import PersonalityConfig
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


class PersonalityRuntime:
    def __init__(
        self,
        config: PersonalityConfig,
        *,
        allow_hex: str,
        hermes: HermesClient,
        sessions: SessionMap,
        ack_policy: AckPolicy,
        bot: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.name = config.name
        self.allow_hex = allow_hex.lower()
        self.hermes = hermes
        self.sessions = sessions
        self.ack_policy = ack_policy
        self._clock = clock or time.time
        self._turn_lock = asyncio.Lock()
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
            profile={"name": self.config.name},
        )
        return NostrBot(bot_config)

    @property
    def relay_count(self) -> int:
        return len(self.config.relays)

    @property
    def queue_depth(self) -> int:
        return self._waiting

    def set_watchdog_status_provider(self, provider: Callable[[], str]) -> None:
        self._watchdog_status = provider

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
        self.sessions.reset(self.name)
        self.sessions.set(
            self.name,
            session_id,
            model=previous.model if previous else self.config.model,
            provider=previous.provider if previous else self.config.provider,
        )
        await self._send_chunks(ctx, new_text())

    async def _command_model(self, ctx: Any, args: str) -> None:
        if not args:
            try:
                options = await self.hermes.list_models()
            except HermesError as exc:
                logger.warning("Could not list models for %s: %s", self.name, exc)
                await self._send_chunks(ctx, error_text(self._error_detail(exc)))
                return
            await self._send_chunks(ctx, models_text(options))
            return
        try:
            selection = parse_model_selection(args)
        except ValueError:
            await self._send_chunks(
                ctx,
                "Usage: /model provider/model (or /model with no arguments)",
            )
            return
        self.sessions.update_model(
            self.name, selection.model, selection.provider,
        )
        await self._send_chunks(
            ctx, model_set_text(selection.model, selection.provider),
        )

    def _status_text(self) -> str:
        entry = self.sessions.get(self.name)
        age: float | None = None
        model = self.config.model
        provider = self.config.provider
        if entry is not None:
            if entry.hermes_session_id and entry.created_at:
                age = max(0.0, self._clock() - entry.created_at)
            model = entry.model
            provider = entry.provider
        return status_text(
            name=self.name,
            model=model,
            provider=provider,
            session_age_seconds=age,
            queue_depth=self._waiting,
            relay_count=self.relay_count,
            watchdog_state=self._watchdog_status(),
        )

    # -- turns -----------------------------------------------------------------

    async def _handle_turn(self, ctx: Any, text: str) -> None:
        sender = ctx.sender_hex
        ack: PendingAck | None = await self.ack_policy.arm(
            text, lambda ack_text: self.bot.send_dm(sender, ack_text),
        )
        waiting = True
        self._waiting += 1
        try:
            async with self._turn_lock:
                self._waiting -= 1
                waiting = False
                result = await self._run_turn_with_recovery(ctx, text)
                # Stop a pending delayed ack before the real reply lands.
                await ack.cancel()
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
            await ack.cancel()
            await self._send_chunks(ctx, error_text(self._error_detail(exc)))
        except asyncio.CancelledError:
            await ack.cancel()
            raise
        except Exception:
            logger.exception("Turn failed for %s", self.name)
            await ack.cancel()
            await self._send_chunks(ctx, error_text())
        finally:
            if waiting:
                self._waiting -= 1
            await ack.cancel()

    async def _run_turn_with_recovery(self, ctx: Any, text: str):
        event_id = getattr(ctx, "event_id", None) or uuid.uuid4().hex
        await self.hermes.ensure_session(self.name)
        try:
            return await self.hermes.run_turn(self.name, text, str(event_id))
        except HermesSessionGone:
            logger.warning(
                "Hermes session for %s vanished mid-turn; recreating",
                self.name,
            )
            await self.hermes.ensure_session(self.name)
            return await self.hermes.run_turn(self.name, text, str(event_id))

    async def _send_chunks(self, ctx: Any, text: str) -> None:
        for chunk in truncate_message(text):
            try:
                ok = await self.bot.send_dm(ctx.sender_hex, chunk)
            except Exception:
                logger.exception("Failed to send a DM chunk for %s", self.name)
                return
            if ok is False:
                logger.warning("send_dm reported failure for %s", self.name)
                return

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
        return ""


__all__ = ["PersonalityRuntime"]
