# HP 7475A Web — Frontend

React 18 + TypeScript (strict) + Vite SPA for the HP 7475A pen-plotter backend
(FastAPI on `127.0.0.1:8750`). Plain CSS dark theme — no UI framework.

## Run

```sh
npm install
npm run dev        # http://localhost:5173 — /api and /api/ws proxied to :8750
```

Backend must be running: `cd ../backend && uvicorn app.main:app --port 8750`.

If the Vite WS proxy misbehaves (rare HMR/proxy conflict), point the socket
straight at the backend:

```sh
VITE_WS_URL=ws://127.0.0.1:8750/api/ws/status npm run dev
```

## Test / Build

```sh
npm test              # vitest run (jsdom, threads pool)
npm run test:watch    # watch mode
npm run test:coverage # c8 coverage (lines/functions ≥ 80 enforced)
npm run build         # tsc --noEmit (strict) + vite build → dist/
npm run preview       # serve the production build
```

Note: the `threads` pool is pinned in `vite.config.ts` — the default `forks`
pool deadlocks on some WSL2 hosts.

## Structure

- `src/api/client.ts` — single typed fetch wrapper (`req<T>`); every call goes
  through the `api` object. Base URL override: `VITE_API_BASE`.
- `src/api/ws.ts` — `useStatusSocket()`: `/api/ws/status` with auto-reconnect
  (1→2→5 s backoff), last-message state, 50-entry event ring.
- `src/api/types.ts` — shared DTOs + `isJobEvent()` WS narrow guard.
- `src/state/app.tsx` — `AppProvider`: device status (4 s poll), paper table,
  toast stack, WS socket.
- `src/pages/` — `Plot` (upload → sanitize → analysis → pens → prepare →
  preview → confirm → live progress), `Manual` (jog/park/move-to, clamped),
  `Jobs` (history + HP-GL drawer + replot/delete), `Device` (5-step connect
  wizard, FTDI/dialout hints, status bits), `Diagnostics` (buffer/error poll,
  raw WS log), `Settings` (stream view + custom JSON editor).
- `src/components/` — `StatusBadge`, `PenMap`, `Modal`, `Progress`, `Toast`,
  `PagePreview` (client-side SVG re-sanitize before inline render).
- Pen mapping modes: **By Layer** (Inkscape layer names) or **By Color**
  (one row per `analysis.stroke_colors` hex — swatch shows the exact stroke
  color; useful for SVGs without Inkscape layers). Default is By Layer when
  the file has >1 layer, else By Color; files reporting no stroke colors fall
  back to layer mapping with a notice. Disabled rows are excluded from
  `pen_map` (don't plot). The create-job payload sends
  `pen_map_mode: "layers" | "colors"` alongside the keyed `pen_map`.
- `src/test/` — vitest + @testing-library; `fakews.ts` is the WS test double.

## Conventions

- All HTTP through `client.ts`; errors surface as `ApiError{status, detail}`
  via `apiErrorMessage()`.
- Pages handle: backend down (banner + Retry), empty states, 4xx/5xx detail,
  WS reconnect.
- TypeScript strict; no `any` in exported types.
