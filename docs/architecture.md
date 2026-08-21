# Architecture

```
┌───────────────────────────── Browser ─────────────────────────────┐
│  React/TS SPA (Vite)                                              │
│  Plot · Manual · Jobs · Device(wizard) · Diagnostics · Settings   │
└──────────────┬───────────────────────────────┬────────────────────┘
        REST /api/*                     WS /api/ws/status
┌──────────────▼───────────────────────▼────────────────────┐
│ FastAPI (app/main.py)                                     │
│  routes.py (thin handlers) · WSHub (fan-out broadcaster)   │
│  ─────────────────────────────────────────────────────    │
│  DeviceManager ── single connection owner                  │
│  FileRegistry   ── uploads, sanitize/analysis cache        │
│  JobStore       ── SQLite (WAL), state machine, pub/sub    │
│  HardwareWorker ── THE only serial writer (queue+thread)   │
│   └─ ChunkedStreamer ── ESC .B software-checking handshake │
│  Pipeline       ── sanitize → analyze → vpype → HP-GL      │
└──────────────┬─────────────────────────────────────────────┘
               │ pyserial (FTDI USB ↔ RS-232-C ↔ DB25)
┌──────────────▼──────────────┐
│  HP 7475A (1024B buffer, 6  │
│  pens, A4/A3/A/B hard-clip) │
└─────────────────────────────┘
```

## Invariants

1. **One writer.** Every byte to the plotter flows through `HardwareWorker`
   (queue-serialized thread). API handlers only translate HTTP ↔ commands.
   Manual controls and job streaming therefore can never interleave mid-
   instruction.
2. **Fail-closed inputs.** SVG uploads are sanitized before storage; HP-GL
   uploads must pass the allowlist validator; job payloads are re-validated
   extents/pen ranges at prepare time.
3. **No fabricated completion.** Byte-count ≠ plot done. Completion waits for
   a queued `OA` reply (parse-order semantics) bounded by a timeout.
4. **Constants have one home.** `protocol.py` (bytes/codes) and `paper.py`
   (hard-clip/paper model), both manual-cited. Everything else imports.
5. **Testable without hardware.** `FakeHP7475A` (PTY) implements the buffered
   parse/execute model incl. fault injection; the same driver code path runs
   against it in CI and against the real plotter in the field.

## State machine (jobs)

QUEUED → PREPARING → READY → SENDING → PLOTTING → COMPLETING → COMPLETED
                         │         │          └→ DISCONNECTED (device lost)
                         └→ PAUSED ↺ (stop feeding; resume re-streams from offset)
Terminal: COMPLETED · CANCELLED · FAILED · DISCONNECTED (kept in history)

Transitions are enforced in `app/jobs/models.py::TRANSITIONS`; illegal
attempts raise `IllegalTransition` → HTTP 409.

## Data

SQLite (`data/hp7475a.sqlite3`, WAL): `jobs`, `settings` (kv), `schema_meta`.
Uploads under `data/files/<id>/` (source + meta.json with sanitize/analysis).
Previews under `data/previews/<job>.svg`.
Vectorize results under `data/vectorize/<id>/` (input + `output.svg`).

## Failure taxonomy (transport)

| Condition | Classification | Job effect |
|---|---|---|
| Query timeout (retries exhausted) | fatal | FAILED |
| ESC.B reply out of range / garbage | fatal | FAILED |
| ESC .E = 16 (buffer overflow) | fatal | FAILED + user diagnostic |
| Write error / port gone | fatal | DISCONNECTED |
| User pause | controlled | PAUSED (resume supported) |
| User cancel | controlled | CANCELLED (stop feeding; device reset is a separate explicit action) |
