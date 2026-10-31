# Hermes gateway setup brief

> **Hand this document to the Hermes CLI agent** (or follow it yourself).
> Goal: enable and verify the API server that `nostr-hermes-proxy` talks to,
> and empirically answer four open questions about its behavior. Do not
> modify any proxy code — this document only configures and probes the
> gateway.

## Context

`nostr-hermes-proxy` is a standalone daemon (repo: `~/nostr-hermes-proxy`)
that receives NIP-17 Nostr DMs from the owner's mobile client and forwards
them to the local Hermes gateway over its authenticated HTTP API
(`http://127.0.0.1:8642`), one long-lived Hermes session per proxy identity.
The gateway currently has no API server running (nothing listens on 8642 and
`API_SERVER_KEY` is absent from `~/.hermes/.env`).

## Task 1 — enable the API server

1. Append to `~/.hermes/.env` (create the key with
   `openssl rand -hex 32`; do not echo it into logs or commit it anywhere):

   ```
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<generated-secret>
   ```

2. Leave the bind defaults alone (`API_SERVER_HOST=127.0.0.1`,
   `API_SERVER_PORT=8642`) and do not set `API_SERVER_CORS_ORIGINS` (no
   browser callers).
3. Do not change `plugins.enabled` — the nostr plugin's presence/absence is
   a separate migration decision.
4. Restart the gateway (`hermes gateway restart`) and confirm the log line:
   `[API Server] API server listening on http://127.0.0.1:8642`.

## Task 2 — verify the basics

With `KEY=$(grep '^API_SERVER_KEY' ~/.hermes/.env | cut -d= -f2)`:

```bash
curl -s http://127.0.0.1:8642/health                                   # expect {"status":"ok"}
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8642/v1/capabilities
```

Record the full capabilities JSON — specifically which of these are
advertised: `session_*` flags, `run_*` flags, anything about steering
(`run_steering` or similar), `run_approval`, `run_unknown_resolution`.

## Task 3 — the four probes

Run each probe and record results in the template at the end. All four exist
because the proxy's design depends on their answers. Use throwaway session
titles (`probe-*`) and clean them up with `DELETE /api/sessions/{id}` at the
end.

**Probe A — Idempotency-Key on session chat** (determines duplicate-DM
protection):
1. `POST /api/sessions` `{"title": "probe-a"}` → note `id`.
2. `POST /api/sessions/{id}/chat` with header `Idempotency-Key: probe-a-key`
   and body `{"input": "Reply with exactly: ok-q1"}`. Note wall time.
3. Repeat step 2 exactly (same key, same body). Note wall time and compare
   the two responses byte-for-byte.

   Reading: identical + instant ⇒ replay (idempotency honored). A second
   full run ⇒ header ignored on this endpoint. HTTP 409
   `admission_conflict` ⇒ header honored with conflict semantics.
4. Also try the same pair on `POST /api/sessions/{id}/chat/stream` (read the
   terminal frames; note whether the stream exposes a `run_id`).

**Probe B — same-session concurrency** (determines the proxy's fallback when
a DM lands mid-turn):
1. Reuse the Probe A session. Fire two chats **concurrently** (background
   the first curl, start the second immediately) with different inputs
   (`"Reply with exactly: ok-q2a"` / `"Reply with exactly: ok-q2b"`), no
   idempotency headers.
2. Record both HTTP codes, both responses, and total wall time for each.
   Serialized (second finishes noticeably later) vs 409 vs 429 vs interleaved
   output — all are informative.

**Probe C — steering** (the design decision: a DM that arrives mid-turn
should steer the running turn, not queue):
1. `POST /v1/runs` with body
   `{"input": "Use the terminal tool to run exactly this command: sleep 20 && echo slept. Then reply DONE.", "session_id": "<probe-a-id>"}`
   → note the returned `run_id`.
2. While it runs (within ~5s), steer it:
   `POST /v1/runs/{run_id}/steer` — discover the accepted body shape from
   the API docs you have internally, or from a 422/400 error body; try a
   JSON body carrying text such as
   `{"input": "Skip the sleep. Reply exactly: steered-ok"}` (adjust field
   name per the error's guidance: could be `input`, `text`, `message`, or
   `prompt`).
3. Poll `GET /v1/runs/{run_id}` until terminal; record the final `output` and
   whether the steer text visibly changed the outcome.
4. Record the exact working steer request (endpoint, body, response code).
   If steering is unsupported/refused, record the exact error — the proxy
   will keep queueing as fallback.

**Probe D — session durability across gateway restart** (the proxy remembers
session ids across its own restarts; the question is whether Hermes does too):
1. `GET /api/sessions/{probe-a-id}` and
   `GET /api/sessions/{probe-a-id}/messages` — note both succeed.
2. Restart the gateway again.
3. Repeat step 1. Record whether the session and its history survived.
   (Either answer is fine: if sessions survive, great; if not, the proxy
   recreates on 404 — but we want to know which world we live in.)

## Task 4 — report back

Fill in and return this table (plus the capabilities JSON from Task 2):

| Probe | Result | Evidence (codes / timings / outputs) |
|---|---|---|
| A: session-chat idempotency | replay / ignored / 409 / other | |
| A2: stream exposes run_id? | yes / no | |
| B: concurrent same-session turns | serialized / 409 / 429 / other | |
| C: steering | works (body shape: …) / unsupported (error: …) | |
| D: session survives gateway restart | yes / no | |

Cleanup: `DELETE /api/sessions/{id}` for every `probe-*` session created.

## Security notes

- `API_SERVER_KEY` grants **full agent toolset access including terminal
  commands** — it never leaves this machine except into the proxy's own env
  (`HERMES_API_KEY`), and never enters git.
- The proxy's own secrets (per-personality nsecs) live in a 0600-perm env
  file consumed by its LaunchAgent; nothing to do there in this brief.
