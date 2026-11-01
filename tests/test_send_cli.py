"""nostr-send CLI: payload building, file input, and exit codes."""

from __future__ import annotations

import json

import httpx
import pytest

from nostr_hermes_proxy.send_cli import main


def _token_file(tmp_path, token="secret-token") -> str:
    path = tmp_path / "control-token"
    path.write_text(token + "\n", encoding="utf-8")
    return str(path)


def _transport(status=202, body=None, error=None):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = json.loads(request.content)
        if error is not None:
            raise error
        return httpx.Response(status, json=body or {})

    return httpx.MockTransport(handler), seen


def test_cli_posts_text_and_prints_json(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    transport, seen = _transport(
        body={"accepted": True, "message_id": "abc", "chunks": 1,
              "duplicate": False},
    )
    code = main(
        ["--token-path", token_path, "--port", "9999", "hello"],
        transport=transport,
    )
    assert code == 0
    assert seen["url"] == "http://127.0.0.1:9999/v1/dm"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["json"] == {"text": "hello"}
    assert json.loads(capsys.readouterr().out)["message_id"] == "abc"


def test_cli_reads_file_and_forwards_options(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    text_file = tmp_path / "digest.txt"
    text_file.write_text("line one\nline two\n", encoding="utf-8")
    transport, seen = _transport()
    code = main(
        [
            "--file", str(text_file),
            "--personality", "atlas",
            "--expiry", "90",
            "--idempotency-key", "daily-1",
            "--token-path", token_path,
        ],
        transport=transport,
    )
    assert code == 0
    assert seen["json"] == {
        "text": "line one\nline two\n",
        "personality": "atlas",
        "expiry_seconds": 90,
        "idempotency_key": "daily-1",
    }
    capsys.readouterr()


def test_cli_http_error_exits_1_and_prints_body(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    transport, _ = _transport(status=403, body={"error": "unknown personality"})
    code = main(["--token-path", token_path, "hi"], transport=transport)
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unknown personality"


def test_cli_connection_error_exits_2(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    transport, _ = _transport(error=httpx.ConnectError("connection refused"))
    code = main(["--token-path", token_path, "hi"], transport=transport)
    assert code == 2
    assert "cannot reach 127.0.0.1" in capsys.readouterr().err


def test_cli_timeout_exits_2(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    transport, _ = _transport(error=httpx.ReadTimeout("too slow"))
    code = main(["--token-path", token_path, "hi"], transport=transport)
    assert code == 2
    assert "timed out" in capsys.readouterr().err


def test_cli_missing_token_file_exits_2(tmp_path, capsys):
    transport, _ = _transport()
    code = main(
        ["--token-path", str(tmp_path / "missing"), "hi"],
        transport=transport,
    )
    assert code == 2
    assert "control token" in capsys.readouterr().err


def test_cli_requires_exactly_one_text_source(tmp_path):
    token_path = _token_file(tmp_path)
    with pytest.raises(SystemExit) as no_text:
        main(["--token-path", token_path])
    assert no_text.value.code == 2

    text_file = tmp_path / "digest.txt"
    text_file.write_text("from file", encoding="utf-8")
    with pytest.raises(SystemExit) as both:
        main(["--file", str(text_file), "from argv"])
    assert both.value.code == 2


def test_cli_unreadable_file_exits_2(tmp_path, capsys):
    token_path = _token_file(tmp_path)
    transport, _ = _transport()
    code = main(
        ["--token-path", token_path, "--file", str(tmp_path / "nope.txt")],
        transport=transport,
    )
    assert code == 2
    assert "cannot read" in capsys.readouterr().err
