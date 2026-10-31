#!/usr/bin/env python
"""Follow-up for Experiment A:
1. Was the marker event actually stored on nos.lol? (fetch_events by id)
2. Does a subscription WITHOUT .limit(0) receive live events? (publish second marker)
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
LOG = open("/var/tmp/opencode/nostr-probe/expA2.log", "w", buffering=1)


def log(msg):
    line = f"{time.time():.3f} {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")


class Recorder(HandleNotification):
    def __init__(self, label, sink):
        super().__init__()
        self.label = label
        self.sink = sink

    async def handle(self, relay_url, subscription_id, event):
        eid = event.id().to_hex()
        created = event.created_at().as_secs()
        content = event.content()[:80].replace("\n", "\\n")
        self.sink.append({"id": eid, "created": created, "content": content})
        log(f"[{self.label}] EVENT sub={subscription_id} id={eid[:16]} created_at={created} content={content!r}")

    async def handle_msg(self, relay_url, msg):
        log(f"[{self.label}] MSG {str(msg)[:120]}")


async def main():
    keys = Keys.generate()
    pk = keys.public_key()
    marker2 = f"ndk-probe-b-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    log(f"pubkey={pk.to_hex()} marker2={marker2!r}")

    signer = NostrSigner.keys(keys)
    client = ClientBuilder().signer(signer).build()
    await client.add_relay(RelayUrl.parse(RELAY))
    await client.connect()
    await client.wait_for_connection(timedelta(seconds=10))
    log("connected")

    # Step 1: is marker1 retrievable? We don't know its id here (fresh run), so instead:
    # subscribe live WITHOUT limit, publish marker2, then fetch by id.
    filt = Filter().author(pk).kind(Kind.from_std(KindStandard.TEXT_NOTE))
    log(f"filter(no limit) json: {filt.as_json()}")
    sink = []
    out = await client.subscribe(filt)
    log(f"subscribed -> {out}")
    htask = asyncio.create_task(client.handle_notifications(Recorder("nolimit", sink)))
    await asyncio.sleep(3)

    log(f"publishing marker2: {marker2!r}")
    out = await client.send_event_builder(EventBuilder.text_note(marker2))
    try:
        mid = out.id().to_hex()
    except Exception:
        mid = str(out)
    log(f"published marker2 id={mid}")
    for _ in range(20):
        if any(e["content"] == marker2 for e in sink):
            break
        await asyncio.sleep(0.5)
    hits = [e for e in sink if e["content"] == marker2]
    log(f"[VERDICT nolimit] live arrival of marker2: {bool(hits)} (total events on sub: {len(sink)})")
    if hits:
        mid = hits[0]["id"]
        log(f"marker2 delivered id={mid} created_at={hits[0]['created']}")

    # fetch by id to check storage
    try:
        evs = await client.fetch_events(Filter().id(mid).kind(Kind.from_std(KindStandard.TEXT_NOTE)),
                                        timedelta(seconds=10))
        log(f"[VERDICT storage] fetch_events by id returned {len(evs)} event(s)")
        for e in evs:
            log(f"  stored: id={e.id().to_hex()[:16]} created_at={e.created_at().as_secs()} content={e.content()[:60]!r}")
    except Exception as e:
        log(f"fetch_events failed: {e!r}")

    await htask.cancel() if False else htask.cancel()
    await client.disconnect()
    await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
