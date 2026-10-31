#!/usr/bin/env python
"""Controls to interpret Experiment B's zero counts:
1. fetch kind-1 marker from expA by id -> relay storage works on this relay?
2. fetch expA author kind-1 WITHOUT limit -> stored event served on plain REQ?
3. fetch bot kind-1059 WITH a since but NO limit -> do stored gift wraps exist for the bot?
"""
import asyncio
import time
from datetime import timedelta

from nostr_sdk import (
    Client, ClientBuilder, Filter, Kind, KindStandard, PublicKey, RelayUrl, Timestamp, EventId,
)

RELAY = "wss://nos.lol"
LOG = open("/var/tmp/opencode/nostr-probe/expB2.log", "w", buffering=1)


def log(msg):
    print(f"{time.time():.3f} {msg}", flush=True)
    LOG.write(f"{time.time():.3f} {msg}\n")


async def fetch(client, label, filt, timeout_s=15):
    try:
        evs = await client.fetch_events(filt, timedelta(seconds=timeout_s))
        evs = evs.to_vec()
        log(f"[{label}] filter={filt.as_json()} -> {len(evs)} event(s)")
        created = [e.created_at().as_secs() for e in evs]
        if created:
            log(f"[{label}] oldest={min(created)} newest={max(created)} span_days={(max(created)-min(created))/86400:.2f} now={int(time.time())}")
            for e in list(evs)[:3]:
                log(f"[{label}]   sample id={e.id().to_hex()[:16]} kind={e.kind()} created_at={e.created_at().as_secs()}")
        return len(evs)
    except Exception as ex:
        log(f"[{label}] fetch failed: {ex!r}")
        return None


async def main():
    client = ClientBuilder().build()
    await client.add_relay(RelayUrl.parse(RELAY))
    await client.connect()
    await client.wait_for_connection(timedelta(seconds=10))
    log("connected")

    # 1. marker from expA by id
    marker_id = EventId.parse("cce41875803a8534590dccd387f2fbc1884801aa6e73b645c5a38258ed059bf6")
    await fetch(client, "ctrl1-marker-by-id", Filter().id(marker_id))

    # 2. expA author, kind 1, no limit (plain REQ)
    expA_pk = PublicKey.parse(
        "e6fa3699f579617ed0c26bafe87596f016fe7b90854dc47a113e6bdf4f7b337c")
    await fetch(client, "ctrl2-expA-author-nolimit",
                Filter().author(expA_pk).kind(Kind.from_std(KindStandard.TEXT_NOTE)))

    # 3. bot gift wraps, no limit, bounded by since=now-7d then now-30d
    bot_pk = PublicKey.parse(
        "<redacted-bot-pubkey-hex>")
    f7 = Filter().pubkey(bot_pk).kind(Kind.from_std(KindStandard.GIFT_WRAP)).since(
        Timestamp.now().sub_duration(timedelta(days=7)))
    n7 = await fetch(client, "ctrl3-bot-gw-7d-nolimit", f7, timeout_s=20)
    if n7 == 0:
        f30 = Filter().pubkey(bot_pk).kind(Kind.from_std(KindStandard.GIFT_WRAP)).since(
            Timestamp.now().sub_duration(timedelta(days=30)))
        await fetch(client, "ctrl4-bot-gw-30d-nolimit", f30, timeout_s=25)

    await client.disconnect()
    await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
