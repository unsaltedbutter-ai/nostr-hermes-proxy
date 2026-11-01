"""Tests for the live gateway config.yaml reader (no caching)."""

from __future__ import annotations

import pytest

from nostr_hermes_proxy.gateway_config import (
    GatewayConfigError,
    read_gateway_models,
)

FULL_YAML = (
    "model:\n"
    "  provider: custom:aspark\n"
    "  default: glm-5.3-flash\n"
    "  context_length: 640000\n"
    "providers:\n"
    "  custom:\n"
    "    request_timeout_seconds: 18000\n"
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
    "        context_length: 1000\n"
)


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_menu_document_order_skips_disabled(tmp_path):
    models = read_gateway_models(_write(tmp_path, FULL_YAML))
    # "custom" has no models mapping; disabled providers are skipped.
    assert models.menu == [
        ("lspark", "deepseek-v4.1-flash"),
        ("aspark", "glm-5.3-flash"),
    ]


def test_context_exact_then_any_provider_then_default(tmp_path):
    models = read_gateway_models(_write(tmp_path, FULL_YAML))
    assert models.context_length("aspark", "glm-5.3-flash") == 640000
    assert models.context_length("lspark", "deepseek-v4.1-flash") == 655360
    # Exact provider misses, but another provider carries the model.
    assert models.context_length("holographic", "glm-5.3-flash") == 640000
    # The gateway default model falls back to the top-level context.
    assert models.context_length("elsewhere", "glm-5.3-flash") == 640000
    assert models.context_length("aspark", "unknown-model") is None
    assert models.context_length(None, None) is None


def test_top_level_default_appended_when_absent(tmp_path):
    models = read_gateway_models(_write(tmp_path, (
        "model:\n"
        "  provider: custom:aspark\n"
        "  default: glm-5.3-flash\n"
        "  context_length: 640000\n"
        "providers:\n"
        "  lspark:\n"
        "    models:\n"
        "      deepseek-v4.1-flash:\n"
        "        context_length: 655360\n"
    )))
    assert models.menu == [
        ("lspark", "deepseek-v4.1-flash"),
        ("aspark", "glm-5.3-flash"),
    ]


def test_no_caching_file_edits_take_effect_immediately(tmp_path):
    path = _write(tmp_path, FULL_YAML)
    first = read_gateway_models(path)
    assert models_has(first, "aspark", "glm-5.3-flash")
    path.write_text(
        FULL_YAML.replace(
            "      glm-5.3-flash:\n            context_length: 640000\n",
            "      glm-5.3-flash:\n            context_length: 999\n",
        ).replace(
            "        context_length: 640000\n",
            "        context_length: 999\n",
        ),
        encoding="utf-8",
    )
    second = read_gateway_models(path)
    assert second.context_length("aspark", "glm-5.3-flash") == 999
    assert not models_has(first, "newprov", "new-model")
    path.write_text(FULL_YAML + (
        "  newprov:\n"
        "    models:\n"
        "      new-model:\n"
        "        context_length: 1000\n"
    ), encoding="utf-8")
    third = read_gateway_models(path)
    assert models_has(third, "newprov", "new-model")


def models_has(models: object, provider: str, model: str) -> bool:
    return (provider, model) in models.menu


def test_missing_file_raises(tmp_path):
    with pytest.raises(GatewayConfigError, match="file not found"):
        read_gateway_models(tmp_path / "absent.yaml")


def test_invalid_yaml_raises(tmp_path):
    path = _write(tmp_path, "providers: [unclosed\n  - item\n")
    with pytest.raises(GatewayConfigError, match="invalid YAML"):
        read_gateway_models(path)


def test_non_mapping_raises(tmp_path):
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(GatewayConfigError, match="top-level mapping"):
        read_gateway_models(path)


def test_no_enabled_models_raises(tmp_path):
    path = _write(tmp_path, (
        "providers:\n"
        "  huggingface:\n"
        "    enabled: false\n"
        "    models:\n"
        "      hf-model:\n"
        "        context_length: 1000\n"
    ))
    with pytest.raises(GatewayConfigError, match="no enabled providers"):
        read_gateway_models(path)
