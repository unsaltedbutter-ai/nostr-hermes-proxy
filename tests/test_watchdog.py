from __future__ import annotations

import pytest

from nostr_hermes_proxy.watchdog import Watchdog


class Named:
    def __init__(self, name: str, bot) -> None:
        self.name = name
        self.bot = bot


class NoHooksBot:
    pass


class HealthBot:
    def __init__(self, value=True) -> None:
        self.value = value
        self.restart_calls = 0

    def health_check(self):
        return self.value

    async def restart(self) -> None:
        self.restart_calls += 1


class ActivityBot:
    def __init__(self, activity: float) -> None:
        self.last_activity_monotonic = activity


async def test_healthy_personality_resets_state():
    bot = HealthBot(True)
    watchdog = Watchdog([Named("atlas", bot)], clock=lambda: 100.0)
    states = await watchdog.check_once()
    assert states["atlas"] == "ok"


async def test_unhealthy_restarts_and_escalates_at_threshold():
    bot = HealthBot(False)
    exits: list[int] = []
    watchdog = Watchdog(
        [Named("atlas", bot)],
        failure_threshold=3,
        clock=lambda: 100.0,
        exit_fn=exits.append,
    )
    await watchdog.check_once()
    await watchdog.check_once()
    assert bot.restart_calls == 2
    assert exits == []
    assert watchdog.state_for("atlas") == "unhealthy (2/3)"
    await watchdog.check_once()
    assert bot.restart_calls == 3
    assert exits == [1]
    assert watchdog.state_for("atlas") == "escalated"
    # No repeated process exits after escalation.
    await watchdog.check_once()
    assert exits == [1]


async def test_healthy_poll_between_failures_resets_counter():
    bot = HealthBot(False)
    exits: list[int] = []
    watchdog = Watchdog(
        [Named("atlas", bot)],
        failure_threshold=2,
        clock=lambda: 100.0,
        exit_fn=exits.append,
    )
    await watchdog.check_once()
    bot.value = True
    await watchdog.check_once()
    bot.value = False
    await watchdog.check_once()
    assert exits == []
    assert watchdog.state_for("atlas") == "unhealthy (1/2)"


async def test_on_escalate_callback_replaces_exit():
    bot = HealthBot(False)
    escalated: list[str] = []
    watchdog = Watchdog(
        [Named("atlas", bot)],
        failure_threshold=1,
        clock=lambda: 100.0,
        on_escalate=escalated.append,
        exit_fn=lambda code: (_ for _ in ()).throw(AssertionError("exit called")),
    )
    await watchdog.check_once()
    assert escalated == ["atlas"]
    assert watchdog.state_for("atlas") == "escalated"


async def test_no_hooks_is_inert_and_reported():
    watchdog = Watchdog(
        [Named("atlas", NoHooksBot())],
        failure_threshold=1,
        clock=lambda: 100.0,
        exit_fn=lambda code: (_ for _ in ()).throw(AssertionError("exit called")),
    )
    states = await watchdog.check_once()
    assert states["atlas"] == "no health hook"


async def test_last_activity_stall_detection():
    bot = ActivityBot(activity=100.0)
    exits: list[int] = []
    watchdog = Watchdog(
        [Named("atlas", bot)],
        failure_threshold=1,
        stall_seconds=300,
        clock=lambda: 500.0,
        exit_fn=exits.append,
    )
    assert (await watchdog.check_once())["atlas"] == "escalated"
    assert exits == [1]

    fresh = Watchdog(
        [Named("atlas", ActivityBot(activity=400.0))],
        stall_seconds=300,
        clock=lambda: 500.0,
    )
    assert (await fresh.check_once())["atlas"] == "ok"


async def test_health_check_exception_counts_as_failure():
    class Broken:
        def health_check(self):
            raise RuntimeError("nope")

    exits: list[int] = []
    watchdog = Watchdog(
        [Named("atlas", Broken())],
        failure_threshold=1,
        clock=lambda: 0.0,
        exit_fn=exits.append,
    )
    await watchdog.check_once()
    assert exits == [1]


async def test_dict_health_payload():
    class DictBot:
        def health_check(self):
            return {"healthy": False, "status": "degraded"}

    exits: list[int] = []
    watchdog = Watchdog(
        [Named("atlas", DictBot())],
        failure_threshold=1,
        clock=lambda: 0.0,
        exit_fn=exits.append,
    )
    await watchdog.check_once()
    assert exits == [1]


def test_invalid_knobs_rejected():
    with pytest.raises(ValueError):
        Watchdog([], interval_seconds=0)
    with pytest.raises(ValueError):
        Watchdog([], failure_threshold=0)
