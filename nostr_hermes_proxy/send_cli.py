"""``nostr-send`` — thin CLI over the proxy's loopback control plane.

Reads the bearer token from disk, POSTs the DM, prints the JSON response, and
exits 0 on 2xx / 1 on an HTTP error status / 2 when the daemon cannot be
reached (or the request could not be built).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import httpx

from .config import DEFAULT_CONTROL_PORT, DEFAULT_CONTROL_TOKEN_PATH
from .control import ControlTokenError, read_control_token

DEFAULT_TIMEOUT_SECONDS = 10.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nostr-send",
        description="Send an outbound DM through the local nostr-hermes-proxy.",
    )
    parser.add_argument(
        "text", nargs="?", help="message text (omit with --file)",
    )
    parser.add_argument(
        "--personality", default=None,
        help="personality name (default: the only configured one)",
    )
    parser.add_argument(
        "--expiry", type=int, default=None,
        help="NIP-40 expiry in seconds (clamped by the daemon)",
    )
    parser.add_argument(
        "--idempotency-key", dest="idempotency_key", default=None,
        help="24-hour idempotency key (safe cron retries)",
    )
    parser.add_argument(
        "--file", default=None,
        help="read message text from this file instead of argv",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_CONTROL_PORT,
        help=f"control-plane port (default: {DEFAULT_CONTROL_PORT})",
    )
    parser.add_argument(
        "--token-path", default=DEFAULT_CONTROL_TOKEN_PATH,
        help=f"bearer token file (default: {DEFAULT_CONTROL_TOKEN_PATH})",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser


def build_payload(
    text: str,
    *,
    personality: str | None = None,
    expiry: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text}
    if personality:
        payload["personality"] = personality
    if expiry is not None:
        payload["expiry_seconds"] = expiry
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return payload


def post_dm(
    payload: dict[str, Any],
    *,
    port: int = DEFAULT_CONTROL_PORT,
    token_path: str = DEFAULT_CONTROL_TOKEN_PATH,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    token = read_control_token(token_path)
    url = f"http://127.0.0.1:{port}/v1/dm"
    with httpx.Client(timeout=timeout, transport=transport) as client:
        return client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )


def _print_response(response: httpx.Response) -> None:
    try:
        body: Any = response.json()
        print(json.dumps(body, sort_keys=True))
    except ValueError:
        print(response.text)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.file and args.text is not None:
        parser.error("provide either TEXT or --file, not both")
    if not args.file and args.text is None:
        parser.error("TEXT or --file is required")

    if args.file:
        try:
            text = Path(args.file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            print(f"nostr-send: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        text = args.text

    payload = build_payload(
        text,
        personality=args.personality,
        expiry=args.expiry,
        idempotency_key=args.idempotency_key,
    )
    try:
        response = post_dm(
            payload,
            port=args.port,
            token_path=args.token_path,
            timeout=args.timeout,
            transport=transport,
        )
    except ControlTokenError as exc:
        print(f"nostr-send: {exc}", file=sys.stderr)
        return 2
    except httpx.TimeoutException:
        print(
            f"nostr-send: timed out talking to 127.0.0.1:{args.port} "
            "(is the proxy running?)",
            file=sys.stderr,
        )
        return 2
    except httpx.TransportError as exc:
        print(
            f"nostr-send: cannot reach 127.0.0.1:{args.port}: {exc}",
            file=sys.stderr,
        )
        return 2

    _print_response(response)
    return 0 if 200 <= response.status_code < 300 else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
