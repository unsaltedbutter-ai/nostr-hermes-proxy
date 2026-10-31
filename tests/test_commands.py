from __future__ import annotations

import pytest

from nostr_hermes_proxy.commands import (
    MAX_MODELS_SHOWN,
    ModelSelection,
    error_text,
    format_model_label,
    format_session_age,
    help_text,
    model_set_text,
    models_text,
    new_text,
    parse_command,
    parse_model_selection,
    status_text,
    unknown_text,
)
from nostr_hermes_proxy.hermes import ModelOption


def test_non_command_is_none():
    assert parse_command("hello there") is None
    assert parse_command("") is None
    assert parse_command("it /new is not a command") is None


def test_command_case_insensitive_and_args():
    assert parse_command("/NEW").name == "new"
    assert parse_command("/MoDeL openrouter/x").name == "model"
    assert parse_command("/MoDeL openrouter/x").args == "openrouter/x"
    assert parse_command("  /status  ").name == "status"


def test_unknown_command_still_parsed_locally():
    command = parse_command("/deploy now")
    assert command.name == "deploy"
    assert command.args == "now"


def test_bare_slash_is_unknown():
    command = parse_command("/")
    assert command is not None
    assert command.name == ""
    assert "Unknown command" in unknown_text(command.name)


def test_reply_builders():
    assert "/new" in help_text()
    assert new_text() == "New conversation started."
    assert unknown_text("nope") == (
        "Unknown command /nope. Send /help for the command list."
    )
    assert model_set_text("claude", "openrouter") == (
        "Model set to openrouter/claude."
    )
    assert model_set_text("openrouter/claude", "openrouter") == (
        "Model set to openrouter/claude."
    )
    assert format_model_label(None, None) == "gateway default"


def test_parse_model_selection_forms():
    assert parse_model_selection("openrouter/claude") == ModelSelection(
        model="claude", provider="openrouter",
    )
    assert parse_model_selection("claude") == ModelSelection(model="claude")
    assert parse_model_selection("openrouter claude sonnet") == ModelSelection(
        model="claude sonnet", provider="openrouter",
    )
    with pytest.raises(ValueError):
        parse_model_selection("")
    with pytest.raises(ValueError):
        parse_model_selection("/claude")


def test_models_text_formats_and_caps():
    options = [
        ModelOption("openrouter", f"model-{index}")
        for index in range(MAX_MODELS_SHOWN + 5)
    ]
    text = models_text(options)
    assert text.startswith(f"Models ({MAX_MODELS_SHOWN + 5}):")
    assert "- openrouter/model-0" in text
    assert "... and 5 more" in text
    assert text.count("- ") == MAX_MODELS_SHOWN + 1
    assert "Use /model provider/model" in text


def test_models_text_handles_dicts_and_empty():
    assert models_text([]) == "No models available."
    text = models_text([
        {"provider": "p", "model": "m"},
        {"id": "n"},
        "bare-model",
    ])
    assert "- p/m" in text
    assert "- n" in text
    assert "- bare-model" in text


def test_session_age_format():
    assert format_session_age(None) == "not started"
    assert format_session_age(42) == "42s"
    assert format_session_age(125) == "2m"
    assert format_session_age(3660) == "1h 1m"
    assert format_session_age(90000) == "1d 1h"


def test_status_text_contains_fields():
    text = status_text(
        name="atlas",
        model="claude",
        provider="openrouter",
        session_age_seconds=125,
        queue_depth=2,
        relay_count=3,
        watchdog_state="ok",
    )
    assert "Personality: atlas" in text
    assert "Model: openrouter/claude" in text
    assert "Session: 2m" in text
    assert "Queue: 2 waiting" in text
    assert "Relays: 3" in text
    assert "Watchdog: ok" in text


def test_error_text_is_short_and_optional_detail():
    assert error_text().startswith("Something went wrong talking to Hermes.")
    assert "detail" in error_text("detail")
