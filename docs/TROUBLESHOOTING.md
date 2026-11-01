# TROUBLESHOOTING — symptom-driven debugging

Work top to bottom in the section that matches your symptom. The
[paste-ready probe](#the-relay-probe) at the bottom answers "did the event
actually reach the relay?" — most mysteries resolve once you know that.
Historical context for every failure mode below lives in
[INCIDENTS.md](../INCIDENTS.md) (five post-mortems); relay query semantics
evidence in [relay-replay-probe.md](relay-replay-probe.md).

Quick triage:

| Symptom | Go to |
|---|---|
| Cron DM missing / late | [1](#1-a-cron-dm-didnt-arrive) then [HERMES-DIAGNOSTICS.md](HERMES-DIAGNOSTICS.md) |
| Bot didn't reply to a DM | [2](#2-a-bot-didnt-reply-to-a-dm) |
| Daemon down / nostr-send exit 2 | [3](#3-daemon-down-or-unreachable) |
| Bot replies stopped after a gateway restart | [4](#4-replies-stopped-after-a-gateway-restart) |
| Messages appear out of order or "in the past" | [5](#5-messages-look-out-of-order-or-time-stamped-in-the-past) |
| Profile or relay list missing on the relay | [6](#6-published-metadata-missing-from-a-relay) |
| Bot flapping / repeated reconnects in the log | [7](#7-watchdog-soft-reconnects) |
| pip/git weirdness when touching the SDK | [8](#8-sdk-install-pitfalls) |

## 1. A cron DM didn't arrive

1. Check `nostr-send`'s exit code convention (0 sent/duplicate · 1 HTTP error
   · 2 daemon unreachable) in the cron run store.
2. Follow [HERMES-DIAGNOSTICS.md](HERMES-DIAGNOSTICS.md): `GET
   /v1/outbox/status` shows pending messages, delivery attempts, and backoff.
   - Pending with `attempts: 0` → accepted, the outbox worker will send
     within seconds; re-check shortly.
   - Growing `attempts` with future `next_attempt_at` → relay delivery is
     failing; check relay reachability and the daemon log's delivery errors.
   - Empty queue + exit 2 → the daemon was down when the job ran; start it
     (below) and re-run the job.
3. Daemon log: `Accepted outbound message … for <personality>` means the
   control plane accepted it; `Outbound chunk …` lines mean it was published.

## 2. A bot didn't reply to a DM

Work down this list — each step splits the search space:

1. **Is the daemon alive?** `curl -s -H "Authorization: Bearer $TOKEN"
   http://127.0.0.1:8787/v1/health` (token: control-token file). If not,
   [3](#3-daemon-down-or-unreachable).
2. **Did the DM reach the relay?** Run [the relay probe](#the-relay-probe)
   for that identity's npub. No recent wrap on nos.lol → the *sender* never
   delivered (client-side relay resolution — the sender should be using the
   recipient's kind-10050 relays; check the sender client's relay set).
3. **Did the bot receive it?** The log does not log inbound DMs at INFO;
   evidence of processing is *outbound* activity (ack/profile lines,
   `Outbound chunk` replies). A wrap on the relay + zero activity → the bot
   didn't receive it (subscription/auth issue — check the
   `Subscribed to kind 4, 1059, 9735` line at startup and restart the
   daemon).
4. **Was it dropped silently?** Only `allow_npub` is accepted; any other
   sender gets *no ack and no error* — by design. Confirm the sender hex is
   the owner.
5. **Is it a command?** Commands (`/new`, `/model`, `/status`, `/stop`,
   `/help`) are handled proxy-side and never reach Hermes. Typos hit
   `unknown_text` ("Unknown command … /help").
6. **Gateway issues?** A turn failing gateway-side produces an error DM
   ("…unavailable" style) — its absence points back upstream. Session-gone
   recovery is automatic (see [4](#4-replies-stopped-after-a-gateway-restart)).

## 3. Daemon down or unreachable

1. `launchctl print gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy` —
   state, last exit status.
2. Read the tail of `/tmp/nostr-hermes-proxy.err.log` — config errors print
   there (`ConfigError: …` names the offending key).
3. Start/restart: `launchctl kickstart -k gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy`.
4. The control plane binds only after all personalities start; give the
   staggered startup ~40s before curling it.
5. If the gateway is down, the daemon still runs; DMs get an error reply
   rather than silence.

## 4. Replies stopped after a gateway restart

The proxy persists its session map (personality → Hermes session id). When
the gateway loses sessions, the first DM after recovery hits a 404 → the
client raises `HermesSessionGone` → the runtime recreates the session
(`nostr:<name> <UTC timestamp>` titles; uniqueness enforced with a random
suffix on collision) and retries the turn. This is self-healing; the only
visible trace is one discarded turn. If recovery *doesn't* happen, check the
session file (`state_dir/sessions.json`) for corruption and the gateway's
`/api/sessions` for the expected titles.

## 5. Messages look out of order or time-stamped in the past

**Expected behavior, not a bug.** NIP-59 randomizes every gift wrap's
`created_at` up to 2 days into the past for metadata privacy; clients order
by that timestamp. The proxy defeats scrambling by giving each chunk of a
multi-chunk message a random backdate within `outbound_spread_seconds`
(default 3600s) **sorted ascending** — chunk 1 is always the newest, so
clients display chunks in reading order, while each wrap still looks
individually random to relays. A single message may still appear "in the
past" (up to an hour) — that's the randomization doing its job. Full
rationale: [DESIGN.md](../DESIGN.md) (delivery row) and the SDK's
`make_gift_wrap_async` docs.

## 6. Published metadata missing from a relay

Symptom: senders can't resolve an identity's DM relays (kind-10050), so
NIP-17 DMs strand. Cause: relay per-IP rate limits silently reject publish
bursts (happened to Notible on 2026-11-01 — three identities' metadata
landed, the fourth didn't, no error anywhere).

The daemon self-heals: every connect/reconnect republishes kind
0/10002/10050 (nostrbot-sdk v0.9.0), starts are staggered, and a startup
verification pass re-checks each identity's list on nos.lol, republishing
missing ones. Healthy log shows
`DM relay list verified on wss://nos.lol for <name>` per identity. To verify
externally, use [the relay probe](#the-relay-probe) against `kind 10050`.

## 7. Watchdog soft-reconnects

`Watchdog polling …` lines are normal; a soft-reconnect appears as
`Restarting NostrBot (soft reconnect)`. Since SDK v0.9.0 the reconnect
republishes metadata (see [6](#6-published-metadata-missing-from-a-relay)).
Frequent reconnects indicate relay instability or gateway stalls — the
watchdog watches both (interval/threshold/stall knobs in config). If a
relay flaps constantly, read
[INCIDENTS.md → Incident 5](../INCIDENTS.md) before touching relay sets:
removing a relay has DM-routing consequences.

## 8. SDK install pitfalls

- After moving a nostrbot-sdk git tag, `pip install` needs
  `--no-cache-dir --force-reinstall --no-deps` or the cached wheel of the
  old tag wins.
- `nostr-sdk` (the Rust bindings) is not on PyPI — always `--no-deps` when
  reinstalling nostrbot-sdk into this venv, or pip fails resolving it.
- NIP-42 auth in the uniffi bindings requires the event loop to be set
  *before* `client.connect()` — the SDK does this; if you see
  `_UniffiTraitImplAuthenticatorImpl.make_auth_event` ctypes tracebacks in
  the log, you are looking at pre-0.7.0-era noise (benign; all current logs
  are clean).

## The relay probe

Paste-ready check: "did an event reach nos.lol?" Swap `KIND`/filter per
need (`Kind(10050)` for DM relay lists, gift-wrap filter for DMs, `Kind(0)`
for profiles). Note: the private relay hides gift wraps from
unauthenticated readers — probe nos.lol, or authenticate as the bot for the
private relay.

```bash
cd ~/nostr-hermes-proxy && venv/bin/python - <<'PY'
import asyncio, datetime, time
from nostr_sdk import ClientBuilder, Filter, Kind, KindStandard, PublicKey, RelayUrl, ReqTarget

NPUB = "npub1…        # recipient (gift wraps) or author (10050/profile)
KIND = "wrap"         # "wrap" | "10050" | "0"

async def main():
    client = ClientBuilder().build()
    await client.add_relay(RelayUrl.parse("wss://nos.lol"))
    await client.connect()
    pk = PublicKey.parse(NPUB)
    if KIND == "wrap":
        f = Filter().pubkey(pk).kind(Kind.from_std(KindStandard.GIFT_WRAP))
    elif KIND == "10050":
        f = Filter().author(pk).kind(Kind(10050))
    else:
        f = Filter().author(pk).kind(Kind(0))
    events = await client.fetch_events(
        ReqTarget.auto([f]), datetime.timedelta(seconds=6),
    )
    got = list(events) if not isinstance(events, list) else events
    now = int(time.time())
    print(f"{len(got)} event(s) stored; recent:")
    for e in sorted(got, key=lambda e: e.created_at().as_secs(), reverse=True)[:5]:
        age = now - e.created_at().as_secs()
        extra = ""
        if KIND == "10050":
            extra = " → " + str([t.to_vec()[1] for t in e.tags() if t.to_vec()[0] == "relay"])
        print(f"  now-{age:>6}s  id {e.id().to_hex()[:12]}{extra}")
    await client.disconnect()

asyncio.run(main())
PY
```

Identity npubs (public): Sam-I-Am `npub1jrttwdf479rtny9raxrcdcnuguygyyy34u5k6fun6xpaugvdk40qteq6na`,
unsaltedbutter-bot `npub1hssdvydgqjx9y6ptlkt23sc5uptnqkc3q2r8j68zpdeyt9psl27s534rcr`,
nostrmix-bot `npub1g27uaw60wmt3j39a72rp73v36a49e0vmj4kw54l6pvyh3248dp7qzcj6jn`,
Notible `npub1ggc3542cpeqhpqdspek5wrhjjjtsxjv0arptvk94f39gqey8htpsa3jw0d`.
