"""Persisted outbound queue and idempotency ledger.

The control plane accepts a DM, chunks it once at enqueue time, records the
message in an on-disk FIFO, and returns immediately. A background worker
drains each personality's queue in order through a caller-supplied delivery
callback. Failures are retried with exponential backoff; a message is dropped
after five failed attempts.

The idempotency ledger is persisted in the same atomically-replaced JSON
document as the queue (mkstemp + fsync + ``os.replace``, mode 0600), exactly
like :class:`nostr_hermes_proxy.sessions.SessionMap`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .chunking import truncate_message

logger = logging.getLogger(__name__)

STATE_VERSION = 1
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
RATE_WINDOW_SECONDS = 60.0
MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0

DeliveryCallback = Callable[[str, str, "int | None"], Awaitable[Any]]


class RateLimited(Exception):
    """The per-personality send-rate cap was hit."""

    def __init__(self, personality: str, limit: int) -> None:
        self.personality = personality
        self.limit = limit
        super().__init__(
            f"rate limit exceeded for personality {personality!r} "
            f"({limit}/minute)"
        )


@dataclass
class OutboxMessage:
    """One accepted outbound DM, chunked once at enqueue time."""

    message_id: str
    personality: str
    text: str
    expiry_seconds: int | None
    chunks: list[str] = field(default_factory=list)
    created_at: float = 0.0
    attempts: int = 0
    next_attempt_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "personality": self.personality,
            "text": self.text,
            "expiry_seconds": self.expiry_seconds,
            "chunks": list(self.chunks),
            "created_at": self.created_at,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "OutboxMessage | None":
        if not isinstance(raw, dict):
            return None
        message_id = raw.get("message_id")
        personality = raw.get("personality")
        text = raw.get("text")
        if not (
            isinstance(message_id, str)
            and message_id
            and isinstance(personality, str)
            and personality
            and isinstance(text, str)
        ):
            return None
        expiry = raw.get("expiry_seconds")
        if expiry is not None:
            if isinstance(expiry, bool) or not isinstance(expiry, int):
                expiry = None
        chunks = raw.get("chunks")
        if not isinstance(chunks, list) or not all(
            isinstance(chunk, str) for chunk in chunks
        ):
            chunks = truncate_message(text)
        try:
            created_at = float(raw.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0
        try:
            attempts = max(0, int(raw.get("attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        try:
            next_attempt_at = float(raw.get("next_attempt_at") or 0.0)
        except (TypeError, ValueError):
            next_attempt_at = 0.0
        return cls(
            message_id=message_id,
            personality=personality,
            text=text,
            expiry_seconds=expiry,
            chunks=list(chunks),
            created_at=created_at,
            attempts=attempts,
            next_attempt_at=next_attempt_at,
        )


class Outbox:
    """Thread-safe queue + idempotency ledger with an optional JSON backing."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        max_attempts: int = MAX_DELIVERY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if rate_limit_per_minute <= 0:
            raise ValueError("rate_limit_per_minute must be > 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0")
        self.path = Path(path).expanduser() if path is not None else None
        self.rate_limit_per_minute = int(rate_limit_per_minute)
        self.max_attempts = int(max_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._clock = clock
        self._queues: dict[str, deque[OutboxMessage]] = {}
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._send_times: dict[str, deque[float]] = {}
        self._lock = threading.RLock()
        self._drain_lock: asyncio.Lock | None = None
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._load()

    # -- queue reads -----------------------------------------------------------

    def pending_count(self, personality: str | None = None) -> int:
        with self._lock:
            if personality is not None:
                return len(self._queues.get(personality, ()))
            return sum(len(queue) for queue in self._queues.values())

    def queued(self, personality: str | None = None) -> list[OutboxMessage]:
        """Snapshot of the queue (oldest first), for tests and diagnostics."""
        with self._lock:
            if personality is not None:
                return list(self._queues.get(personality, ()))
            return [
                message
                for queue in self._queues.values()
                for message in queue
            ]

    # -- enqueue ---------------------------------------------------------------

    def enqueue(
        self,
        personality: str,
        text: str,
        expiry_seconds: int | None,
    ) -> tuple[str, int]:
        """Queue ``text`` for ``personality``; returns (message id, chunks)."""
        chunks = truncate_message(text)
        with self._lock:
            self._check_rate_locked(personality)
            message = OutboxMessage(
                message_id=uuid.uuid4().hex,
                personality=personality,
                text=text,
                expiry_seconds=expiry_seconds,
                chunks=chunks,
                created_at=self._clock(),
            )
            self._queues.setdefault(personality, deque()).append(message)
            self._write_locked()
        self._wake.set()
        return message.message_id, len(chunks)

    # -- idempotency -----------------------------------------------------------

    def idempotent_response(self, key: str | None) -> dict[str, Any] | None:
        """The stored response for ``key`` while it is younger than 24 h."""
        if not key:
            return None
        now = self._clock()
        with self._lock:
            entry = self._idempotency.get(key)
            if entry is None:
                return None
            if now - float(entry.get("stored_at", 0.0)) > IDEMPOTENCY_TTL_SECONDS:
                self._idempotency.pop(key, None)
                self._write_locked()
                return None
            response = entry.get("response")
            return dict(response) if isinstance(response, dict) else None

    def remember_response(self, key: str | None, response: dict[str, Any]) -> None:
        """Persist the response returned for ``key`` (no-op without a key)."""
        if not key:
            return
        with self._lock:
            self._idempotency[key] = {
                "stored_at": self._clock(),
                "response": dict(response),
            }
            self._prune_idempotency_locked()
            self._write_locked()

    # -- rate limiting ---------------------------------------------------------

    def _check_rate_locked(self, personality: str) -> None:
        now = self._clock()
        times = self._send_times.setdefault(personality, deque())
        cutoff = now - RATE_WINDOW_SECONDS
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) >= self.rate_limit_per_minute:
            raise RateLimited(personality, self.rate_limit_per_minute)
        times.append(now)

    # -- worker ----------------------------------------------------------------

    async def drain_once(self, deliver: DeliveryCallback) -> dict[str, int]:
        """Attempt every ready queued message once, FIFO per personality.

        A failed head-of-line message stays queued (preserving order) and gets
        a backoff deadline; the rest of that personality's queue waits for it.
        Returns ``{"sent", "retried", "dropped"}`` counts.
        """
        stats = {"sent": 0, "retried": 0, "dropped": 0}
        if self._drain_lock is None:
            self._drain_lock = asyncio.Lock()
        async with self._drain_lock:
            with self._lock:
                personalities = list(self._queues)
            for personality in personalities:
                await self._drain_personality(personality, deliver, stats)
        return stats

    async def _drain_personality(
        self,
        personality: str,
        deliver: DeliveryCallback,
        stats: dict[str, int],
    ) -> None:
        while True:
            with self._lock:
                queue = self._queues.get(personality)
                if not queue:
                    self._queues.pop(personality, None)
                    return
                message = queue[0]
                if message.next_attempt_at > self._clock():
                    return
            try:
                result = await deliver(
                    message.personality,
                    message.text,
                    message.expiry_seconds,
                )
                ok = result is not False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Outbox delivery failed for %s (message %s, attempt %d)",
                    personality, message.message_id[:8], message.attempts + 1,
                    exc_info=True,
                )
                ok = False

            with self._lock:
                queue = self._queues.get(personality)
                if not queue or queue[0] is not message:
                    # Should not happen: drain_once is serialized.
                    return
                if ok:
                    queue.popleft()
                    if not queue:
                        self._queues.pop(personality, None)
                    stats["sent"] += 1
                    self._write_locked()
                    continue

                message.attempts += 1
                if message.attempts >= self.max_attempts:
                    queue.popleft()
                    if not queue:
                        self._queues.pop(personality, None)
                    stats["dropped"] += 1
                    self._write_locked()
                    logger.error(
                        "Dropping outbound message %s for %s after %d failed "
                        "attempts",
                        message.message_id[:8], personality, message.attempts,
                    )
                    continue

                delay = self.retry_backoff_seconds * (
                    2 ** (message.attempts - 1)
                )
                message.next_attempt_at = self._clock() + delay
                stats["retried"] += 1
                self._write_locked()
                logger.warning(
                    "Requeued outbound message %s for %s after attempt %d "
                    "(retry in %.1fs)",
                    message.message_id[:8], personality, message.attempts, delay,
                )
                # Keep strict FIFO: do not skip past a failed message.
                return

    def start_worker(
        self,
        deliver: DeliveryCallback,
        *,
        interval_seconds: float = 1.0,
    ) -> asyncio.Task:
        """Start the drain task (woken by enqueue, plus a periodic retry poll)."""
        if self._worker is not None and not self._worker.done():
            return self._worker
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._worker = asyncio.create_task(
            self._worker_loop(deliver, interval_seconds),
            name="nostr_hermes_proxy_outbox",
        )
        return self._worker

    async def stop_worker(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _worker_loop(
        self,
        deliver: DeliveryCallback,
        interval_seconds: float,
    ) -> None:
        logger.info(
            "Outbox worker started (rate cap %d/min, %d attempts max)",
            self.rate_limit_per_minute, self.max_attempts,
        )
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.drain_once(deliver)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbox drain failed")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=interval_seconds,
                )
            except TimeoutError:
                pass
        logger.info("Outbox worker stopped")

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Outbox %s is unreadable (%s); starting empty", self.path, exc,
            )
            return
        if not isinstance(raw, dict):
            logger.warning("Outbox %s has no state table; starting empty", self.path)
            return

        now = self._clock()
        loaded_idempotency: dict[str, dict[str, Any]] = {}
        entries = raw.get("idempotency")
        if isinstance(entries, dict):
            for key, entry in entries.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                response = entry.get("response")
                if not isinstance(response, dict):
                    continue
                try:
                    stored_at = float(entry.get("stored_at") or 0.0)
                except (TypeError, ValueError):
                    continue
                if now - stored_at > IDEMPOTENCY_TTL_SECONDS:
                    continue
                loaded_idempotency[key] = {
                    "stored_at": stored_at,
                    "response": dict(response),
                }
        self._idempotency = loaded_idempotency

        loaded_queues: dict[str, deque[OutboxMessage]] = {}
        queue_entries = raw.get("queue")
        if isinstance(queue_entries, list):
            for entry in queue_entries:
                message = OutboxMessage.from_json(entry)
                if message is not None:
                    loaded_queues.setdefault(
                        message.personality, deque(),
                    ).append(message)
        self._queues = loaded_queues

    def _prune_idempotency_locked(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, entry in self._idempotency.items()
            if now - float(entry.get("stored_at", 0.0))
            > IDEMPOTENCY_TTL_SECONDS
        ]
        for key in expired:
            self._idempotency.pop(key, None)

    def _write_locked(self) -> None:
        if self.path is None:
            return
        self._prune_idempotency_locked()
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass
        payload = json.dumps(
            {
                "version": STATE_VERSION,
                "queue": [
                    message.to_json()
                    for queue in self._queues.values()
                    for message in queue
                ],
                "idempotency": self._idempotency,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:  # pragma: no cover - directory fsync is best effort
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "MAX_DELIVERY_ATTEMPTS",
    "Outbox",
    "OutboxMessage",
    "RateLimited",
]
