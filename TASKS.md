# TASKS.md — HP 7475A Web Plotter Controller

Legend: `TODO` → `IN PROGRESS` → `DONE` (tested) · `READY FOR USER HARDWARE TEST`
(real device required — never auto-run; never mark DONE without hardware).

## Phase 1 — Research & environment

- [x] DONE — Download + read HP 7475A Operation/Interconnection + Interfacing/Programming manuals
- [x] DONE — Verify command subset, serial config, buffer-space query (ESC .B), completion semantics → docs/hardware-notes.md
- [x] DONE — Cross-check vpype 1.15 `hp7475a` device config vs manual (hard-clip tables match)
- [x] DONE — Repo scaffold + GitHub remote (pgedeon/hp7475a-web, public)
- [x] DONE — protocol.py + paper.py single-source modules

## Phase 2 — Serial transport

- [ ] IN PROGRESS — Port discovery (`serial.tools.list_ports`, by-id preference, FTDI flag, permission check)
- [ ] TODO — Connection lifecycle + settings validation (9600 8N1 default, editable)
- [ ] TODO — Response parser (OI/OA/OC/OE/OS/ESC.B/ESC.E; CR-terminated)
- [ ] TODO — Flow-control strategies: software-checking (ESC .B polling) preferred; XON/XOFF; hardwire DTR; diagnostic chunk/delay
- [ ] TODO — PTY fake plotter emulator (identify, buffer query, position, errors, finite buffer, delayed exec, timeout/disconnect/malformed modes)
- [ ] TODO — Unit tests: discovery, parsing, chunk sizing, partial writes, timeouts, retries, disconnect

## Phase 3 — HP 7475A driver

- [ ] TODO — HP7475ADevice: init, identify, error/position queries, pen select/up/down, absolute moves, velocity, park
- [ ] TODO — Jog safeguards (hard-clip clamping)
- [ ] TODO — Completion detection (queued OA + OS polling)
- [ ] TODO — Cancel semantics: stop-sending vs device reset (documented only)
- [ ] TODO — Tests with fake plotter

## Phase 4 — SVG pipeline

- [ ] TODO — Secure upload (size cap, sanitize: scripts/events/XXE/foreignObject/external refs)
- [ ] TODO — Analyzer: layers (Inkscape), groups, stroke colors, unsupported-content report (text, raster, filters…)
- [ ] TODO — vpype integration: read → optimize (simplify/merge/sort/reloop options) → layout → pens → HP-GL (hp7475a profile)
- [ ] TODO — Fill modes: ignore / outline-only (hatch = Phase 2+)
- [ ] TODO — HP-GL writer + safety validator (allowlist, extents, pen range, size cap, safe suffix)
- [ ] TODO — Preview payload = post-processing geometry (optimized SVG path data)
- [ ] TODO — Fixtures (simple shapes → malicious → all paper sizes) + golden tests

## Phase 5 — Job runner & API

- [ ] TODO — SQLite models (jobs, settings, pens) + migrations
- [ ] TODO — Single hardware worker queue (serialized serial access)
- [ ] TODO — Buffer-safe streamer job state machine (QUEUED…DISCONNECTED)
- [ ] TODO — Pause/resume/cancel/replot/duplicate; history + retention
- [ ] TODO — REST API per spec §33 + WebSocket /api/ws/status
- [ ] TODO — Bind 127.0.0.1 default; LAN mode + auth off by default
- [ ] TODO — Tests: state transitions, concurrency (one writer), timeout→FAILED, disconnect→DISCONNECTED

## Phase 6 — Frontend

- [ ] TODO — React+TS+Vite app shell, pages: Plot, Manual Control, Jobs, Device, Pens, Settings, Diagnostics
- [ ] TODO — Connection wizard (5 steps per spec §8)
- [ ] TODO — Plot workspace: preview (page/hard-clip/geometry/pens/travel), pen mapping, optimization panel
- [ ] TODO — Start-confirmation modal + active-plot global visibility
- [ ] TODO — HP-GL inspector panel
- [ ] TODO — Frontend tests (upload, mapping, confirmation, progress, pause/resume/cancel, connection errors)

## Phase 7 — Fake-device E2E

- [ ] TODO — PTY end-to-end: connect → identify → plot square → completion
- [ ] TODO — E2E: pause/resume/cancel/timeout/disconnect paths
- [ ] TODO — Playwright smoke (if practical)

## Phase 8 — Hardware validation (USER-INITIATED ONLY)

- [ ] READY FOR USER HARDWARE TEST — procedure doc (docs/hardware-acceptance-test.md)
- [ ] READY FOR USER HARDWARE TEST — real identify + test plot on physical HP 7475A

## Phase 9 — Deployment & docs

- [ ] TODO — README (install, first-run, permissions/dialout, run, build)
- [ ] TODO — .env.example, scripts/dev.sh, scripts/build.sh
- [ ] TODO — systemd unit (deploy/systemd/) + docs
- [ ] TODO — docs/architecture.md, serial-troubleshooting.md, svg-support.md, hpgl-safety.md
- [ ] TODO — Final acceptance audit (spec §47) + push
