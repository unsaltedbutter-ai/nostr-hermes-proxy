"""Loopback-only HTTP control plane for outbound Nostr DMs.

Two gates: the TCPSite binds 127.0.0.1, and every request must present the
bearer token from the 0600 token file. The recipient is never taken from the
request — it is the personality's configured allowlisted npub.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from aiohttp import web

from .config import Config
from .outbox import Outbox, RateLimited
from .personality import PersonalityRuntime

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 64 * 1024
_HOST = "127.0.0.1"
_HEALTHY_WORDS = {"ok", "healthy", "connected", "ready", "up"}


class ControlTokenError(RuntimeError):
    """The control token file is missing, unreadable, or empty."""


def read_control_token(path: str | Path) -> str:
    token_path = Path(path).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControlTokenError(
            f"cannot read control token {token_path}: {exc}"
        ) from exc
    if not token:
        raise ControlTokenError(f"control token {token_path} is empty")
    return token


def ensure_control_token(path: str | Path) -> tuple[str, bool]:
    """Return ``(token, created)``, generating a 0600 file when missing."""
    token_path = Path(path).expanduser()
    if token_path.exists():
        return read_control_token(token_path), False
    parent = token_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    token = os.urandom(32).hex()
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{token_path.name}.", suffix=".tmp", dir=str(parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, token_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return token, True


def _error(message: str, status: int, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _bearer_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, separator, presented = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    presented = presented.strip()
    return presented or None


def _normalize_health(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True  # no explicit failure signal
    if isinstance(value, str):
        return value.strip().lower() in _HEALTHY_WORDS
    if isinstance(value, dict):
        for key in ("healthy", "ok", "alive", "connected"):
            if key in value and isinstance(value[key], bool):
                return value[key]
        status = value.get("status")
        if isinstance(status, str):
            return status.strip().lower() in _HEALTHY_WORDS
        return not bool(value.get("error"))
    if isinstance(value, (tuple, list)) and value:
        return _normalize_health(value[0])
    return True


async def _is_healthy(runtime: PersonalityRuntime) -> bool:
    check = getattr(runtime.bot, "health_check", None)
    if not callable(check):
        return True  # bots without the hook are unmonitored, not down
    try:
        result = check()
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        logger.warning(
            "health_check failed for %s", runtime.name, exc_info=True,
        )
        return False
    return _normalize_health(result)


class ControlPlane:
    """Request handlers plus the AppRunner/TCPSite lifecycle."""

    def __init__(
        self,
        *,
        config: Config,
        outbox: Outbox,
        runtimes: Mapping[str, PersonalityRuntime],
        token: str,
        host: str = _HOST,
        port: int | None = None,
    ) -> None:
        if not token:
            raise ValueError("control token must be non-empty")
        self.config = config
        self.outbox = outbox
        self.runtimes = dict(runtimes)
        #: Compare against bytes so compare_digest is not accidentally
        #: comparing two differently-typed strings.
        self._token = token.encode("utf-8")
        self.host = host
        self.port = config.control_port if port is None else int(port)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # -- app construction ------------------------------------------------------

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/dm", self.handle_dm)
        app.router.add_get("/v1/health", self.handle_health)
        return app

    # -- auth ------------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        presented = _bearer_token(request)
        if presented is None:
            return False
        return hmac.compare_digest(
            presented.encode("utf-8"), self._token,
        )

    # -- POST /v1/dm -----------------------------------------------------------

    async def handle_dm(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return _error("unauthorized", 401)

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error("invalid JSON body", 400)
        if not isinstance(body, dict):
            return _error("JSON body must be an object", 400)

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("text is required and must be non-empty", 400)
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return _error("text exceeds 64 KB", 400)

        requested = body.get("personality")
        if requested is not None and not isinstance(requested, str):
            return _error("personality must be a string", 400)
        requested = (requested or "").strip() or None
        if requested is None:
            if len(self.runtimes) == 1:
                personality = next(iter(self.runtimes))
            else:
                return _error(
                    "personality is required when several are configured", 400,
                )
        else:
            personality = requested
        runtime = self.runtimes.get(personality)
        if runtime is None:
            return _error(f"unknown personality {personality!r}", 403)

        expiry = body.get("expiry_seconds")
        if expiry is not None and (
            isinstance(expiry, bool) or not isinstance(expiry, int)
        ):
            return _error("expiry_seconds must be an integer", 400)
        if expiry is None:
            expiry = self.config.outbound_expiry_max_seconds

        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and not isinstance(
            idempotency_key, str,
        ):
            return _error("idempotency_key must be a string", 400)
        idempotency_key = (idempotency_key or "").strip() or None

        stored = self.outbox.idempotent_response(idempotency_key)
        if stored is not None:
            return web.json_response({**stored, "duplicate": True}, status=202)

        try:
            message_id, chunks = self.outbox.enqueue(
                personality, text, expiry,
            )
        except RateLimited as exc:
            logger.warning(
                "Outbound rate cap hit for %s (%d/min)",
                personality, exc.limit,
            )
            return _error(
                "rate limit exceeded", 429, retry_after=60, limit=exc.limit,
            )

        response: dict[str, Any] = {
            "accepted": True,
            "message_id": message_id,
            "chunks": chunks,
            "duplicate": False,
        }
        # Remember before the health await so a concurrent retry with the same
        # key cannot enqueue a second copy.
        self.outbox.remember_response(idempotency_key, response)

        healthy = await _is_healthy(runtime)
        if not healthy:
            response["queued_for_retry"] = True
            self.outbox.remember_response(idempotency_key, response)
            logger.warning(
                "Queued outbound message for %s; Nostr connection is unhealthy",
                personality,
            )
            return web.json_response(response, status=503)

        logger.info(
            "Accepted outbound message %s for %s (%d chunk(s))",
            message_id[:8], personality, chunks,
        )
        return web.json_response(response, status=202)

    # -- GET /v1/health --------------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return _error("unauthorized", 401)

        names = list(self.runtimes)
        health = await asyncio.gather(
            *(_is_healthy(self.runtimes[name]) for name in names),
        )
        return web.json_response([
            {
                "name": name,
                "healthy": healthy,
                "relay_count": self.runtimes[name].relay_count,
                "watchdog": self.runtimes[name].watchdog_state,
            }
            for name, healthy in zip(names, health)
        ])

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        app = self.create_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            "Control plane listening on http://%s:%d", self.host, self.port,
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def create_app(
    *,
    config: Config,
    outbox: Outbox,
    runtimes: Mapping[str, PersonalityRuntime],
    token: str,
) -> web.Application:
    """Build the aiohttp app (tests use this with TestClient)."""
    return ControlPlane(
        config=config,
        outbox=outbox,
        runtimes=runtimes,
        token=token,
    ).create_app()


__all__ = [
    "ControlPlane",
    "ControlTokenError",
    "MAX_TEXT_BYTES",
    "create_app",
    "ensure_control_token",
    "read_control_token",
]
