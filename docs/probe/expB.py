#!/usr/bin/env python
"""Experiment B: gift-wrap (kind 1059) replay against the bot's pubkey on nos.lol.

Phase 1: throwaway read-only client, subscribe {pubkey=bot, kinds=[1059], limit:0} (no since) -> 20s count
Phase 2: same but with .since(now-30s) -> count
Never prints the private key; only derives and prints the public key.
"""
import asyncio
import time
from datetime import timedelta

from nostr_sdk import (
    Client, ClientBuilder, Filter, Kind, KindStandard, Keys, HandleNotification, RelayUrl,
)

RELAY = "wss://nos.lol"
LOG = open("/var/tmp/opencode/nostr-probe/expB.log", "w", buffering=1)


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
        self._record("handle", subscription_id, event)

    def _record(self, via, sub_id, event):
        eid = event.id().to_hex()
        created = event.created_at().as_secs()
        self.sink.append((eid, created))
        log(f"[{self.label}] EVENT sub={str(sub_id)[:12]} id={eid} created_at={created}")

    async def handle_msg(self, relay_url, msg):
        m = msg.as_enum()
        tn = type(m).__name__
        if "END_OF_STORED_EVENTS" in tn:
            log(f"[{self.label}] EOSE sub={str(getattr(m, 'subscription_id', None))[:12]} t={time.time():.3f}")
        elif "NOTICE" in tn:
            log(f"[{self.label}] NOTICE {getattr(m, 'message', m)!r}")
        elif "CLOSED" in tn:
            log(f"[{self.label}] CLOSED sub={getattr(m, 'subscription_id', None)} msg={getattr(m, 'message', '')!r}")


def bot_pubkey():
    secret = None
    with open("/Users/butter/.hermes/.env", "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("NOSTR_PRIVATE_KEY="):
                secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not secret:
        raise RuntimeError("NOSTR_PRIVATE_KEY not found in /Users/butter/.hermes/.env")
    keys = Keys.parse(secret)
    del secret
    return keys.public_key()


async def probe(label, bot_pk, since_secs=None):
    client = ClientBuilder().build()  # no signer: read-only throwaway
    await client.add_relay(RelayUrl.parse(RELAY))
    await client.connect()
    await client.wait_for_connection(timedelta(seconds=10))
    log(f"[{label}] connected")

    f = Filter().pubkey(bot_pk).kind(Kind.from_std(KindStandard.GIFT_WRAP)).limit(0)
    if since_secs is not None:
        f = f.since(Timestamp.now().sub_duration(timedelta(seconds=since_secs)))
    log(f"[{label}] filter={f.as_json()}")

    sink = []
    handler = asyncio.create_task(client.handle_notifications(Recorder(label, sink)))
    out = await client.subscribe(f)
    log(f"[{label}] subscribe -> {out}")
    before = len(sink)
    await asyncio.sleep(20)
    served = sink[before:]
    created = [c for (_i, c) in served]
    log(f"[VERDICT {label}] served {len(served)} gift wrap(s) in 20s")
    if served:
        log(f"[{label}] oldest created_at={min(created)} newest={max(created)} "
            f"(span={max(created)-min(created)}s = {(max(created)-min(created))/86400:.1f} days; now={int(time.time())})")
        log(f"[{label}] first5={[i[:16] for i, _c in served[:5]]}")
        log(f"[{label}] last5={[i[:16] for i, _c in served[-5:]]}")
    handler.cancel()
    await client.disconnect()
    await client.shutdown()
    return len(served)


async def main():
    bot_pk = bot_pubkey()
    log(f"bot pubkey hex = {bot_pk.to_hex()}")
    log(f"bot npub       = {bot_pk.to_bech32()}")

    n1 = await probe("B1-limit0-noSince", bot_pk, None)
    log(f"--- phase1 done: {n1} events; pausing 3s before phase 2")
    await asyncio.sleep(3)
    n2 = await probe("B2-limit0-since30", bot_pk, 30)
    log(f"=== Experiment B done: limit0-noSince={n1}, limit0-since30={n2}")


from nostr_sdk import Timestamp
if __name__ == "__main__":
    asyncio.run(main())
