"""Loopback control plane: auth, validation, idempotency, health."""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from nostr_hermes_proxy.chunking import truncate_message
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


def _setup_multi(
    tmp_path,
    allow_hex,
    *,
    names=("atlas", "notible"),
    default_personality=None,
):
    harnesses = [
        make_harness(name=name, allow_hex=allow_hex, tmp_path=tmp_path)
        for name in names
    ]
    config = Config(
        hermes_url="http://127.0.0.1:8642",
        allow_npub=allow_hex,
        allow_hex=allow_hex,
        ack_text="👀",
        ack_ttl_seconds=1200,
        personalities=tuple(h.config for h in harnesses),
        state_dir=tmp_path,
        outbound_rate_limit_per_minute=10,
        outbound_expiry_max_seconds=604800,
        default_personality=default_personality,
    )
    outbox = Outbox(None, rate_limit_per_minute=10)
    app = create_app(
        config=config,
        outbox=outbox,
        runtimes={h.config.name: h.runtime for h in harnesses},
        token=TOKEN,
    )
    return app, outbox, harnesses


async def test_post_dm_without_personality_uses_default(tmp_path, test_hex):
    app, outbox, _ = _setup_multi(
        tmp_path, test_hex, default_personality="notible",
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm", json={"text": "hi"}, headers=AUTH,
        )
    assert response.status == 202
    assert len(outbox.queued("notible")) == 1
    assert outbox.queued("atlas") == []


async def test_post_dm_without_personality_without_default_rejected(
    tmp_path, test_hex,
):
    app, outbox, _ = _setup_multi(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm", json={"text": "hi"}, headers=AUTH,
        )
    assert response.status == 400
    assert outbox.queued() == []


async def test_post_dm_explicit_personality_overrides_default(
    tmp_path, test_hex,
):
    app, outbox, _ = _setup_multi(
        tmp_path, test_hex, default_personality="notible",
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dm",
            json={"text": "hi", "personality": "atlas"},
            headers=AUTH,
        )
    assert response.status == 202
    assert len(outbox.queued("atlas")) == 1
    assert outbox.queued("notible") == []


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


# -- GET /v1/outbox/status -----------------------------------------------------


async def test_outbox_status_requires_auth(tmp_path, test_hex):
    app, _outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        missing = await client.get("/v1/outbox/status")
        wrong = await client.get(
            "/v1/outbox/status",
            headers={"Authorization": "Bearer nope"},
        )
        assert missing.status == 401
        assert wrong.status == 401
        assert (await missing.json())["error"] == "unauthorized"


async def test_outbox_status_empty_outbox(tmp_path, test_hex):
    app, _outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/outbox/status", headers=AUTH)
        assert response.status == 200
        assert await response.json() == {
            "pending_total": 0,
            "personalities": [],
            "rate_limit_per_minute": 10,
        }


async def test_outbox_status_reports_queued_message(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    text = "First line\nsecond line\n" + "y" * 5000
    expected_preview = ("First line second line " + "y" * 5000)[:80]
    async with TestClient(TestServer(app)) as client:
        posted = await client.post(
            "/v1/dm", json={"text": text}, headers=AUTH,
        )
        assert posted.status == 202
        posted_body = await posted.json()

        response = await client.get("/v1/outbox/status", headers=AUTH)
        assert response.status == 200
        body = await response.json()

    assert body["pending_total"] == 1
    assert body["rate_limit_per_minute"] == 10
    [entry] = body["personalities"]
    assert entry["name"] == "atlas"
    assert entry["pending"] == 1
    [message] = entry["messages"]
    assert message["message_id"] == posted_body["message_id"]
    assert isinstance(message["created_at"], float)
    assert message["created_at"] > 0
    assert message["attempts"] == 0
    assert message["next_attempt_at"] == 0.0
    assert message["chunks"] == posted_body["chunks"] > 1
    assert message["chunks"] == len(truncate_message(text))
    assert message["preview"] == expected_preview
    assert len(message["preview"]) == 80
    assert "\n" not in message["preview"]


async def test_outbox_status_sorts_personalities_and_messages(
    tmp_path, test_hex,
):
    app, _outbox, _ = _setup_multi(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        ids = []
        for payload in (
            {"text": "atlas one", "personality": "atlas"},
            {"text": "atlas two", "personality": "atlas"},
            {"text": "notible one", "personality": "notible"},
        ):
            posted = await client.post("/v1/dm", json=payload, headers=AUTH)
            assert posted.status == 202
            ids.append((await posted.json())["message_id"])

        response = await client.get("/v1/outbox/status", headers=AUTH)
        body = await response.json()

    assert body["pending_total"] == 3
    assert [entry["name"] for entry in body["personalities"]] == [
        "atlas", "notible",
    ]
    assert [
        message["message_id"] for message in body["personalities"][0]["messages"]
    ] == ids[:2]
    assert body["personalities"][1]["pending"] == 1


async def test_outbox_status_shows_retry_backoff(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    async with TestClient(TestServer(app)) as client:
        posted = await client.post(
            "/v1/dm", json={"text": "relay down"}, headers=AUTH,
        )
        assert posted.status == 202

    async def deliver(personality, text, expiry):
        raise RuntimeError("relay unreachable")

    stats = await outbox.drain_once(deliver)
    assert stats["retried"] == 1

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/outbox/status", headers=AUTH)
        body = await response.json()

    [message] = body["personalities"][0]["messages"]
    assert message["attempts"] == 1
    assert message["next_attempt_at"] > message["created_at"]
    assert message["preview"] == "relay down"
    assert body["pending_total"] == 1


async def test_outbox_status_is_read_only(tmp_path, test_hex):
    app, outbox, _ = _setup(tmp_path, test_hex)
    payload = {"text": "once", "idempotency_key": "cron-2026-11-01"}
    async with TestClient(TestServer(app)) as client:
        posted = await client.post("/v1/dm", json=payload, headers=AUTH)
        assert posted.status == 202
        message_id = (await posted.json())["message_id"]

        before = [message.to_json() for message in outbox.queued()]
        first = await client.get("/v1/outbox/status", headers=AUTH)
        second = await client.get("/v1/outbox/status", headers=AUTH)
        duplicate = await client.post("/v1/dm", json=payload, headers=AUTH)
        after = [message.to_json() for message in outbox.queued()]

        assert first.status == second.status == 200
        # Identical snapshots: reading the status did not drain or mutate.
        assert await first.json() == await second.json()
        assert duplicate.status == 202
        duplicate_body = await duplicate.json()
        assert duplicate_body["duplicate"] is True
        assert duplicate_body["message_id"] == message_id

    assert outbox.pending_count() == 1
    assert before == after
    assert after[0]["message_id"] == message_id
