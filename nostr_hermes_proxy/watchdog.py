"""Process-local supervision for each personality's NostrBot.

nostrbot-sdk v0.6.0 ships the hooks this watchdog consumes:
``health_check()`` (notification loop alive, a relay connectable, and — when
``NostrBotConfig.watchdog_stall_seconds`` is set — no stalled frame window)
and ``restart()`` (soft reconnect without republishing identity events).
The personality runtime plumbs the proxy's ``watchdog_stall_seconds`` config
into the bot so the SDK's health_check() performs stall detection itself.

For bots without the hooks (older SDKs or test fakes) this watchdog degrades
gracefully: with only ``last_activity_monotonic`` it applies its own
``stall_seconds`` window; with neither hook it marks the personality
unmonitored ("no health hook") and stays inert.

On an unhealthy poll: log, call ``restart()`` if present, and after N
consecutive failed cycles escalate to a full process exit so the LaunchAgent
can restart us cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_STALL_SECONDS = 2 * 60 * 60
_HEALTHY_WORDS = {"ok", "healthy", "connected", "ready", "up"}


def _normalize_health(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        # An explicit None is not a failure signal; avoid false escalations.
        return True
    if isinstance(value, str):
        return value.strip().lower() in _HEALTHY_WORDS
    if isinstance(value, dict):
        for key in ("healthy", "ok", "alive", "connected"):
            if key in value and isinstance(value[key], bool):
                return value[key]
        status = value.get("status")
        if isinstance(status, str):
            return status.strip().lower() in _HEALTHY_WORDS
        error = value.get("error")
        return not bool(error)
    if isinstance(value, (tuple, list)) and value:
        return _normalize_health(value[0])
    return True


class Watchdog:
    """Polls every personality on one shared cadence."""

    def __init__(
        self,
        personalities: Iterable[Any],
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        clock: Callable[[], float] | None = None,
        exit_fn: Callable[[int], Any] = os._exit,
        on_escalate: Callable[[str], Any] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.personalities = list(personalities)
        self.interval_seconds = float(interval_seconds)
        self.failure_threshold = int(failure_threshold)
        self.stall_seconds = float(stall_seconds)
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._exit_fn = exit_fn
        self._on_escalate = on_escalate
        self._failures: dict[str, int] = {
            personality.name: 0 for personality in self.personalities
        }
        self._states: dict[str, str] = {
            personality.name: "starting" for personality in self.personalities
        }
        self._escalated: set[str] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> "Watchdog":
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="nostr_hermes_proxy_watchdog",
            )
        return self

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        logger.info(
            "Watchdog polling %d personality(ies) every %.0fs (threshold %d)",
            len(self.personalities), self.interval_seconds, self.failure_threshold,
        )
        while not self._stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watchdog cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds,
                )
            except TimeoutError:
                pass

    # -- health ----------------------------------------------------------------

    def state_for(self, name: str) -> str:
        return self._states.get(name, "unknown")

    async def check_once(self) -> dict[str, str]:
        for personality in self.personalities:
            await self._check_personality(personality)
        return dict(self._states)

    async def _check_personality(self, personality: Any) -> None:
        name = personality.name
        if name in self._escalated:
            return
        healthy, detail, monitored = await self._healthy(personality)
        if healthy:
            self._failures[name] = 0
            self._states[name] = "ok" if monitored else "no health hook"
            return

        self._failures[name] += 1
        count = self._failures[name]
        self._states[name] = f"unhealthy ({count}/{self.failure_threshold})"
        logger.warning(
            "Personality %s is unhealthy (%d/%d)%s",
            name, count, self.failure_threshold,
            f": {detail}" if detail else "",
        )
        await self._maybe_restart(personality, name)
        if count >= self.failure_threshold:
            await self._escalate(name, detail)

    async def _healthy(self, personality: Any) -> tuple[bool, str, bool]:
        bot = getattr(personality, "bot", personality)
        check = getattr(bot, "health_check", None)
        if callable(check):
            try:
                result = check()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                return False, f"health_check raised {type(exc).__name__}", True
            return (
                _normalize_health(result),
                "health_check returned unhealthy",
                True,
            )

        last_activity = getattr(bot, "last_activity_monotonic", None)
        if last_activity is not None:
            try:
                value = last_activity() if callable(last_activity) else last_activity
                idle = self._clock() - float(value)
            except Exception as exc:
                return False, f"last_activity raised {type(exc).__name__}", True
            if idle > self.stall_seconds:
                return False, f"no activity for {idle:.0f}s", True
            return True, "", True

        return True, "", False

    async def _maybe_restart(self, personality: Any, name: str) -> None:
        bot = getattr(personality, "bot", personality)
        restart = getattr(bot, "restart", None)
        if not callable(restart):
            logger.warning(
                "Personality %s has no restart() hook; continuing", name,
            )
            return
        logger.warning("Restarting personality %s", name)
        self._states[name] = "restarting"
        try:
            result = restart()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "restart() for %s failed: %s", name, type(exc).__name__,
            )
            return
        # Keep the unhealthy marker visible until the next healthy poll.
        self._states[name] = (
            f"unhealthy ({self._failures[name]}/{self.failure_threshold})"
        )

    async def _escalate(self, name: str, detail: str) -> None:
        if name in self._escalated:
            return
        self._escalated.add(name)
        self._states[name] = "escalated"
        logger.critical(
            "Personality %s failed %d consecutive health checks%s; exiting "
            "so the LaunchAgent can restart the proxy",
            name, self.failure_threshold, f" ({detail})" if detail else "",
        )
        if self._on_escalate is not None:
            try:
                result = self._on_escalate(name)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("on_escalate callback failed")
            return
        self._exit_fn(1)


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_STALL_SECONDS",
    "Watchdog",
]
