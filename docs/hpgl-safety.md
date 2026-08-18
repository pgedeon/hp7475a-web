# HP-GL Safety Model

Why job HP-GL is constrained, and how. Enforced in
`backend/app/services/pipeline/validator.py` using constants from
`backend/app/services/serial/protocol.py` (manual-cited).

## Rules (job payloads)

| # | Rule | Rationale |
|---|------|-----------|
| 1 | **Allowlist only** — `SAFE_HPGL_COMMANDS`; anything else is an error | Unknown instructions on a 1980s parser = undefined behavior (error 1 at best) |
| 2 | **Output instructions forbidden** (`OA OC OE OF OI OO OP OS OW OD`) | They inject replies into the stream the streamer doesn't expect, breaking the ESC .B handshake loop |
| 3 | **No ESC bytes anywhere** | Device-control instructions reconfigure the interface mid-job (could disable the handshake the streamer depends on) |
| 4 | `IN`/`DF` only at stream start | Mid-stream init discards pending state mid-plot |
| 5 | **Pen numbers 0–6** | Carousel has 6 stations; 0 = park current pen |
| 6 | **Extent check vs hard-clip** of the selected paper (warning ≤200u beyond; error further) | Off-page commands are silently clipped by the plotter — a silent wrong-output class we surface instead |
| 7 | Instructions `;`-terminated; chunks split only at terminators | A split instruction across a buffer-full pause would parse as two garbage instructions |
| 8 | Payload ≤ 5 MB | Planner sanity cap |
| 9 | `FS`/`PW` flagged "accepted but ineffective" | Documented as no-ops for this model — honest UX, not a hard fail |

## Generated payloads (pipeline)

Order: optional `IN;` → per layer: `SP n;` → `PU`/`PD`+`PA` runs (from
optimized geometry) → final `PU <corner>;` `SP0;` (park, per vpype
`final_pu_params` convention) → completion sentinel `OA;` appended by the
**streamer** (not stored in the payload, keeping rule 2 intact).

## Why software-checking (ESC .B) is the default

Manual §10: the plotter's ENQ/ACK "dummy handshake" answers ACK regardless of
buffer space — explicitly not overflow-safe; hardwire DTR depends on cable
wiring; XON/XOFF is best-effort through USB adapters. ESC .B polling is the
only mode where the host *knows* free space before every write. Margins:
never send more than `free − 32` bytes; ≤512-byte chunks.
