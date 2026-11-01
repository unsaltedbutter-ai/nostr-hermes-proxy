# HERMES DIAGNOSTICS — why didn't my cron DM arrive?

> **Hand this to the Hermes CLI agent.** Use it when the owner says a cron
> delivery (finance summary, weigh-in reminder, reconcile ping) did not show
> up on Nostur. It answers "was it accepted, is it stuck, or was it never
> sent?" from the daemon's live outbox — no daemon log reading required.

## What this is

A read-only status endpoint on the same loopback control plane that accepts
outbound DMs:

- **Endpoint:** `GET http://127.0.0.1:8787/v1/outbox/status`
- **Auth:** `Authorization: Bearer $TOKEN`, token from
  `~/.config/nostr-hermes-proxy/control-token` (0600)
- **Bound to `127.0.0.1` only.** It is reachable from this machine and
  nowhere else.

The endpoint is **read-only**. It never sends a DM, never drains or reorders
the queue, never retries, and never changes state. Reading it is always safe,
including while a cron job is mid-flight. Polling it is harmless.

## How to read it

Read the token into a shell variable and keep it there — **never paste the
token into a prompt, a log line, a report, or a repo**:

```bash
TOKEN=$(cat ~/.config/nostr-hermes-proxy/control-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/v1/outbox/status | python3 -m json.tool
```

A healthy response looks like:

```json
{
  "pending_total": 1,
  "personalities": [
    {
      "name": "Sam-I-Am",
      "pending": 1,
      "messages": [
        {
          "message_id": "6f1c0f5c0b9a4d2e8f3a1c7d5e0b9a42",
          "created_at": 1730000000.0,
          "attempts": 0,
          "next_attempt_at": 0.0,
          "chunks": 2,
          "preview": "First 80 characters of the message text..."
        }
      ]
    }
  ],
  "rate_limit_per_minute": 10
}
```

### Fields

| Field | Meaning |
|---|---|
| `pending_total` | Messages accepted but not yet delivered across all personalities. `0` means the queue is empty. |
| `personalities[]` | **Only queues with pending messages appear.** The outbox is queue-derived and does not know the configured personality names, so an empty list does not mean no identities are configured — it means nothing is queued. |
| `personalities[].pending` | Pending messages for that identity. |
| `messages[]` | Oldest first (strict FIFO order; the head is the one being retried). |
| `message_id` | The id `nostr-send` / `POST /v1/dm` returned as `message_id`. Match it to the cron run's output. |
| `created_at` | Enqueue time, Unix seconds. |
| `attempts` | Delivery attempts so far. `0` = never tried yet (normal for a fresh 202); `> 0` = the daemon retried after a relay failure. |
| `next_attempt_at` | Unix seconds of the next retry. In the future = exponential backoff is active: the daemon is waiting and will try again on its own. `0.0` = no retry scheduled. |
| `chunks` | Gift wraps the text was split into (long messages chunk automatically). |
| `preview` | First 80 characters of the text, single line. This is the caller's own message; it is the fastest way to confirm the right content was queued. |
| `rate_limit_per_minute` | The daemon's per-identity send cap. |

## Decision guide

Combine the status with the exit code of the `nostr-send` call from the cron
run:

| Observation | Diagnosis | Action |
|---|---|---|
| `pending_total` drains to `0` (or already is) and `nostr-send` exited `0` | Delivery was accepted and published; nothing is stuck. | If the owner still sees nothing, it is a client-side (Nostur) issue — relays accepted the event. |
| A message sits with `attempts` growing and `next_attempt_at` in the future | Relay delivery is failing and being retried with exponential backoff (up to 5 attempts, then dropped). | **Report to the owner**; do not loop retries. The daemon keeps trying on its own until the message is sent or dropped. |
| `personalities` is `[]` **and** `nostr-send` exited `2` | The daemon was unreachable, so nothing was even queued — the process is down. | Start it: `launchctl kickstart -k gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy`, then re-check status. |
| `nostr-send` exited `1` | The HTTP call failed; `nostr-send` printed the error body (`400` invalid request, `401` bad token, `403` unknown personality, `429` rate cap, `503` queued but bot unhealthy). | Fix the cause named in the body: a `401`/`403` is a token or personality **config** problem, not a delivery problem. |
| Message drained and `nostr-send` exited `0` | Accepted (`202`) and published, or the idempotency key returned the stored result (`duplicate: true`). | Report success. |

Exit-code recap: `0` = accepted (202) or duplicate · `1` = HTTP error, body
printed · `2` = daemon unreachable/timeout.

## Rules (do not violate)

- The endpoint is **read-only**: diagnostics never send DMs and never modify
  queues, rate state, or idempotency entries.
- Never print or paste the control token. Read it into a shell variable from
  the 0600 file each time; quote nothing else from that file.
- The `preview` is message content. Keep reports to the owner about *whether*
  a message is pending; only quote content when the owner asks.
- Do not "fix" a retrying message by re-sending it with a new idempotency key
  — that can deliver a duplicate. Wait for the backoff, or report to the
  owner.
- If the daemon is down, report it plainly and let launchd restart it; do not
  start loops of retries or background pollers.
