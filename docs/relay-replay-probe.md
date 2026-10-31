# Relay replay probe — nos.lol, 2026-10-31

Empirical answer to DESIGN.md §9 open question 3: what does a relay actually
serve on a fresh kind-1059 gift-wrap subscription with `.limit(0)` and no
`.since`? Scripts and raw logs in `docs/probe/` (run against `wss://nos.lol`
with `nostr-sdk` 0.44.2 / Python 3.12; throwaway keys throughout).

## Verdicts

| Question | Answer on `wss://nos.lol` |
|---|---|
| `.limit(0)` + no `.since` replays history on fresh subscribe? | **NO** — immediate EOSE, 0 stored events, then live events only |
| Replays after disconnect/reconnect? | **NO** — consistent across two reconnect cycles |
| Does `.since(now−30s)` prevent replay? | Yes, but moot here — there is no replay to prevent |
| Would a fresh daemon with an empty dedup cache reprocess old DMs? | **No** on nos.lol. ⚠️ Caveat below |

**Key finding:** nos.lol interprets `limit: 0` as *"serve zero stored
events"* while the subscription stays open and **live events are delivered
normally**. Proven in both directions: the `limit:0` subscription served 0
events despite matching stored data, and a plain REQ without `limit` served
that same data minutes later.

## Experiment A — kind-1 notes, throwaway key

Filter on the wire: `{"authors":["e6fa…"],"kinds":[1],"limit":0}`

| Phase | Result |
|---|---|
| Fresh sub, `limit(0)` | EOSE at t+0.36s, 0 events (fresh key) |
| Publish marker note | live delivery on the same sub in 0.5s |
| Reconnect #1 / #2, same filter | EOSE ≈0.2s, **0 events in 15s** each — marker not replayed |
| Decisive controls | fetch-by-id → 1 event; plain REQ without limit → 1 event |

## Experiment B — kind-1059 gift wraps, bot pubkey

Filter: `{"kinds":[1059],"#p":["90d6…"],"limit":0}`

| Phase | Result |
|---|---|
| Fresh read-only client, `limit(0)`, 20s | EOSE at t+0.33s, **0 gift wraps** |
| Same + `.since(now−30s)` | 0 gift wraps |
| Control: same, `since(now−7d)`, **no limit** | **393 stored gift wraps** served (5.6-day span) |

Data exists (393 events), yet the `limit(0)` subscription serves none of it.

## Caveats and flags

1. **Relay-dependence:** NIP-01 does not define `limit: 0` semantics. Some
   relays treat `0` as "unlimited" — on those, a cold-start daemon WOULD
   replay everything matching. Remediation shipped: nostrbot-sdk v0.6.0
   `gift_wrap_lookback_seconds` (default 49 h = NIP-59's 48 h created_at
   randomization window + 1 h boundary slack) adds a `.since` to the
   gift-wrap filter, capping any worst-case replay to the window; the SDK
   dedup absorbs the rest. (`.since(now)` alone would silently drop real
   DMs — that is exactly the NIP-59 footgun.)
2. **Privacy:** NIP-59 protects the payload only. Gift-wrap *metadata*
   (recipient `#p`, volume, timing) is world-readable — a throwaway read-only
   key could observe all 393 of the bot's gift wraps in retention. Assume DM
   metadata is public when choosing relays.
3. No NOTICE/CLOSED/rate-limiting observed; publishes OK'd instantly; EOSE
   latency 0.16–0.4s.
