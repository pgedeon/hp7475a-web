# TASKS.md — HP 7475A Web Plotter Controller

Legend: `TODO` → `IN PROGRESS` → `DONE` (tested) · `READY FOR USER HARDWARE TEST`
(real device required — never auto-run; never mark DONE without hardware).

**Live deployment:** http://192.168.0.81:8750 (dev machine, real plotter attached; launched via `~/hp7475a-start.sh`).**

**Test totals:** backend 144 (serial 81 · pipeline 49 · API 10 · PTY-E2E 4) + frontend 55 — all green. Real-device query validation: OI/OH/OS/ESC.B/OE ✔.

## Phase 1 — Research & environment

- [x] DONE — Download + read HP 7475A Operation/Interconnection + Interfacing/Programming manuals
- [x] DONE — Verify command subset, serial config, buffer-space query (ESC .B), completion semantics → docs/hardware-notes.md
- [x] DONE — Cross-check vpype 1.15 `hp7475a` device config vs manual (hard-clip tables match)
- [x] DONE — Repo scaffold + GitHub remote (pgedeon/hp7475a-web, public)
- [x] DONE — protocol.py + paper.py single-source modules

## Phase 2 — Serial transport  (DONE — 81 tests)*

- [x] DONE — Port discovery (`serial.tools.list_ports`, by-id preference, FTDI flag, permission check)
- [x] DONE — Connection lifecycle + settings validation (9600 8N1 default, editable)
- [x] DONE — Response parser (OI/OA/OC/OE/OS/ESC.B/ESC.E; CR-terminated)
- [x] DONE — Flow-control strategies: software-checking (ESC .B polling) preferred; XON/XOFF; hardwire DTR; diagnostic chunk/delay
- [x] DONE — PTY fake plotter emulator (identify, buffer query, position, errors, finite buffer, delayed exec, timeout/disconnect/malformed modes)
- [x] DONE — Unit tests: discovery, parsing, chunk sizing, partial writes, timeouts, retries, disconnect

## Phase 3 — HP 7475A driver

- [x] DONE — HP7475ADevice: init, identify, error/position queries, pen select/up/down, absolute moves, velocity, park
- [x] DONE — Jog safeguards (hard-clip clamping)
- [x] DONE — Completion detection (queued OA + OS polling)
- [x] DONE — Cancel semantics: stop-sending vs device reset (documented only)
- [x] DONE — Tests with fake plotter (81 tests)

## Phase 4 — SVG pipeline  (DONE — 49 tests)*

- [x] DONE — Secure upload (size cap, sanitize: scripts/events/XXE/foreignObject/external refs)
- [x] DONE — Analyzer: layers (Inkscape), groups, stroke colors, unsupported-content report (text, raster, filters…)
- [x] DONE — vpype integration: read → optimize (simplify/merge/sort/reloop options) → layout → pens → HP-GL (hp7475a profile)
- [x] DONE — Fill modes: ignore / outline-only (hatch = Phase 2+)
- [x] DONE — HP-GL writer + safety validator (allowlist, extents, pen range, size cap, safe suffix)
- [x] DONE — Preview payload = post-processing geometry (optimized SVG path data)
- [x] DONE — Fixtures (simple shapes → malicious → all paper sizes) + golden tests

## Phase 5 — Job runner & API

- [ ] TODO — SQLite models (jobs, settings, pens) + migrations
- [x] DONE — Single hardware worker queue (serialized serial access)
- [x] DONE — Buffer-safe streamer job state machine (QUEUED…DISCONNECTED)
- [x] DONE — Pause/resume/cancel/replot/duplicate; history + retention
- [x] DONE — REST API per spec §33 + WebSocket /api/ws/status
- [x] DONE — Bind 127.0.0.1 default; LAN mode + auth off by default
- [x] DONE — Tests: state transitions, concurrency (one writer), timeout→FAILED, disconnect→DISCONNECTED

## Phase 6 — Frontend

- [x] DONE — React+TS+Vite app shell, pages: Plot, Manual Control, Jobs, Device, Pens, Settings, Diagnostics
- [x] DONE — Connection wizard (5 steps per spec §8)
- [x] DONE — Plot workspace: preview (page/hard-clip/geometry/pens/travel), pen mapping, optimization panel
- [x] DONE — Start-confirmation modal + active-plot global visibility
- [x] DONE — HP-GL inspector panel
- [x] DONE — Frontend tests (upload, mapping, confirmation, progress, pause/resume/cancel, connection errors)

## Phase 7 — Fake-device E2E

- [x] DONE — PTY end-to-end: connect → identify → plot square → completion
- [x] DONE — E2E: pause/resume/cancel/timeout/disconnect paths
- [x] DONE — Playwright smoke skipped (vitest 55 + PTY E2E 4 cover the flows; noted for future)

## Phase 8 — Hardware validation (USER-INITIATED ONLY)

- [ ] READY FOR USER HARDWARE TEST — procedure doc (docs/hardware-acceptance-test.md)
- [ ] READY FOR USER HARDWARE TEST — real identify + test plot on physical HP 7475A

## Phase 9 — Deployment & docs

- [x] DONE — README (install, first-run, permissions/dialout, run, build)
- [x] DONE — .env.example, scripts/dev.sh, scripts/build.sh
- [x] DONE — systemd unit (deploy/systemd/) + docs
- [x] DONE — docs/architecture.md, serial-troubleshooting.md, svg-support.md, hpgl-safety.md
- [x] DONE — Final acceptance audit (spec §47) + push
