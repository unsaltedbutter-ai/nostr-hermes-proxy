"""Loopback control plane: auth, validation, idempotency, health."""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from nostr_hermes_proxy.config import Config
from nostr_hermes_proxy.control import create_app
from nostr_hermes_proxy.outbox import Outbox

from .fakes import make_harness

TOKEN = "test-control-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _setup(tmp_path, allow_hex, *, rate=10, expiry_max=604800):
    harness = make_harness(allow_hex=allow_hex, tmp_path=tmp_path)
    config = Config(
        hermes_url="http://127.0.0.1:8642",
        allow_npub=allow_hex,
        allow_hex=allow_hex,
        ack_text="👀",
        ack_ttl_seconds=1200,
        personalities=(harness.config,),
        state_dir=tmp_path,
        outbound_rate_limit_per_minute=rate,
        outbound_expiry_max_seconds=expiry_max,
    )
    outbox = Outbox(None, rate_limit_per_minute=rate)
    app = create_app(
        config=config,
        outbox=outbox,
        runtimes={"atlas": harness.runtime},
        token=TOKEN,
    )
    return app, outbox, harness


async def test_post_dm_accepts_and_queues(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm", json={"text": "Morning digest"}, headers=AUTH,
        )
        assert response.status == 202
        body = await response.json()
        assert body["accepted"] is True
        assert body["duplicate"] is False
        assert body["chunks"] == 1
        assert len(body["message_id"]) == 32

    queued = outbox.queued("atlas")
    assert len(queued) == 1
    assert queued[0].text == "Morning digest"
    # The expiry defaults to the configured maximum.
    assert queued[0].expiry_seconds == 604800


async def test_idempotency_key_returns_stored_response_and_does_not_reenqueue(
    tmp_path, test_hex,
):
    app, outbox, _ = _setup(tmp_path, test_hex)
    payload = {"text": "once", "idempotency_key": "cron-2026-11-01"}
    async with TestClient(TestServer(app)) as client:
        first = await client.post("/v1/dm", json=payload, headers=AUTH)
        second = await client.post("/v1/dm", json=payload, headers=AUTH)
        assert first.status == 202 and second.status == 202
        first_body = await first.json()
        second_body = await second.json()

    assert second_body["duplicate"] is True
    assert second_body["message_id"] == first_body["message_id"]
    assert outbox.pending_count() == 1


async def test_empty_and_oversized_text_are_rejected(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        empty = await client.post("/v1/dm", json={"text": ""}, headers=AUTH)
        spaces = await client.post("/v1/dm", json={"text": "   "}, headers=AUTH)
        too_big = await client.post(
            "/v1/dm", json={"text": "x" * (64 * 1024 + 1)}, headers=AUTH,
        )
        missing = await client.post("/v1/dm", json={}, headers=AUTH)
        assert empty.status == 400
        assert spaces.status == 400
        assert too_big.status == 400
        assert missing.status == 400
    assert outbox.pending_count() == 0


async def test_bad_token_is_401(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        missing = await client.post("/v1/dm", json={"text": "hi"})
        wrong = await client.post(
            "/v1/dm",
            json={"text": "hi"},
            headers={"Authorization": "Bearer nope"},
        )
        assert missing.status == 401
        assert wrong.status == 401
    assert outbox.pending_count() == 0


async def test_unknown_personality_is_403(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm",
            json={"text": "hi", "personality": "nobody"},
            headers=AUTH,
        )
        assert response.status == 403
    assert outbox.pending_count() == 0


async def test_rate_cap_is_429(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex, rate=2)
    async with TestClient(TestServer(app)) as client:
        statuses = []
        for index in range(3):
            response = await client.post(
                "/v1/dm", json={"text": f"message {index}"}, headers=AUTH,
            )
            statuses.append(response.status)
        assert statuses == [202, 202, 429]
    assert outbox.pending_count() == 2


async def test_unhealthy_bot_still_queues_and_reports_retry(tmp_path, test_hex):
    app, outbox, harness = _setup(tmp_path, test_hex)
    harness.bot.health_value = False
    payload = {"text": "nudge", "idempotency_key": "k"}
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/dm", json=payload, headers=AUTH)
        assert response.status == 503
        body = await response.json()
        assert body["queued_for_retry"] is True
        assert body["accepted"] is True
        assert outbox.pending_count() == 1

        duplicate = await client.post("/v1/dm", json=payload, headers=AUTH)
        assert duplicate.status == 202
        duplicate_body = await duplicate.json()
        assert duplicate_body["duplicate"] is True
        assert duplicate_body["queued_for_retry"] is True
    assert outbox.pending_count() == 1


async def test_health_endpoint_reports_each_personality(tmp_path, test_hex):
    app, _outbox, harness = _setup(tmp_path, test_hex)
    harness.runtime.set_watchdog_status_provider(lambda: "ok")
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/health", headers=AUTH)
        assert response.status == 200
        body = await response.json()
        assert body == [{
            "name": "atlas",
            "healthy": True,
            "relay_count": 2,
            "watchdog": "ok",
        }]

        harness.bot.health_value = False
        response = await client.get("/v1/health", headers=AUTH)
        body = await response.json()
        assert body[0]["healthy"] is False


async def test_health_requires_auth(tmp_path, test_hex):
    app, _outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/health")
        assert response.status == 401


async def test_invalid_expiry_is_400(tmp_path, test_hex):
    app, _outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm",
            json={"text": "hi", "expiry_seconds": "soon"},
            headers=AUTH,
        )
        assert response.status == 400


def test_ensure_control_token_creates_0600_and_reuses(tmp_path):
    import stat

    from nostr_hermes_proxy.control import ensure_control_token

    token_path = tmp_path / "config" / "control-token"
    token, created = ensure_control_token(token_path)
    assert created is True
    assert len(token) == 64
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700

    again, created_again = ensure_control_token(token_path)
    assert created_again is False
    assert again == token


async def test_recipient_is_never_caller_supplied(tmp_path, test_hex):
    app, outbox, harness = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm",
            json={"text": "hello", "recipient": "ab" * 32},
            headers=AUTH,
        )
        assert response.status == 202

    async def deliver(personality, text, expiry):
        runtime = harness.runtime
        await runtime.deliver_dm(runtime.allow_hex, text, expiry)

    await outbox.drain_once(deliver)
    sent = harness.bot.sent_events[0]
    p_tags = [
        tag.to_vec()[1]
        for tag in sent.event.tags()
        if tag.to_vec() and tag.to_vec()[0] == "p"
    ]
    assert test_hex in p_tags
    assert "ab" * 32 not in p_tags
