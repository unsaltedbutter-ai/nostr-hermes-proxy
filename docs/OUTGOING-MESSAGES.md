> Written by the Hermes CLI agent; filed here (the proxy repo) because this
> daemon is the implementation target. The copy in ~/nostr-dm-relay/ (the VPS
> relay repo) is where it was originally saved — that relay is NOT involved
> in outbound delivery: cron jobs POST to 127.0.0.1:8787 on this Mac (§2), and
> the daemon publishes to the public relays (§7).

# OUTGOING MESSAGES — proxy control plane for outbound Nostr DMs

Problem: the Hermes gateway's nostr plugin was removed (flakey). The
nostr-hermes-proxy covers **inbound** DMs (Nostr → Hermes), but several Hermes
cron jobs still need to **push** messages out (finance nudges, morning summary,
weigh-in reminders, watchdog alerts). This document specifies the missing
outbound half.

**Design goal:** one sender, one place for the nsec, zero new secrets, and cron
jobs that need nothing but a curl.

---

## 1. Architecture decision

Two possible shapes:

| Shape | Verdict |
|---|---|
| **A. Control-plane endpoint inside the proxy daemon** + thin `nostr-send` CLI | **Chosen.** The daemon already holds the identities, relay connections, and chunking/expiry logic. A cron job (or any local process) POSTs text; the daemon owns relay I/O. No relay session cold-starts per cron run, no duplicated nsec handling. |
| B. Standalone CLI that opens its own relay session per send | Rejected: cold-start latency in cron context, second nsec consumer, relay connection churn every morning at 8/9am. |

The endpoint is **loopback-only**. The proxy daemon already runs on this Mac;
the cron jobs run in Hermes on the same machine, so `127.0.0.1` is always
reachable.

## 2. HTTP API (added to the proxy daemon)

Bind: `127.0.0.1:<control_port>` (config addition, suggested `127.0.0.1:8787`;
must not collide with anything on the box — 8080 is LND-on-VPS, irrelevant here,
but keep it configurable). Auth: `Authorization: Bearer <token>`, token from
`/etc-style` config file `~/.config/nostr-hermes-proxy/control-token` (0600,
generated once at setup). Loopback bind + bearer token = two independent gates;
both cheap.

### `POST /v1/dm`

Send a NIP-17 gift wrap to the personality's allowlisted correspondent.

Request:
```json
{
  "text": "Morning finance digest:\n- ...",
  "personality": "hermes",          // optional; default = the single configured personality
  "expiry_seconds": 604800,         // optional; NIP-40 dm_expiration_tag, default from config
  "idempotency_key": "cron-finance-summary-2026-11-01"   // optional; see below
}
```

Rules:
- `text` is plain text (UTF-8), max 64 KB before chunking. The daemon chunks
  exactly like inbound replies (same `chunking.py` logic, same limits).
- **Recipient is never caller-supplied.** It is the personality's configured
  allowlisted npub. This keeps the endpoint from becoming an open relay if the
  token leaks: worst case, someone can text *you*.
- `idempotency_key`: if seen within the last 24 h (daemon keeps a small
  key→result map, persisted tmp+replace like `SessionMap`), return the original
  result with `"duplicate": true` and do **not** send again. Cron retries after
  a crash must not double-send.

Response `202 Accepted`:
```json
{
  "accepted": true,
  "message_id": "b3f0…",        // gift-wrap event id (assigned when queued)
  "chunks": 2,
  "duplicate": false
}
```

The endpoint **queues and returns** — it does not block on relay publish.
Delivery failures surface in the daemon log and the per-personality health
status; a `GET /v1/outbox/status?message_id=…` (optional, phase 2) can confirm
publication. Rationale: cron agents should not sit waiting on relay I/O; the
[ACCEPTED] ack is what the CLI returns.

Errors: `400` text too large/empty · `401` bad token · `403` personality
unknown · `429` local send-rate cap hit (configurable, default 10/min — relay
politeness) · `503` personality's Nostr connection down (message still queued
for retry; response says `"queued_for_retry": true`).

### `GET /v1/health`

Already-adjacent to the health hooks the daemon tracks: returns per-personality
relay connection state. The CLI uses this for its `--wait` flag.

## 3. CLI — `nostr-send`

A thin script (can live in the proxy repo, installed on PATH):

```
nostr-send [options] <text>
  --personality NAME   (optional, default personality)
  --expiry SECONDS     (optional, forwarded)
  --idempotency-key K  (optional)
  --file PATH          read text from file instead of argv (for long cron output)
  --timeout SECONDS    default 10; exit 2 if the daemon doesn't answer
```

Behavior: reads control-port + token from the config, POSTs, prints the JSON
response, exits 0 on 2xx, nonzero otherwise. Curl-equivalent one-liner for
agents that prefer it:

```bash
curl -s -X POST http://127.0.0.1:8787/v1/dm \
  -H "Authorization: Bearer $(cat ~/.config/nostr-hermes-proxy/control-token)" \
  -H 'Content-Type: application/json' \
  -d '{"text":"..."}'
```

## 4. Cron job integration

For each of the four jobs currently `deliver: "nostr"`:

1. Set `deliver` to `local` (run output stays in the cron store for debugging;
   nothing tries the dead nostr adapter).
2. Prepend one delivery instruction to the prompt:

```
DELIVERY: Send your final output to the user by running:
  nostr-send --idempotency-key <job-name>-$(date +%F) --file <(your output)
If your result is [SILENT], do NOT run nostr-send — emit [SILENT] as usual.
If nostr-send exits nonzero, state "NOSTR-SEND FAILED" as the first line of
your final output so the failure is visible in the cron run store.
```

Notes:
- `job-store-reconcile` runs a script rather than a conversational turn: its
  script calls `nostr-send` directly with the same idempotency-key pattern.
- The daily-key idempotency pattern (`<job>-<date>`) makes a gateway/daemon
  restart mid-run safe: a retried cron run the same day is a no-op if the
  first send succeeded.
- The 4 jobs to migrate: `Finance: morning summary` (07c00aea75f5),
  `Finance: predict` (c1dce05f56ff), `retatrutide-weight-check`
  (4acfe351dd47), `job-store-reconcile` (2a5e0f808e3f).

## 5. Security invariants

- Control plane binds `127.0.0.1` only — never `0.0.0.0` (same rule as every
  other service on our boxes).
- Bearer token file 0600, owned by the user; regenerate on any suspicion.
- Recipient fixed by personality config; callers cannot pick a destination.
- The nsec lives only where it lives today (proxy config); the cron/Hermes side
  never sees it.
- Sends pass through the same rate cap and NIP-40 default expiry as inbound
  replies; `expiry_seconds` can shorten but not exceed the configured max.

## 6. Acceptance checklist (before pointing cron at it)

- [ ] `nostr-send "test"` → 202, gift wrap visible on nos.lol, decrypts in your client
- [ ] Same idempotency key twice → second response `"duplicate": true`, one message
- [ ] Daemon stopped → CLI exits 2 with a clear error (cron visibility)
- [ ] 5 KB text → chunked correctly (client shows pieces in order)
- [ ] Token wrong → 401; token file perms 0600
- [ ] The four cron jobs migrated; one dry-run of each with a fake summary

## 7. Out of scope (deliberately)

- Outbound sends to arbitrary npubs (endpoint is personality-locked)
- Any inbound change — the existing DM→Hermes flow is untouched
- Delivery to the nip17 relay (`nip17.unsaltedbutter.ai`) — that inbox is for
  *incoming* DMs; outbound pushes go to your client via the public relays the
  proxy already uses. Revisit only if you later want the bot's outbound traffic
  to also live on the private relay.
