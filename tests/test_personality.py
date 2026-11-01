from __future__ import annotations

import asyncio

from nostr_hermes_proxy.hermes import (
    HermesError,
    HermesSessionGone,
    ModelOption,
    RunSubmission,
    TurnResult,
)

from .fakes import FakeBot, FakeCtx, make_harness


async def wait_for(predicate, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def test_non_allowlisted_sender_is_dropped(test_hex):
    harness = make_harness(allow_hex=test_hex)
    intruder = FakeCtx(sender_hex="ab" * 32, event_id="e")
    await harness.runtime.handle_dm(intruder, "hello")
    assert harness.bot.sent == []
    assert harness.hermes.submit_calls == []
    assert harness.hermes.ensure_calls == []


async def test_non_command_is_forwarded_and_replied(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "  hello world  ")
    assert harness.hermes.submit_calls == [
        ("atlas", "  hello world  ", "event-1"),
    ]
    assert harness.bot.sent[-1][1] == "hello"


async def test_missing_event_id_generates_one(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.ctx.event_id = None
    await harness.runtime.handle_dm(harness.ctx, "hello")
    generated = harness.hermes.submit_calls[0][2]
    assert isinstance(generated, str) and len(generated) == 32


async def test_run_id_is_tracked_during_turn_and_cleared(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.gate.clear()
    task = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "hello"))
    await harness.hermes.entered.wait()
    assert harness.runtime._run_id == "run-1"
    harness.hermes.gate.set()
    await task
    assert harness.runtime._run_id is None


async def test_help_command_is_local(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/HELP")
    assert harness.hermes.submit_calls == []
    assert harness.bot.sent_events == []
    assert "Commands:" in harness.bot.sent[-1][1]


async def test_unknown_command_is_local(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/deploy now")
    assert harness.hermes.submit_calls == []
    assert "Unknown command /deploy" in harness.bot.sent[-1][1]


async def test_model_list_is_a_numbered_user_defined_menu(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        providers=[
            {
                "slug": "aspark",
                "models": ["glm-5.3-flash", "glm-5.3"],
                "is_user_defined": True,
            },
            {
                "slug": "openrouter",
                "models": ["claude"],
                "is_user_defined": False,
            },
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "1. aspark/glm-5.3-flash" in reply
    assert "2. aspark/glm-5.3" in reply
    assert "openrouter/claude" not in reply
    assert "Reply /model # to switch." in reply
    assert harness.hermes.submit_calls == []


async def test_model_list_marks_the_current_selection(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        providers=[
            {
                "slug": "aspark",
                "models": ["glm-5.3-flash", "glm-5.3"],
                "is_user_defined": True,
            },
        ],
    )
    harness.sessions.update_model("atlas", "glm-5.3", "aspark")
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "2. aspark/glm-5.3 ← current" in reply
    assert "1. aspark/glm-5.3-flash ← current" not in reply


async def test_model_list_fetch_failure_sends_error_text(test_hex):
    harness = make_harness(
        allow_hex=test_hex, models_error=HermesError("gateway down"),
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    assert "Something went wrong talking to Hermes." in harness.bot.sent[-1][1]


async def test_model_list_with_no_user_defined_providers(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        providers=[
            {"slug": "openrouter", "models": ["claude"],
             "is_user_defined": False},
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "No user-defined providers or models" in reply
    assert "gateway config" in reply


async def test_model_number_switch_updates_and_persists(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        providers=[
            {
                "slug": "aspark",
                "models": ["glm-5.3-flash", "glm-5.3"],
                "is_user_defined": True,
            },
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    await harness.runtime.handle_dm(harness.ctx, "/model 2")

    assert "Model set to aspark/glm-5.3." in harness.bot.sent[-1][1]
    entry = harness.sessions.get("atlas")
    assert (entry.model, entry.provider) == ("glm-5.3", "aspark")

    # The preference is what the next run submission carries.
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.hermes.submitted_prefs[-1] == ("glm-5.3", "aspark")


async def test_model_number_without_a_menu_hints(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/model 2")
    assert "Send /model first to list available models." in (
        harness.bot.sent[-1][1]
    )
    assert harness.sessions.get("atlas") is None


async def test_model_number_out_of_range_hints(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        providers=[
            {"slug": "aspark", "models": ["glm-5.3-flash"],
             "is_user_defined": True},
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    await harness.runtime.handle_dm(harness.ctx, "/model 9")
    assert "No model #9" in harness.bot.sent[-1][1]
    assert "Send /model first" in harness.bot.sent[-1][1]
    assert harness.sessions.get("atlas") is None


async def test_model_menu_reads_gateway_config_live(test_hex, tmp_path):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n"
        "providers:\n"
        "  lspark:\n"
        "    models:\n"
        "      deepseek-v4.1-flash:\n"
        "        context_length: 655360\n"
        "  aspark:\n"
        "    models:\n"
        "      glm-5.3-flash:\n"
        "        context_length: 640000\n"
        "  huggingface:\n"
        "    enabled: false\n"
        "    models:\n"
        "      hf-model:\n"
        "        context_length: 1000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "1. lspark/deepseek-v4.1-flash" in reply
    assert "2. aspark/glm-5.3-flash" in reply
    assert "hf-model" not in reply  # disabled providers are skipped
    assert "Reply /model # to switch." in reply
    # The menu comes from the gateway's config.yaml, not the API inventory.
    assert harness.hermes.models_calls == 0

    # Live re-read, no caching: a provider added to the gateway config
    # shows up on the next /model — no proxy restart, no code change.
    with gateway_yaml.open("a", encoding="utf-8") as fh:
        fh.write(
            "  newprov:\n"
            "    models:\n"
            "      new-model:\n"
            "        context_length: 1000\n"
        )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    assert "3. newprov/new-model" in harness.bot.sent[-1][1]


async def test_model_menu_falls_back_to_api_without_gateway_config(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        gateway_config_path="/nonexistent/config.yaml",
        providers=[
            {"slug": "aspark", "models": ["glm-5.3-flash"],
             "is_user_defined": True},
        ],
    )
    await harness.runtime.handle_dm(harness.ctx, "/model")
    reply = harness.bot.sent[-1][1]
    assert "1. aspark/glm-5.3-flash" in reply
    assert harness.hermes.models_calls == 1


async def test_model_switch_after_restart_resolves_menu_and_context(
    test_hex, tmp_path,
):
    """Post-restart, "/model 2" rebuilds the menu on demand — no /model first."""
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "providers:\n"
        "  lspark:\n"
        "    models:\n"
        "      deepseek-v4.1-flash:\n"
        "        context_length: 655360\n"
        "  aspark:\n"
        "    models:\n"
        "      glm-5.3-flash:\n"
        "        context_length: 640000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    assert harness.runtime._model_menu == []  # fresh daemon: menu not loaded
    await harness.runtime.handle_dm(harness.ctx, "/model 2")
    entry = harness.sessions.get("atlas")
    assert (entry.model, entry.provider) == ("glm-5.3-flash", "aspark")
    reply = harness.bot.sent[-1][1]
    assert "Model set to aspark/glm-5.3-flash. Context: 640,000 tokens." in (
        reply
    )
    # The gateway API inventory was never needed.
    assert harness.hermes.models_calls == 0


async def test_model_switch_reply_includes_context(test_hex, tmp_path):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "providers:\n"
        "  openrouter:\n"
        "    models:\n"
        "      claude:\n"
        "        context_length: 200000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    await harness.runtime.handle_dm(harness.ctx, "/model openrouter/claude")
    assert (
        "Model set to openrouter/claude. Context: 200,000 tokens."
        in harness.bot.sent[-1][1]
    )


async def test_model_set_command_persists(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/model openrouter/claude")
    entry = harness.sessions.get("atlas")
    assert entry.model == "claude"
    assert entry.provider == "openrouter"
    assert "Model set to openrouter/claude." in harness.bot.sent[-1][1]
    assert harness.hermes.submit_calls == []


async def test_model_set_bad_usage(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/model /bad")
    assert "Usage:" in harness.bot.sent[-1][1]


async def test_new_command_swaps_session_and_echoes_model_and_context(
    test_hex, tmp_path,
):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n"
        "providers:\n"
        "  aspark:\n"
        "    models:\n"
        "      glm-5.3-flash:\n"
        "        context_length: 131072\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex,
        gateway_config_path=str(gateway_yaml),
    )
    harness.sessions.set(
        "atlas", "old-session", model="glm-5.3-flash", provider="aspark",
    )
    await harness.runtime.handle_dm(harness.ctx, "/new")
    entry = harness.sessions.get("atlas")
    assert entry.hermes_session_id == "sess-1-new"
    assert entry.model == "glm-5.3-flash"
    assert entry.provider == "aspark"
    assert harness.hermes.create_calls == ["atlas"]
    reply = harness.bot.sent[-1][1]
    assert "New conversation started." in reply
    assert "Model: aspark/glm-5.3-flash." in reply
    assert "Context: 131,072 tokens" in reply


async def test_new_command_unknown_context_variant(test_hex, tmp_path):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    harness.sessions.set("atlas", "old-session", model="mystery", provider="p")
    await harness.runtime.handle_dm(harness.ctx, "/new")
    reply = harness.bot.sent[-1][1]
    assert "Model: p/mystery." in reply
    # "mystery" is not the gateway default and no provider lists it, so the
    # per-model lookup misses and the top-level default must NOT be applied.
    assert "Context: unknown for this model" in reply


async def test_new_command_default_model_falls_back_to_gateway_context(
    test_hex, tmp_path,
):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    harness.sessions.set(
        "atlas", "old-session", model="glm-5.3-flash", provider="aspark",
    )
    await harness.runtime.handle_dm(harness.ctx, "/new")
    assert "Context: 640,000 tokens" in harness.bot.sent[-1][1]


async def test_new_command_surfaces_hermes_error_detail(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        create_error=HermesError(
            "create session failed (HTTP 400: Title 'nostr:Sam-I-Am' "
            "is already in use)",
        ),
    )
    await harness.runtime.handle_dm(harness.ctx, "/new")
    reply = harness.bot.sent[-1][1]
    assert "create session failed" in reply
    assert "already in use" in reply


async def test_new_command_resolves_gateway_default_model(test_hex, tmp_path):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    await harness.runtime.handle_dm(harness.ctx, "/new")
    entry = harness.sessions.get("atlas")
    assert (entry.model, entry.provider) == ("glm-5.3-flash", "aspark")
    reply = harness.bot.sent[-1][1]
    assert "Model: aspark/glm-5.3-flash." in reply
    assert "Context: 640,000 tokens" in reply


async def test_new_command_gateway_default_without_gateway_config(test_hex):
    harness = make_harness(allow_hex=test_hex)
    await harness.runtime.handle_dm(harness.ctx, "/new")
    reply = harness.bot.sent[-1][1]
    assert "Model: gateway default." in reply
    assert "Context: unknown for this model" in reply


async def test_status_command(test_hex, clock):
    harness = make_harness(allow_hex=test_hex, clock=clock)
    harness.sessions.set("atlas", "sess-1", model="claude", provider="openrouter")
    harness.runtime.set_watchdog_status_provider(lambda: "ok")
    await harness.runtime.handle_dm(harness.ctx, "/status")
    reply = harness.bot.sent[-1][1]
    assert "Personality: atlas" in reply
    assert "Model: openrouter/claude" in reply
    assert "Session: not started" not in reply
    assert "State: idle" in reply
    assert "Context: no turns yet" in reply
    assert "Queue: 0 waiting" in reply
    assert "Relays: 2" in reply
    assert "Watchdog: ok" in reply
    assert harness.hermes.submit_calls == []


async def test_status_context_uses_last_turn_usage_and_gateway_total(
    test_hex, clock, tmp_path,
):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "providers:\n"
        "  openrouter:\n"
        "    models:\n"
        "      claude:\n"
        "        context_length: 131072\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex,
        clock=clock,
        gateway_config_path=str(gateway_yaml),
    )
    harness.sessions.set(
        "atlas", "sess-1", model="claude", provider="openrouter",
    )
    harness.sessions.update_usage("atlas", 45230)
    await harness.runtime.handle_dm(harness.ctx, "/status")
    reply = harness.bot.sent[-1][1]
    assert "Context: 45,230 / 131,072 tokens (35%)" in reply
    assert "State: idle" in reply


async def test_status_context_without_configured_total(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.sessions.set("atlas", "sess-1", model="claude", provider="p")
    harness.sessions.update_usage("atlas", 45230)
    await harness.runtime.handle_dm(harness.ctx, "/status")
    assert "Context: 45,230 tokens (total unknown)" in (
        harness.bot.sent[-1][1]
    )


async def test_status_resolves_gateway_default_model(test_hex, tmp_path):
    gateway_yaml = tmp_path / "config.yaml"
    gateway_yaml.write_text(
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n",
        encoding="utf-8",
    )
    harness = make_harness(
        allow_hex=test_hex, gateway_config_path=str(gateway_yaml),
    )
    await harness.runtime.handle_dm(harness.ctx, "/status")
    reply = harness.bot.sent[-1][1]
    assert "Model: aspark/glm-5.3-flash" in reply
    assert "Context: no turns yet" in reply


async def test_status_reports_busy_during_a_turn(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.gate.clear()
    task = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    await harness.runtime.handle_dm(harness.ctx, "/status")
    assert "State: busy (turn in flight)" in harness.bot.sent[-1][1]

    harness.hermes.gate.set()
    await task
    await harness.runtime.handle_dm(harness.ctx, "/status")
    assert "State: idle" in harness.bot.sent[-1][1]


# -- steering ------------------------------------------------------------------


async def test_second_dm_during_turn_steers_and_is_acked_once(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    second_ctx = FakeCtx(sender_hex=test_hex, event_id="event-2")
    await harness.runtime.handle_dm(second_ctx, "two")

    assert harness.hermes.steer_calls == [("run-1", "two")]
    assert len(harness.hermes.submit_calls) == 1
    # Both accepted DMs were acked, including the steered one.
    assert len(harness.bot.sent_events) == 2
    assert harness.activity == ["ack", "submit", "ack", "steer"]
    assert harness.runtime.queue_depth == 0

    harness.hermes.gate.set()
    await first
    assert [text for _, text in harness.bot.sent] == ["hello"]


async def test_steer_refused_falls_back_to_queued_turn(test_hex):
    harness = make_harness(allow_hex=test_hex, steer_result=False)
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    second_ctx = FakeCtx(sender_hex=test_hex, event_id="event-2")
    second = asyncio.create_task(
        harness.runtime.handle_dm(second_ctx, "two"),
    )
    await wait_for(lambda: harness.runtime.queue_depth == 1)
    assert harness.hermes.steer_calls == [("run-1", "two")]
    assert len(harness.hermes.submit_calls) == 1
    assert harness.hermes.max_active == 1

    harness.hermes.gate.set()
    await asyncio.gather(first, second)
    assert [call[1] for call in harness.hermes.submit_calls] == ["one", "two"]
    assert harness.hermes.max_active == 1
    assert len(harness.bot.sent) == 2
    assert harness.runtime.queue_depth == 0


async def test_steer_exception_falls_back_to_new_turn(test_hex):
    harness = make_harness(allow_hex=test_hex)
    harness.hermes.steer_results = [RuntimeError("gateway hiccup")]
    harness.hermes.gate.clear()

    first = asyncio.create_task(harness.runtime.handle_dm(harness.ctx, "one"))
    await harness.hermes.entered.wait()

    second_ctx = FakeCtx(sender_hex=test_hex, event_id="event-2")
    second = asyncio.create_task(
        harness.runtime.handle_dm(second_ctx, "two"),
    )
    await wait_for(lambda: harness.runtime.queue_depth == 1)

    harness.hermes.gate.set()
    await asyncio.gather(first, second)
    assert [call[1] for call in harness.hermes.submit_calls] == ["one", "two"]


# -- duplicate replay ----------------------------------------------------------


async def test_replayed_duplicate_sends_nothing(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        submissions=[RunSubmission(run_id="run-1", replayed=True)],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    # Only the immediate ack; a replay does not produce a reply.
    assert len(harness.bot.sent_events) == 1
    assert harness.bot.sent == []
    assert harness.hermes.stream_calls == []
    assert harness.sessions.get("atlas").last_input_tokens is None


# -- errors / replies ----------------------------------------------------------


async def test_hermes_error_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        submissions=[HermesError("gateway down")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    reply = harness.bot.sent[-1][1]
    assert "Something went wrong talking to Hermes." in reply


async def test_empty_reply_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("   ", status="completed")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert "empty reply" in harness.bot.sent[-1][1]


async def test_failed_run_sends_error_dm(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("", status="failed", error="model exploded")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    reply = harness.bot.sent[-1][1]
    assert "model exploded" in reply


# -- usage capture -------------------------------------------------------------


async def test_completed_turn_persists_input_tokens(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("hi", status="completed", input_tokens=45230)],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.sessions.get("atlas").last_input_tokens == 45230


async def test_completed_turn_without_usage_persists_none(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("hi", status="completed")],
    )
    harness.sessions.set("atlas", "sess-1")
    harness.sessions.update_usage("atlas", 999)
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.sessions.get("atlas").last_input_tokens is None


async def test_failed_turn_does_not_overwrite_usage(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[
            TurnResult(
                "", status="failed", error="boom", input_tokens=123,
            ),
        ],
    )
    harness.sessions.set("atlas", "sess-1")
    harness.sessions.update_usage("atlas", 111)
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert harness.sessions.get("atlas").last_input_tokens == 111


async def test_long_reply_is_chunked(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        results=[TurnResult("x" * 5000, status="completed")],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert len(harness.bot.sent) == 2
    assert harness.bot.sent[0][1].endswith(" (1/2)")
    assert harness.bot.sent[1][1].endswith(" (2/2)")


async def test_session_gone_is_recreated_and_retried(test_hex):
    harness = make_harness(
        allow_hex=test_hex,
        submissions=[HermesSessionGone("gone"), None],
    )
    await harness.runtime.handle_dm(harness.ctx, "hello")
    assert len(harness.hermes.submit_calls) == 2
    assert harness.bot.sent[-1][1] == "hello"
    assert harness.sessions.get("atlas").hermes_session_id is not None
    assert harness.hermes.ensure_calls == ["atlas", "atlas"]


async def test_command_reply_is_chunked_if_long(test_hex):
    options = [ModelOption("p", f"model-{index}") for index in range(500)]
    harness = make_harness(allow_hex=test_hex, models=options)
    await harness.runtime.handle_dm(harness.ctx, "/model")
    assert len(harness.bot.sent) >= 1
    assert all(len(text) <= 4096 for _, text in harness.bot.sent)


async def test_start_and_stop_are_delegated(test_hex):
    bot = FakeBot()
    harness = make_harness(allow_hex=test_hex, bot=bot)
    await harness.runtime.start()
    assert bot.started == 1
    assert bot.dm_handler == harness.runtime.handle_dm
    await harness.runtime.stop()
    assert bot.stopped == 1


def test_build_bot_passes_watchdog_stall_seconds(test_hex, monkeypatch):
    """nostrbot-sdk >= 0.6.0 performs stall detection inside health_check()
    when NostrBotConfig.watchdog_stall_seconds is set. The runtime must plumb
    the proxy-level knob into the bot config so the SDK's health_check() is
    the single source of truth the process watchdog polls."""
    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeBot:
        def __init__(self, config):
            captured["bot_config"] = config

        def on_dm(self, handler):
            pass

    import nostrbot_sdk

    monkeypatch.setattr(nostrbot_sdk, "NostrBot", _FakeBot)
    monkeypatch.setattr(nostrbot_sdk, "NostrBotConfig", _FakeConfig)

    from nostr_hermes_proxy.config import PersonalityConfig
    from nostr_hermes_proxy.personality import PersonalityRuntime
    from nostr_hermes_proxy.sessions import SessionMap
    from .fakes import FakeHermes

    config = PersonalityConfig(
        name="atlas",
        nsec="nsec1synthetictestkeynotreal",
        relays=("wss://nos.lol",),
        model=None,
        provider=None,
    )
    PersonalityRuntime(
        config,
        allow_hex=test_hex,
        hermes=FakeHermes(SessionMap(None)),
        sessions=SessionMap(None),
        watchdog_stall_seconds=7200.0,
    )
    assert captured["watchdog_stall_seconds"] == 7200.0
    assert captured["bot_config"] is not None
