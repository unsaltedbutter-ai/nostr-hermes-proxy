> Copied from [`unsaltedbutter-ai/hermes-nostr`](https://github.com/unsaltedbutter-ai/hermes-nostr)@`2f1a5f8` (2026-10-31).
> The canonical copy lives with the gateway plugin; this repo owns the
> follow-up remediations (see [DESIGN.md](DESIGN.md) §7, Relay hygiene).

# Nostr Integration — Incident Log

Record of delivery failures between Nostur (iOS) and the Hermes gateway, with the
facts observed at the time and what we changed. All timestamps Pacific unless noted.

## Summary of failure modes

| # | Failure mode | Where the fault was | Status |
|---|--------------|--------------------|--------|
| 1 | Hung notification loop — bot silently deaf while relays show CONNECTED | hermes-nostr plugin / nostr-sdk | Fixed (`edbbf8a`) |
| 2 | DM published to relays the bot doesn't listen on | Relay-set mismatch client vs bot | Fixed (aligned `NOSTR_RELAYS`) |
| 3 | Wrong npub — DMs encrypted to a key the bot doesn't hold | Config | Fixed |
| 4 | Nostur silently fails to publish (message never leaves the phone) | iOS client | Unfixed (transient; workaround: reopen app + resend) |
| 5 | Relay accepts gift-wrap writes but never serves them back | relay.damus.io | Unfixed (live hazard) |

In every episode, the Hermes gateway itself was healthy — canary green, both
relays connected. Each "message never arrives" traced to Nostur not publishing
or to damus.io black-holding gift wraps. Both are invisible from either end.

---

## Incident 1 — Bot went silently deaf (Aug 21–26, 2026)

**Symptom:** Nostr DMs stopped being received; the bot sat silent for 2+ days.

**Facts observed**
- Relay pool reported CONNECTED throughout; `task.done()` health checks passed.
- The Rust notification task inside nostr-sdk had hung: socket open, frames
  queued, handler never pumped.
- Loop was observed flapping ~6x/min on Aug 25 before going silent.
- Gateway was otherwise healthy (canary every 15 min, 2/2 relays connected).

**Changes made**
- Commit `edbbf8a` — "fix(watchdog): detect hung notification loop + escalate
  on flapping":
  - Activity heartbeat refreshed in `handle()`/`handle_msg()`; a
    connectable-but-silent loop is unhealthy past
    `NOSTR_WATCHDOG_STALL_MINUTES` (default 120).
  - If unhealthy recurs within 5 min of a prior recovery, escalate from soft
    reconnect to a full client rebuild after `NOSTR_WATCHDOG_ESCALATE_AFTER`
    (default 3) recurrences.
  - 5 new tests; 18 passing at commit time.

## Incident 2 — Relay-set mismatch (Aug 21, 2026)

**Symptom:** A sent DM never arrived at the bot.

**Facts observed**
- Direct query of the bot's two relays: 0 gift-wrap (kind 1059) events to the
  bot's pubkey in the prior 6h on both `relay.damus.io` and `nos.lol`.
- The bot subscribes only to `NOSTR_RELAYS` = `wss://relay.damus.io,wss://nos.lol`;
  the sending client published to its own configured relays instead.
- Encryption/sending worked fine on the client — the event just went to a relay
  the bot never listens on.
- The bot does publish a NIP-10050 inbox list (its 2 relays) telling compliant
  clients where to send, but not all clients honor it.

**Changes made**
- Aligned the client's relay set with `NOSTR_RELAYS` (both sides now use
  `relay.damus.io` + `nos.lol`).

## Incident 3 — Wrong npub (Aug 2026)

**Symptom:** Inbound DMs not received despite correct relays.

**Facts observed**
- DMs were being sent to (or encrypted for) the wrong public key — not the
  npub whose private key the bot holds.

**Changes made**
- Corrected the npub in the sending client; after restart, inbound gift wraps
  landed and were processed end-to-end (verified: DM round-trip ~11s).

## Incident 4 — Nostur never publishes (Sept 3, 2026)

**Symptom:** A DM sent from Nostur never arrived; no reply.

**Facts observed**
- Hermes side healthy the whole time: canary DMs every 15 min, 2/2 relays
  connected, sending npub allow-listed.
- Scanned 6 major relays directly (damus, nos.lol, primal, snort, offchain,
  wine) for ALL event kinds from the user's key over 24h — zero events, not
  even non-DM kinds. The message was never published at all.
- Nostur uses only 2 relays — `relay.damus.io` and `nos.lol` — the same two as
  the bot, ruling out "published to an unscanned relay."
- A resend minutes later published instantly via `nos.lol` and round-tripped
  in ~11s.
- Conclusion: Nostur accepted the message locally but held it — dead socket or
  stuck outbox entry. Classic iOS backgrounding failure; identical server-side
  symptoms either way.

**Changes made**
- None (transient client-side). Workaround: reopen Nostur (forces relay
  reconnect) and resend. Diagnostic step: check the stuck message's
  pending/failed status in Nostur's outbox to distinguish queued vs
  disconnected.

## Incident 5 — damus.io black-holes gift wraps (Oct 27, 2026)

**Symptom:** `/new` DMs from Nostur produced no reply, apparently a Hermes fault.

**Facts observed**
- Gateway up 5h31m (PID 58761), canary HEALTHY every 15 min throughout;
  nothing changed server-side when delivery resumed.
- Relay timeline: zero gift wraps from the user's npub on either relay until
  23:12, when the user's test DMs arrived via `nos.lol` within seconds of
  reopening the app. The earlier messages were never published — gone, not
  queued (Nostur again; see Incident 4).
- Separate verified relay finding: `relay.damus.io` serves kind-1 notes fine,
  returns `OK` on writes for kind 4 and kind 1059, then never serves those
  events back — not by id, not by filter. It accepts writes it never stores.
- The bot advertises `relay.damus.io` FIRST in its kind-10050 NIP-17 inbox
  list, so any client that publishes only to the first advertised relay loses
  every DM with no error anywhere.
- `nostr_canary.py` hardcodes `RELAY = "wss://nos.lol"` — it proves the
  gateway is alive on the one relay that works and can never detect a dead or
  black-holed relay in `NOSTR_RELAYS`. This is why these failures ran for
  days without an alert.

**Verified relay status (write + readback of kind-1059 gift wraps, Oct 27, 2026)**

```
OK    nos.lol, relay.primal.net, relay.snort.social, nostr.mom,
      nostr-pub.wellorder.net
BLACK relay.damus.io, relay.nostr.net   (accept write, serve nothing)
DEAD  relay.nostr.band, nostr.wine, relay.nostr.bg, eden.nostr.land, nostr.land
```

**Changes made**
- None yet. Open remediation options agreed but not implemented:
  - Patch the canary to probe every relay in `NOSTR_RELAYS` and alert when any
    stops serving gift wraps (closes the blind spot without changing delivery).
  - Reorder the kind-10050 list so `nos.lol` is first (removes the trap for
    clients that only use the first advertised relay).
  - Considered but deferred: self-hosted relay on the VPS for a deterministic
    delivery path and per-event observability.

---

## Debugging playbook (what worked)

1. **Check the gateway first:** `gateway.log` lines
   `Nostr: received gift wrap <id> from <relay>` prove inbound flow.
2. **Prove delivery at the relay:** publish a canary gift wrap (NIP-17, kind
   1059) from a test key to the bot's npub, then fetch it back **by event id**
   from the same relay. Write-success without readback = black-holed relay.
3. **Separate "never published" from "published but not received":** query the
   bot's relays for kind-1059 events `#p` = bot pubkey over the window. Zero
   events of ANY kind from the sender's key ⇒ the client never published.
4. **Unwrap a suspect gift wrap** with the bot key
   (`UnwrappedGift.from_gift_wrap_async`) to read the inner rumor and confirm
   sender/content.
5. **When in doubt on the phone:** reopen Nostur and resend. iOS backgrounding
   silently kills relay sockets and holds outgoing messages.
