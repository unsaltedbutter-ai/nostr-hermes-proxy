#!/usr/bin/env python
"""Experiment A (v2, fixed): does .limit(0) (no since) replay history on fresh subscribe/reconnect?

Fix vs v1: one long-lived handler task per client for the whole client lifetime; events are
extracted from RelayMessage.handle_msg (the `handle` callback is never invoked by these bindings).

Phases:
 1. c1: subscribe author=self kind=1 limit=0 -> 5s baseline
 2. c1: publish marker note, watch live delivery on same subscription
 3. c1: full disconnect, wait 2s
 4. c2: fresh client, same filter (.limit(0), no since), 15s -> marker replay?
 5. c3: second reconnect, 15s
 6. c4: control, filter + .since(now-30s), 15s -> marker replay?
"""
import asyncio
import time
import uuid
from datetime import timedelta

from nostr_sdk import (
    Client, ClientBuilder, Filter, Kind, KindStandard, Keys, NostrSigner,
    HandleNotification, Timestamp, EventBuilder, RelayUrl,
)

RELAY = "wss://nos.lol"
LOG = open("/var/tmp/opencode/nostr-probe/expA.log", "w", buffering=1)


def log(msg):
    line = f"{time.time():.3f} {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")


class Recorder(HandleNotification):
    def __init__(self, label, sink):
        super().__init__()
        self.label = label
        self.sink = sink

    async def handle(self, relay_url, subscription_id, event):  # never invoked in this binding, kept for safety
        self._record("handle", subscription_id, event)

    def _record(self, via, sub_id, event):
        eid = event.id().to_hex()
        created = event.created_at().as_secs()
        content = event.content()[:70].replace("\n", "\\n")
        self.sink.append({"id": eid, "created": created, "content": content})
        log(f"[{self.label}] EVENT({via}) sub={str(sub_id)[:12]} id={eid} created_at={created} content={content!r}")

    async def handle_msg(self, relay_url, msg):
        m = msg.as_enum()
        tn = type(m).__name__
        if tn.endswith("EVENT_MSG"):
            self._record("msg", m.subscription_id, m.event)
        elif "END_OF_STORED_EVENTS" in tn or "EOSE" in tn:
            sub = getattr(m, "subscription_id", None)
            log(f"[{self.label}] EOSE sub={str(sub)[:12]} t={time.time():.3f}")
        elif "NOTICE" in tn:
            log(f"[{self.label}] NOTICE {getattr(m, 'message', m)!r}")
        elif "CLOSED" in tn:
            log(f"[{self.label}] CLOSED sub={getattr(m, 'subscription_id', None)} msg={getattr(m, 'message', '')!r}")
        elif "OK" in tn:
            log(f"[{self.label}] OK id={m.event_id.to_hex()[:16]} status={m.status} msg={m.message!r}")
        # COUNT / AUTH ignored


async def new_client(keys, label):
    client = ClientBuilder().signer(NostrSigner.keys(keys)).build()
    await client.add_relay(RelayUrl.parse(RELAY))
    await client.connect()
    await client.wait_for_connection(timedelta(seconds=10))
    log(f"[{label}] connected")
    sink = []
    handler = asyncio.create_task(client.handle_notifications(Recorder(label, sink)))
    await asyncio.sleep(0.2)
    return client, sink, handler


def make_filter(keys, with_since_secs=None):
    f = Filter().author(keys.public_key()).kind(Kind.from_std(KindStandard.TEXT_NOTE)).limit(0)
    if with_since_secs is not None:
        f = f.since(Timestamp.now().sub_duration(timedelta(seconds=with_since_secs)))
    return f


async def observe(client, label, filt, sink, seconds):
    out = await client.subscribe(filt)
    log(f"[{label}] filter={filt.as_json()}")
    log(f"[{label}] subscribe -> {out}")
    before = len(sink)
    await asyncio.sleep(seconds)
    return sink[before:]


async def teardown(client, label, handler):
    handler.cancel()
    await client.disconnect()
    await client.shutdown()
    log(f"[{label}] disconnected + shutdown")


async def main():
    t0 = int(time.time())
    marker = f"ndk-probe-{t0}-{uuid.uuid4().hex[:8]}"
    keys = Keys.generate()
    log(f"=== Experiment A v2 start; pubkey={keys.public_key().to_hex()}; marker={marker!r}")

    # ---- Phase 1: baseline on fresh client
    c1, sink1, h1 = await new_client(keys, "c1")
    filt0 = make_filter(keys)
    new_events = await observe(c1, "c1-baseline", filt0, sink1, 5)
    log(f"[VERDICT baseline] fresh-subscribe served {len(new_events)} event(s) in 5s (fresh key: expect 0)")

    # ---- Phase 2: publish marker, watch live delivery
    log(f"[c1] publishing marker: {marker!r}")
    pub_out = await c1.send_event_builder(EventBuilder.text_note(marker))
    marker_id = pub_out.id.to_hex()
    log(f"[c1] publish ok, marker_id={marker_id}")
    t_pub = time.time()
    for _ in range(30):
        if any(e["id"] == marker_id for e in sink1):
            break
        await asyncio.sleep(0.5)
    live = [e for e in sink1 if e["id"] == marker_id]
    log(f"[VERDICT live] marker arrived on live .limit(0) sub: {bool(live)} "
        f"latency={time.time()-t_pub:.1f}s total_seen={len(sink1)}")

    # ---- Phase 3: full disconnect
    await teardown(c1, "c1", h1)
    await asyncio.sleep(2)

    # ---- Phase 4: reconnect #1
    c2, sink2, h2 = await new_client(keys, "c2")
    got = await observe(c2, "c2-reconnect1", make_filter(keys), sink2, 15)
    replay = [e for e in got if e["id"] == marker_id]
    log(f"[VERDICT reconnect1] served {len(got)} event(s); marker_replayed={len(replay)}")
    await teardown(c2, "c2", h2)
    await asyncio.sleep(2)

    # ---- Phase 5: reconnect #2
    c3, sink3, h3 = await new_client(keys, "c3")
    got = await observe(c3, "c3-reconnect2", make_filter(keys), sink3, 15)
    replay = [e for e in got if e["id"] == marker_id]
    log(f"[VERDICT reconnect2] served {len(got)} event(s); marker_replayed={len(replay)}")
    await teardown(c3, "c3", h3)
    await asyncio.sleep(2)

    # ---- Phase 6: control with .since(now-30s)
    c4, sink4, h4 = await new_client(keys, "c4")
    got = await observe(c4, "c4-since-control", make_filter(keys, 30), sink4, 15)
    replay = [e for e in got if e["id"] == marker_id]
    log(f"[VERDICT since-control] served {len(got)} event(s); marker_replayed={len(replay)}")
    await teardown(c4, "c4", h4)

    log(f"=== Experiment A v2 done. marker={marker!r} id={marker_id}")


if __name__ == "__main__":
    asyncio.run(main())
