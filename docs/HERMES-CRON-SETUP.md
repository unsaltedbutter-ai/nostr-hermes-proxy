# HERMES CRON SETUP — outgoing Nostr DMs via nostr-hermes-proxy

> **Hand this to the Hermes CLI agent.** It configures the cron jobs that
> need to push messages to the owner over Nostr. The outbound path is LIVE
> and acceptance-tested; this brief only wires the jobs to it.

## What is available (verified live)

A control plane inside the `nostr-hermes-proxy` daemon (LaunchAgent
`ai.unsaltedbutter.nostr-hermes-proxy`, auto-restarted by launchd):

- **Endpoint:** `http://127.0.0.1:8787/v1/dm` — loopback only.
- **Auth:** `Authorization: Bearer $(cat ~/.config/nostr-hermes-proxy/control-token)`
- **`nostr-send` CLI** — installed in the proxy venv and symlinked at
  `~/.local/bin/nostr-send`; the cron instruction uses the absolute
  venv path so minimal-PROMPT cron shells work regardless of PATH:
  `/Users/butter/nostr-hermes-proxy/venv/bin/nostr-send`
- **Verified behaviors** (acceptance run 2026-10-31): 202 on accept ·
  idempotency-key retries return the stored result with `"duplicate": true`
  and never send twice · long texts auto-chunked · 401 on bad token ·
  exit 2 with a clear error when the daemon is unreachable · published
  gift wraps decrypt in the owner's Nostur client.

## Contract

```bash
nostr-send [options] "<text>"        # or --file PATH for long output
  --idempotency-key K   # dedupe: same key within 24h → no second send
  --personality NAME    # optional; omitted → the daemon's default (Sam-I-Am)
  --expiry SECONDS      # optional; default 7 days
  --port / --token-path # override defaults if ever needed
```

- Identities configured (2026-11-01): `Sam-I-Am` (the daemon's
  `default_personality`), `nostrmix-bot`, `unsaltedbutter-bot`, `Notible`.
  Omitting `--personality` sends as Sam-I-Am; name another identity
  explicitly when the owner asks for it.

- Exit codes: `0` sent (or duplicate) · `1` HTTP error (400/401/403/429/503
  body printed) · `2` daemon unreachable/timeout.
- The recipient is fixed to the owner's npub by the daemon config — callers
  cannot choose a destination.
- Rate cap: 10 sends/minute. Queue-and-return: the 202 does not wait on relay
  I/O; delivery failures retry in the daemon (5 attempts, exponential
  backoff) and are logged.

## Task 1 — migrate the four cron jobs

For each of these jobs:

| Job | Job ID |
|---|---|
| `Finance: morning summary` | `07c00aea75f5` |
| `Finance: predict` | `c1dce05f56ff` |
| `retatrutide-weight-check` | `4acfe351dd47` |
| `job-store-reconcile` | `2a5e0f808e3f` |

1. Set its delivery to `local` (output stays in the cron run store for
   debugging; nothing tries the removed nostr adapter).
2. Prepend this instruction block to the prompt:

```
DELIVERY: Send your final output to the user by running:
  /Users/butter/nostr-hermes-proxy/venv/bin/nostr-send \
    --idempotency-key <job-name>-$(date +%F) --file <(your output)
If your result is [SILENT], do NOT run nostr-send — emit [SILENT] as usual.
If nostr-send exits nonzero, state "NOSTR-SEND FAILED" as the first line of
your final output so the failure is visible in the cron run store.
```

`job-store-reconcile` runs a script rather than a conversational turn: its
script calls `nostr-send` directly with the same daily idempotency-key
pattern (`job-store-reconcile-$(date +%F)`).

The daily key makes gateway/daemon restarts safe: a retried cron run on the
same day is a no-op if the first send already succeeded.

## Task 2 — verify

1. Trigger one dry-run of each migrated job (or run its prompt once).
2. Confirm in the cron run store: exit 0 and the delivery line.
3. The owner receives the DMs on Nostur.

## Rules (do not violate)

- Never put the control token in a prompt, a log line, or a repo — it lives
  only at `~/.config/nostr-hermes-proxy/control-token` (0600).
- Do not add recipients: the daemon rejects them (the endpoint is
  personality-locked by design).
- If `nostr-send` exits 2 (daemon down), report it as
  "NOSTR-SEND FAILED: daemon unreachable" — do not retry in a loop; the
  owner monitors the daemon via launchd (KeepAlive) and the watchdog.
