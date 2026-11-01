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
    assert config.hermes_config_path == "~/.hermes/config.yaml"
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


def test_hermes_config_path_custom(test_npub):
    text = config_text(
        test_npub, extra='hermes_config_path = "~/custom/hermes.yaml"\n',
    )
    config = parse_config(text, environ=NSEC_ENV)
    assert config.hermes_config_path == "~/custom/hermes.yaml"


def test_hermes_config_path_blank_rejected(test_npub):
    text = config_text(test_npub, extra='hermes_config_path = "  "\n')
    with pytest.raises(ConfigError, match="hermes_config_path"):
        parse_config(text, environ=NSEC_ENV)


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


# -- control plane -------------------------------------------------------------


def test_control_plane_defaults(test_npub):
    config = parse_config(config_text(test_npub), environ=NSEC_ENV)
    assert config.control_port == 8787
    assert config.control_token_path.endswith("control-token")
    assert config.outbound_rate_limit_per_minute == 10
    assert config.outbound_expiry_max_seconds == 7 * 24 * 60 * 60


def test_control_plane_knobs(test_npub):
    config = parse_config(
        config_text(
            test_npub,
            extra=(
                "control_port = 9999\n"
                'control_token_path = "~/custom-token"\n'
                "outbound_rate_limit_per_minute = 3\n"
                "outbound_expiry_max_seconds = 120\n"
            ),
        ),
        environ=NSEC_ENV,
    )
    assert config.control_port == 9999
    assert config.control_token_path == "~/custom-token"
    assert config.outbound_rate_limit_per_minute == 3
    assert config.outbound_expiry_max_seconds == 120


def test_control_port_zero_disables(test_npub):
    config = parse_config(
        config_text(test_npub, extra="control_port = 0\n"),
        environ=NSEC_ENV,
    )
    assert config.control_port == 0


@pytest.mark.parametrize(
    "extra, match",
    [
        ("control_port = 70000\n", "control_port"),
        ('control_port = "soon"\n', "control_port"),
        ("outbound_rate_limit_per_minute = 0\n", "outbound_rate_limit"),
        ('outbound_rate_limit_per_minute = "often"\n', "outbound_rate_limit"),
        ("outbound_expiry_max_seconds = 0\n", "outbound_expiry_max"),
        ('outbound_expiry_max_seconds = "long"\n', "outbound_expiry_max"),
        ('control_token_path = "  "\n', "control_token_path"),
    ],
)
def test_bad_control_knobs_rejected(test_npub, extra, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(config_text(test_npub, extra=extra), environ=NSEC_ENV)


# -- outbound spread -----------------------------------------------------------


def test_outbound_spread_defaults_to_one_hour(test_npub):
    config = parse_config(config_text(test_npub), environ=NSEC_ENV)
    assert config.outbound_spread_seconds == 3600


def test_outbound_spread_custom(test_npub):
    config = parse_config(
        config_text(test_npub, extra="outbound_spread_seconds = 120\n"),
        environ=NSEC_ENV,
    )
    assert config.outbound_spread_seconds == 120


@pytest.mark.parametrize("value", ["0", "-5", '"soon"', "true"])
def test_invalid_outbound_spread_rejected(test_npub, value):
    with pytest.raises(ConfigError, match="outbound_spread_seconds"):
        parse_config(
            config_text(
                test_npub, extra=f"outbound_spread_seconds = {value}\n",
            ),
            environ=NSEC_ENV,
        )


def test_personality_profile_parses(test_npub):
    text = config_text(
        test_npub,
        personality_extra=(
            '[personality.profile]\n'
            'about = "I help bitcoiners"\n'
            'lud16 = "bot@pay.example"\n'
            'nip05 = "bot@example"\n'
            'name = "Fancy Display Name"'
        ),
    )
    personality = parse_config(text, environ=NSEC_ENV).personalities[0]
    assert personality.profile == {
        "about": "I help bitcoiners",
        "lud16": "bot@pay.example",
        "nip05": "bot@example",
        "name": "Fancy Display Name",
    }


def test_personality_profile_defaults_to_empty(test_npub):
    personality = parse_config(
        config_text(test_npub), environ=NSEC_ENV,
    ).personalities[0]
    assert personality.profile == {}


def test_personality_profile_rejects_unknown_key(test_npub):
    text = config_text(
        test_npub,
        personality_extra='[personality.profile]\nlocation = "the moon"',
    )
    with pytest.raises(ConfigError, match="unknown profile key"):
        parse_config(text, environ=NSEC_ENV)


def test_personality_profile_rejects_empty_value(test_npub):
    text = config_text(
        test_npub,
        personality_extra='[personality.profile]\nabout = "  "',
    )
    with pytest.raises(ConfigError, match="profile.about"):
        parse_config(text, environ=NSEC_ENV)


def test_personality_profile_rejects_non_string(test_npub):
    text = config_text(
        test_npub,
        personality_extra="[personality.profile]\nlud16 = 42",
    )
    with pytest.raises(ConfigError, match="profile.lud16"):
        parse_config(text, environ=NSEC_ENV)


def test_personality_profile_rejects_non_table(test_npub):
    text = config_text(
        test_npub,
        personality_extra='profile = "not a table"',
    )
    with pytest.raises(ConfigError, match="profile must be a table"):
        parse_config(text, environ=NSEC_ENV)


def test_personality_profile_rejects_control_characters(test_npub):
    text = config_text(
        test_npub,
        personality_extra='[personality.profile]\nabout = "two\\nlines"',
    )
    with pytest.raises(ConfigError, match="single-line"):
        parse_config(text, environ=NSEC_ENV)
