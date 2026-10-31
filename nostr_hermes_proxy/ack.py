"""Ack policy: immediate, delayed, or none.

DESIGN.md §3: ack on accept, or ack only if the turn hasn't replied within a
configurable delay (avoids ack + answer as two messages on a fast turn).

``AckPolicy`` is deliberately I/O-free: the caller supplies the send coroutine
and (for tests) the sleep coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

AckSender = Callable[[str], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]
AckTextBuilder = Callable[[str], str]

ACK_PREFIX = "Working on "
SNIPPET_LIMIT = 60
ELLIPSIS = "\u2026"


def build_ack_text(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """'Working on <first ~60 chars>…' with whitespace collapsed."""
    snippet = " ".join((text or "").split()).strip()
    if not snippet:
        return "Working on it" + ELLIPSIS
    if len(snippet) > limit:
        snippet = snippet[:limit].rstrip() + ELLIPSIS
    return f"{ACK_PREFIX}{snippet}"


@dataclass
class PendingAck:
    """Handle to one turn's ack task."""

    task: asyncio.Task | None = None
    sent: bool = False

    async def cancel(self) -> bool:
        """Cancel a pending ack. Returns True if it had already been sent."""
        if self.task is None:
            return self.sent
        if not self.task.done():
            self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - send errors are swallowed below
            logger.warning("Ack task failed", exc_info=True)
        return self.sent

    async def wait(self) -> bool:
        if self.task is not None:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        return self.sent


class AckPolicy:
    """Builds ack tasks according to ``delay_seconds``.

    ``delay_seconds == 0`` sends the ack immediately (on accept). A positive
    value waits that long; the reply path calls :meth:`PendingAck.cancel`
    before sending the answer.
    """

    def __init__(
        self,
        delay_seconds: float,
        *,
        sleep: Sleeper = asyncio.sleep,
        builder: AckTextBuilder = build_ack_text,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("ack delay must be >= 0")
        self.delay_seconds = float(delay_seconds)
        self._sleep = sleep
        self._builder = builder
        self._clock = clock

    async def arm(self, text: str, send: AckSender) -> PendingAck:
        """Schedule (or immediately send) the ack for an accepted turn."""
        ack_text = self._builder(text)
        if self.delay_seconds == 0:
            sent = await self._safe_send(send, ack_text)
            return PendingAck(task=None, sent=sent)
        pending = PendingAck()
        pending.task = asyncio.create_task(
            self._delayed(ack_text, send, pending),
            name="nostr_hermes_proxy_ack",
        )
        return pending

    async def _delayed(
        self, ack_text: str, send: AckSender, pending: PendingAck,
    ) -> None:
        await self._sleep(self.delay_seconds)
        pending.sent = await self._safe_send(send, ack_text)

    @staticmethod
    async def _safe_send(send: AckSender, text: str) -> bool:
        try:
            await send(text)
            return True
        except Exception:
            # An ack is best effort; never take down the turn for it.
            logger.warning("Could not send ack DM", exc_info=True)
            return False


__all__ = [
    "ACK_PREFIX",
    "AckPolicy",
    "PendingAck",
    "build_ack_text",
]
