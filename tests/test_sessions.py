from __future__ import annotations

import json
import os
import stat

from nostr_hermes_proxy.sessions import SessionMap


def make_clock(value: float):
    state = {"value": value}

    def clock() -> float:
        return state["value"]

    clock.advance = lambda seconds: state.__setitem__("value", state["value"] + seconds)
    return clock


def test_set_get_and_reload(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    sessions = SessionMap(path, clock=make_clock(100.0))
    sessions.set("atlas", "sess-1", model="m", provider="p")

    assert sessions.get("atlas").hermes_session_id == "sess-1"
    assert sessions.get("nope") is None

    reloaded = SessionMap(path)
    entry = reloaded.get("atlas")
    assert entry.hermes_session_id == "sess-1"
    assert entry.model == "m"
    assert entry.provider == "p"
    assert entry.created_at == 100.0


def test_write_is_atomic_and_private(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    sessions = SessionMap(path)
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def capture(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", capture)
    sessions.set("atlas", "sess-1")

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith(".tmp")
    assert dst == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_get_or_create_keeps_existing_and_preferences(tmp_path):
    sessions = SessionMap(tmp_path / "sessions.json", clock=make_clock(10.0))
    sessions.update_model("atlas", "m1", "p1")
    created = sessions.get_or_create("atlas", "sess-1")
    assert created.hermes_session_id == "sess-1"
    assert created.model == "m1"
    assert created.provider == "p1"

    same = sessions.get_or_create("atlas", "sess-2")
    assert same.hermes_session_id == "sess-1"


def test_set_preserves_created_at(tmp_path):
    clock = make_clock(100.0)
    sessions = SessionMap(tmp_path / "sessions.json", clock=clock)
    sessions.set("atlas", "sess-1")
    clock.advance(50)
    sessions.set("atlas", "sess-2")
    assert sessions.get("atlas").created_at == 100.0


def test_reset_removes_and_persists(tmp_path):
    path = tmp_path / "sessions.json"
    sessions = SessionMap(path)
    sessions.set("atlas", "sess-1")
    previous = sessions.reset("atlas")
    assert previous.hermes_session_id == "sess-1"
    assert sessions.get("atlas") is None
    assert SessionMap(path).get("atlas") is None


def test_new_session_after_reset_gets_new_age(tmp_path):
    clock = make_clock(100.0)
    sessions = SessionMap(tmp_path / "sessions.json", clock=clock)
    sessions.set("atlas", "sess-1")
    clock.advance(30)
    sessions.reset("atlas")
    sessions.set("atlas", "sess-2")
    assert sessions.get("atlas").created_at == 130.0


def test_update_model_without_session(tmp_path):
    sessions = SessionMap(tmp_path / "sessions.json", clock=make_clock(5.0))
    entry = sessions.update_model("atlas", "model-x", "provider-y")
    assert entry.hermes_session_id is None
    assert entry.model == "model-x"
    assert entry.created_at == 5.0


def test_seed_preferences_does_not_overwrite(tmp_path):
    sessions = SessionMap(tmp_path / "sessions.json")
    sessions.update_model("atlas", "chosen", "p")
    sessions.seed_preferences("atlas", "config-default", "default-provider")
    assert sessions.get("atlas").model == "chosen"
    sessions.seed_preferences("new", "config-default", None)
    assert sessions.get("new").model == "config-default"


def test_corrupt_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")
    sessions = SessionMap(path)
    assert len(sessions) == 0
    assert "unreadable" in caplog.text


def test_payload_shape(tmp_path):
    path = tmp_path / "sessions.json"
    sessions = SessionMap(path)
    sessions.set("atlas", "sess-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["sessions"]["atlas"]["hermes_session_id"] == "sess-1"


def test_usage_persists_and_survives_reload(tmp_path):
    path = tmp_path / "sessions.json"
    sessions = SessionMap(path, clock=make_clock(100.0))
    sessions.set("atlas", "sess-1", model="m", provider="p")
    sessions.update_usage("atlas", 45230)

    entry = sessions.get("atlas")
    assert entry.last_input_tokens == 45230
    assert entry.model == "m" and entry.provider == "p"

    reloaded = SessionMap(path).get("atlas")
    assert reloaded.last_input_tokens == 45230


def test_update_model_keeps_usage(tmp_path):
    sessions = SessionMap(tmp_path / "sessions.json")
    sessions.set("atlas", "sess-1", model="m1", provider="p1")
    sessions.update_usage("atlas", 1234)
    sessions.update_model("atlas", "m2", "p2")
    entry = sessions.get("atlas")
    assert (entry.model, entry.provider) == ("m2", "p2")
    assert entry.last_input_tokens == 1234


def test_new_session_resets_usage(tmp_path):
    sessions = SessionMap(tmp_path / "sessions.json")
    sessions.set("atlas", "sess-1")
    sessions.update_usage("atlas", 1234)
    sessions.reset("atlas")
    sessions.set("atlas", "sess-2")
    assert sessions.get("atlas").last_input_tokens is None


def test_old_state_without_usage_loads(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "atlas": {
                    "hermes_session_id": "sess-1",
                    "model": "m",
                    "provider": "p",
                    "created_at": 10.0,
                },
            },
        }),
        encoding="utf-8",
    )
    entry = SessionMap(path).get("atlas")
    assert entry.last_input_tokens is None
    assert entry.model == "m"


def test_malformed_usage_is_ignored(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "atlas": {
                    "hermes_session_id": "sess-1",
                    "last_input_tokens": "lots",
                },
            },
        }),
        encoding="utf-8",
    )
    assert SessionMap(path).get("atlas").last_input_tokens is None
