from __future__ import annotations

import pytest

from nostr_hermes_proxy.config import (
    ConfigError,
    load_config,
    npub_to_hex,
    parse_config,
)

NSEC_ENV = {"NSEC_ATLAS": "nsec1synthetictestkeynotreal"}


def config_text(npub: str, *, extra: str = "", personality_extra: str = "") -> str:
    return f'''
hermes_url = "http://127.0.0.1:8642"
allow_npub = "{npub}"
relays = ["wss://relay.damus.io", "wss://nos.lol"]
{extra}

[[personality]]
name = "atlas"
nsec = "${{NSEC_ATLAS}}"
{personality_extra}
'''


def test_valid_config_parses(test_npub, test_hex):
    config = parse_config(config_text(test_npub), environ=NSEC_ENV)
    assert config.hermes_url == "http://127.0.0.1:8642"
    assert config.allow_hex == test_hex
    assert config.allow_npub == test_npub.lower()
    assert config.ack_text == "👀"
    assert config.ack_ttl_seconds == 1200
    assert config.context_lengths == {}
    assert len(config.personalities) == 1
    atlas = config.personalities[0]
    assert atlas.name == "atlas"
    assert atlas.nsec == NSEC_ENV["NSEC_ATLAS"]
    assert atlas.relays == ("wss://nos.lol", "wss://relay.damus.io")
    assert config.state_dir.name == "nostr-hermes-proxy"


def test_hex_allow_npub_accepted(test_hex):
    config = parse_config(
        config_text(test_hex, extra=""), environ=NSEC_ENV,
    )
    assert config.allow_hex == test_hex
    assert config.allow_npub == test_hex


def test_npub_to_hex_rejects_bad_checksum():
    with pytest.raises(ConfigError):
        npub_to_hex("npub1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq")


def test_missing_env_is_reported(test_npub):
    with pytest.raises(ConfigError, match="NSEC_ATLAS"):
        parse_config(config_text(test_npub), environ={})


def test_literal_nsec_is_rejected(test_npub):
    text = config_text(
        test_npub, personality_extra='nsec = "nsec1notarealsecretvalue"',
    )
    # The template already has an nsec; make the literal the only one.
    text = text.replace('nsec = "${NSEC_ATLAS}"', "")
    with pytest.raises(ConfigError, match="literal nsec"):
        parse_config(text, environ=NSEC_ENV)


def test_bad_npub_is_rejected():
    with pytest.raises(ConfigError, match="npub"):
        parse_config(config_text("npub1nope"), environ=NSEC_ENV)


def test_no_personalities_is_rejected(test_npub):
    text = f'''
hermes_url = "http://127.0.0.1:8642"
allow_npub = "{test_npub}"
'''
    with pytest.raises(ConfigError, match="at least one"):
        parse_config(text, environ=NSEC_ENV)


def test_duplicate_personality_names_rejected(test_npub):
    text = config_text(test_npub) + f'''
[[personality]]
name = "atlas"
nsec = "${{NSEC_ATLAS}}"
relays = ["wss://nos.lol"]
'''
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(text, environ=NSEC_ENV)


def test_non_wss_relay_rejected(test_npub):
    text = config_text(
        test_npub,
        personality_extra='relays = ["ws://insecure.example"]',
    )
    with pytest.raises(ConfigError, match="wss://"):
        parse_config(text, environ=NSEC_ENV)


def test_personality_model_provider_split(test_npub):
    text = config_text(
        test_npub,
        personality_extra='model = "openrouter/anthropic/claude-sonnet"',
    )
    personality = parse_config(text, environ=NSEC_ENV).personalities[0]
    assert personality.provider == "openrouter"
    assert personality.model == "anthropic/claude-sonnet"


def test_explicit_provider_is_not_split(test_npub):
    text = config_text(
        test_npub,
        personality_extra=(
            'provider = "openrouter"\nmodel = "anthropic/claude-sonnet"'
        ),
    )
    personality = parse_config(text, environ=NSEC_ENV).personalities[0]
    assert personality.provider == "openrouter"
    assert personality.model == "anthropic/claude-sonnet"


def test_ack_and_watchdog_knobs(test_npub):
    text = config_text(
        test_npub,
        extra=(
            'ack_text = "✅"\n'
            "ack_ttl_seconds = 90\n"
            "watchdog_interval_seconds = 5\n"
            "watchdog_failure_threshold = 7\n"
            "watchdog_stall_seconds = 30\n"
        ),
    )
    config = parse_config(text, environ=NSEC_ENV)
    assert config.ack_text == "✅"
    assert config.ack_ttl_seconds == 90
    assert config.watchdog_interval_seconds == 5
    assert config.watchdog_failure_threshold == 7
    assert config.watchdog_stall_seconds == 30


def test_context_lengths_table(test_npub):
    text = config_text(
        test_npub,
        extra=(
            "[context_lengths]\n"
            '"glm-5.3-flash" = 131072\n'
            '"claude" = 200000\n'
        ),
    )
    config = parse_config(text, environ=NSEC_ENV)
    assert config.context_lengths == {
        "glm-5.3-flash": 131072,
        "claude": 200000,
    }


def test_invalid_context_lengths_rejected(test_npub):
    bad_value = config_text(
        test_npub, extra='[context_lengths]\n"m" = 0\n',
    )
    with pytest.raises(ConfigError, match="context_lengths"):
        parse_config(bad_value, environ=NSEC_ENV)

    bad_type = config_text(
        test_npub, extra='[context_lengths]\n"m" = "lots"\n',
    )
    with pytest.raises(ConfigError, match="context_lengths"):
        parse_config(bad_type, environ=NSEC_ENV)

    bad_table = config_text(test_npub, extra='context_lengths = 5\n')
    with pytest.raises(ConfigError, match="context_lengths"):
        parse_config(bad_table, environ=NSEC_ENV)


def test_bad_ack_knobs_rejected(test_npub):
    with pytest.raises(ConfigError, match="ack_ttl_seconds"):
        parse_config(
            config_text(test_npub, extra="ack_ttl_seconds = 59"),
            environ=NSEC_ENV,
        )
    with pytest.raises(ConfigError, match="ack_ttl_seconds"):
        parse_config(
            config_text(test_npub, extra='ack_ttl_seconds = "soon"'),
            environ=NSEC_ENV,
        )
    with pytest.raises(ConfigError, match="ack_text"):
        parse_config(
            config_text(test_npub, extra='ack_text = ""'),
            environ=NSEC_ENV,
        )


def test_load_config_reads_file(tmp_path, test_npub):
    path = tmp_path / "config.toml"
    path.write_text(config_text(test_npub), encoding="utf-8")
    config = load_config(path, environ=NSEC_ENV)
    assert config.source == path
    assert config.personalities[0].name == "atlas"


def test_missing_personality_relays_rejected(test_npub):
    text = f'''
hermes_url = "http://127.0.0.1:8642"
allow_npub = "{test_npub}"

[[personality]]
name = "atlas"
nsec = "${{NSEC_ATLAS}}"
'''
    with pytest.raises(ConfigError, match="relay"):
        parse_config(text, environ=NSEC_ENV)


def test_example_config_has_placeholders_and_no_secret_literal():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    assert "${NSEC_ATLAS}" in text
    assert "nsec1" not in text.lower()
    assert "npub1REPLACE" in text


def test_invalid_numeric_knobs_rejected(test_npub):
    with pytest.raises(ConfigError, match="watchdog_interval_seconds"):
        parse_config(
            config_text(
                test_npub, extra='watchdog_interval_seconds = "soon"',
            ),
            environ=NSEC_ENV,
        )
    with pytest.raises(ConfigError, match="watchdog_stall_seconds"):
        parse_config(
            config_text(
                test_npub, extra='watchdog_stall_seconds = "later"',
            ),
            environ=NSEC_ENV,
        )
