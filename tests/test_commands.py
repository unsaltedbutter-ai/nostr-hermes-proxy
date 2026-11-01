from __future__ import annotations

import pytest

from nostr_hermes_proxy.commands import (
    MODEL_MENU_HINT,
    MODEL_MENU_PROMPT,
    NO_MODELS_TEXT,
    UNKNOWN_CONTEXT_HINT,
    ModelSelection,
    context_line,
    error_text,
    format_model_label,
    format_session_age,
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


def test_non_command_is_none():
    assert parse_command("hello there") is None
    assert parse_command("") is None
    assert parse_command("it /new is not a command") is None


def test_command_case_insensitive_and_args():
    assert parse_command("/NEW").name == "new"
    assert parse_command("/MoDeL openrouter/x").name == "model"
    assert parse_command("/nee").name == "new"
    assert parse_command("/nee extra args").name == "new"
    assert parse_command("/nee extra args").args == "extra args"
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
    assert "/model #" in help_text()
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


def test_new_text_echoes_model_and_context():
    text = new_text("glm-5.3-flash", "aspark", 131072)
    assert text.splitlines()[0] == "New conversation started."
    assert "Model: aspark/glm-5.3-flash." in text
    assert "Context: 131,072 tokens" in text

    default = new_text()
    assert "Model: gateway default." in default
    assert UNKNOWN_CONTEXT_HINT in default


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


def test_parse_model_index():
    assert parse_model_index("1") == 1
    assert parse_model_index(" 12 ") == 12
    assert parse_model_index("+3") == 3
    assert parse_model_index("-1") == -1
    assert parse_model_index("0") == 0
    assert parse_model_index("aspark/glm") is None
    assert parse_model_index("") is None


def test_models_menu_numbering_and_current_marker():
    menu = [
        ("aspark", "glm-5.3-flash"),
        ("aspark", "glm-5.3"),
        ("lspark", "deepseek-v4.1-flash"),
    ]
    text = models_menu_text(
        menu, current_model="glm-5.3", current_provider="aspark",
    )
    lines = text.splitlines()
    assert lines[0] == "Models (3):"
    assert lines[1] == "1. aspark/glm-5.3-flash"
    assert lines[2] == "2. aspark/glm-5.3 ← current"
    assert lines[3] == "3. lspark/deepseek-v4.1-flash"
    assert lines[-1] == MODEL_MENU_PROMPT


def test_models_menu_matches_by_model_when_provider_unpinned():
    text = models_menu_text(
        [("aspark", "glm-5.3-flash")],
        current_model="glm-5.3-flash",
        current_provider=None,
    )
    assert "1. aspark/glm-5.3-flash ← current" in text


def test_models_menu_empty():
    assert models_menu_text([]) == NO_MODELS_TEXT
    assert "gateway config" in NO_MODELS_TEXT


def test_model_index_hint_texts():
    assert MODEL_MENU_HINT == "Send /model first to list available models."
    assert MODEL_MENU_HINT in model_index_out_of_range_text(9)


def test_context_line_variants():
    assert context_line(None, 131072) == "Context: no turns yet"
    assert context_line(45230, None) == (
        "Context: 45,230 tokens (total unknown)"
    )
    assert context_line(45230, 131072) == (
        "Context: 45,230 / 131,072 tokens (35%)"
    )


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
        consumed_tokens=45230,
        context_total=131072,
        busy=True,
    )
    assert "Personality: atlas" in text
    assert "Model: openrouter/claude" in text
    assert "Session: 2m" in text
    assert "State: busy (turn in flight)" in text
    assert "Context: 45,230 / 131,072 tokens (35%)" in text
    assert "Queue: 2 waiting" in text
    assert "Relays: 3" in text
    assert "Watchdog: ok" in text


def test_status_text_idle_defaults():
    text = status_text(
        name="atlas",
        model=None,
        provider=None,
        session_age_seconds=None,
        queue_depth=0,
        relay_count=1,
        watchdog_state="ok",
    )
    assert "State: idle" in text
    assert "Context: no turns yet" in text
    assert "Model: gateway default" in text


def test_error_text_is_short_and_optional_detail():
    assert error_text().startswith("Something went wrong talking to Hermes.")
    assert "detail" in error_text("detail")
