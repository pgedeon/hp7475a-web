# OpenClaw Build Specification: HP 7475A Web Plotter Controller

## Mission

Build a production-quality local web application that controls a **Hewlett-Packard HP 7475A six-pen plotter** connected to a Linux development machine through a **USB 2.0 → RS-232 DB9 FTDI adapter**.

The application must make the plotter usable from a browser without requiring terminal commands for normal operation. Its primary workflow is:

1. Open the web interface.
2. Detect/select the USB serial adapter.
3. Connect to and verify the HP 7475A.
4. Upload an SVG.
5. Preview exactly how it will fit on the physical page.
6. Assign SVG layers/colors to the six physical pens.
7. Optimize the vector paths.
8. Generate HP-GL specifically for the HP 7475A.
9. Plot it safely without overflowing the plotter's small input buffer.
10. Show useful progress/status and retain job history.

This is not a mock UI. Implement the complete working application, serial driver, SVG/HP-GL pipeline, job queue, tests, documentation, and deployment configuration.

---

# 1. Hardware Context

Target hardware:

- Plotter: **HP 7475A**
- Interface: **RS-232-C**
- Plotter connector: typically DB25 on the plotter
- Computer-side adapter: **USB 2.0 to RS-232 DB9, FTDI chipset**
- Host OS: Linux
- The USB adapter will normally appear as something like `/dev/ttyUSB0`, but **do not hard-code this path**.
- Prefer stable Linux paths under `/dev/serial/by-id/` when available.
- The HP 7475A has **six pen positions**.
- The application must support A4, A3, ANSI A/Letter, and ANSI B/Tabloid plotter configurations.

The HP documentation gives a common PC serial configuration of:

- 9600 baud
- 8 data bits
- no parity
- 1 stop bit

Treat **9600 8N1 as the initial default**, not an immutable assumption. The serial configuration must be editable because the physical DIP-switch configuration on vintage hardware may differ.

The app must not assume a particular handshaking method. Implement a proper serial transport abstraction supporting:

- HP buffer-space polling / software checking
- XON/XOFF software flow control
- RTS/CTS hardware flow control
- no-flow-control diagnostic mode with conservative chunking/delay

The preferred production mode should be whichever is proven reliable by the HP 7475A manual and a real-device connection test. Do not blindly blast an entire HP-GL file at the serial port.

---

# 2. Source Material You Must Consult Before Implementing Hardware Behavior

Do not guess HP-GL commands, escape sequences, buffer behavior, paper coordinates, or serial handshaking details.

Use these as authoritative/reference material:

## HP documentation

HP 7475A Operation and Interconnection Manual:

https://pearl-hifi.com/06_Lit_Archive/15_Mfrs_Publications/20_HP_Agilent/HP_7475A_Plotter/HP_7475A_Op_Interconnect.pdf

HP 7475A Interfacing and Programming Manual:

https://ia803104.us.archive.org/23/items/HP7475AInterfacingandProgrammingManual/HP7475AInterfacingandProgrammingManual.pdf

HP maximum plot areas/support information:

https://support.hp.com/us-en/document/bpp01377

HP error light information:

https://support.hp.com/us-en/document/bpp01374

## Serial library

pySerial:

https://pyserial.readthedocs.io/en/latest/pyserial_api.html

## Plotter-oriented SVG/HP-GL tooling

vpype:

https://github.com/abey79/vpype

vpype documentation:

https://vpype.readthedocs.io/en/latest/

vpype HP-GL cookbook:

https://vpype.readthedocs.io/en/latest/cookbook.html

vpype already includes an `hp7475a` device configuration. Prefer using and validating that implementation rather than inventing paper coordinate mappings from scratch.

Important: before depending on any particular command or behavior, verify it against the HP 7475A manual. Do not rely on another plotter's HP-GL/2 behavior.

---

# 3. Required Technology Stack

Use a maintainable Linux-native architecture.

## Backend

Preferred:

- Python 3.12+
- FastAPI
- Uvicorn
- pySerial
- vpype
- Pydantic
- SQLite for job/settings persistence
- SQLAlchemy or SQLModel
- WebSockets for live plot/job/device status

The backend owns the serial port. The browser must **never** communicate directly with `/dev/ttyUSB*`.

Only one backend process may own the physical serial device at a time.

## Frontend

Use:

- React
- TypeScript
- Vite
- a clean component system
- responsive desktop-first layout

A plotting application benefits from a real interactive frontend. Do not create a page of crude HTML buttons.

## Deployment

Primary supported deployment must be native Linux because direct access to USB serial hardware is simplest and most reliable there.

Provide:

- development run commands
- production build commands
- `.env.example`
- a systemd user/service example
- documented Linux permissions
- optional Docker support only as a secondary method

If Docker is supplied, document that the serial device must be explicitly passed through to the container.

---

# 4. Repository Structure

Use a structure approximately like this:

```text
hp7475a-web/
├── README.md
├── .env.example
├── .gitignore
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── serial/
│   │   │   │   ├── discovery.py
│   │   │   │   ├── connection.py
│   │   │   │   ├── transport.py
│   │   │   │   ├── flow_control.py
│   │   │   │   └── hp7475a.py
│   │   │   ├── svg/
│   │   │   │   ├── sanitizer.py
│   │   │   │   ├── analyzer.py
│   │   │   │   ├── converter.py
│   │   │   │   └── preview.py
│   │   │   ├── hpgl/
│   │   │   │   ├── validator.py
│   │   │   │   ├── analyzer.py
│   │   │   │   └── streamer.py
│   │   │   └── jobs/
│   │   │       ├── queue.py
│   │   │       ├── runner.py
│   │   │       └── estimates.py
│   │   └── db/
│   └── tests/
├── frontend/
│   ├── package.json
│   └── src/
│       ├── pages/
│       ├── components/
│       ├── hooks/
│       ├── api/
│       └── types/
├── fixtures/
│   ├── svg/
│   └── hpgl/
└── deploy/
    └── systemd/
```

Adjust the exact structure if necessary, but maintain clear separation between:

- hardware transport
- HP 7475A protocol logic
- vector conversion
- job management
- HTTP/WebSocket API
- UI

---

# 5. Linux Serial Device Discovery

Implement automatic serial-port discovery using `serial.tools.list_ports`.

For each candidate port show:

- device path
- `/dev/serial/by-id/...` path if available
- manufacturer
- product
- serial number
- USB VID
- USB PID
- description
- whether it appears to be FTDI
- whether the backend has permission to open it

Prefer a `/dev/serial/by-id/` identifier when persisting a selected device because `/dev/ttyUSB0` may change after reboot or reconnection.

The Settings page must have:

- Refresh devices
- Select device
- Connect
- Disconnect
- Test connection
- Auto-connect on application startup
- Remember selected device

Do not automatically send motion commands merely because a USB adapter appears.

---

# 6. Linux Permissions

Detect and explain permission problems.

Typical Linux distributions grant serial access through the `dialout` group. Document commands such as:

```bash
ls -l /dev/ttyUSB0
groups
```

and, when appropriate:

```bash
sudo usermod -aG dialout "$USER"
```

Explain that the user must log out/in after group changes.

Do not automatically modify groups or install udev rules with sudo without explicit user permission.

Optionally provide a documented udev rule for creating a stable alias if `/dev/serial/by-id/` is unavailable.

---

# 7. Serial Settings UI

Expose these settings:

- Baud rate
- Data bits
- Parity
- Stop bits
- Flow-control mode
- read timeout
- write timeout
- conservative send chunk size
- buffer reserve/safety margin
- retry timeout
- maximum retry count

Preset:

**HP 7475A / PC default**

- 9600 baud
- 8 data bits
- no parity
- 1 stop bit

Do not silently change physical plotter DIP switches, obviously. Show a small panel telling the user what settings the application expects the rear-panel switches to match.

---

# 8. Connection Wizard

Create a useful first-run wizard.

## Step 1 — Find serial adapter

List detected ports and highlight FTDI devices.

## Step 2 — Serial settings

Default to 9600 8N1.

## Step 3 — Communication test

Safely test bidirectional communication.

The HP manuals demonstrate using initialization followed by the plotter-identification query. Verify the exact command and response behavior from the manual before implementation.

The UI should display something like:

```text
Port opened
Sent identification query
Received: HP 7475A / 7475A
Bidirectional communication: OK
```

If no response is received, give specific diagnostics:

- wrong serial port
- baud mismatch
- parity mismatch
- incorrect DB9↔DB25 cable wiring
- null-modem vs straight-through cable issue
- missing handshake wires
- unsupported flow-control mode
- plotter not in LINE mode
- plotter off
- USB permissions problem

Do not claim the device is connected merely because `open()` succeeded.

## Step 4 — Flow control verification

Verify the selected buffer/handshake strategy before allowing long jobs.

## Step 5 — Optional physical test

Offer a **user-initiated** small test plot.

Never draw automatically during installation.

---

# 9. HP 7475A Driver

Create an explicit `HP7475ADevice` abstraction.

Responsibilities:

- serial connection lifecycle
- safe initialization
- identity query
- error query
- actual-position query
- pen selection
- pen up
- pen down
- move to absolute coordinates
- plotting velocity
- paper/device capability metadata
- flow-control setup
- output/input buffer handling
- status polling
- wait-until-finished behavior
- safe pen parking
- job abort behavior where the manual supports it

Keep raw escape sequences in one well-documented protocol module.

Do not scatter string literals such as `"\x1b..."` throughout the codebase.

Every implemented HP-GL or RS-232 escape command should have:

- a descriptive constant/function
- a source-manual reference in a code comment
- unit tests
- expected response parsing where applicable

---

# 10. Buffer-Safe Streaming Is Critical

The vintage HP 7475A has a small input buffer. Sending a large SVG-derived HP-GL job as one huge `serial.write()` is unacceptable.

Implement a robust streaming strategy.

## Preferred strategy: plotter buffer-space polling

The HP 7475A programming documentation describes an immediate escape-sequence query for available input-buffer space. Verify the exact sequence and response format from the manual.

Create logic approximately like:

1. Prepare HP-GL bytes.
2. Ask the plotter how much input-buffer space is available.
3. Reserve a safety margin.
4. Send only a chunk that safely fits.
5. Repeat until the job is completely transmitted.
6. Send a command/query that can be used to verify that all earlier plotting commands have executed.
7. Mark the job complete only after the device has actually drained/finished, not merely after the host has written all bytes.

Handle:

- lost query responses
- serial timeout
- partial write
- device disconnect
- malformed reply
- job cancellation
- pause/resume
- flow-control deadlock
- retry limits

## Alternative modes

Also support:

### XON/XOFF

Use pySerial software flow control when deliberately selected and verified.

### RTS/CTS

Use only when the cable wiring actually carries the required handshake lines.

### Diagnostic chunk/delay mode

Useful for troubleshooting, but not the preferred normal mode.

---

# 11. Plot Completion Detection

The UI needs meaningful states:

```text
QUEUED
PREPARING
READY
SENDING
PLOTTING
PAUSED
COMPLETING
COMPLETED
CANCELLED
FAILED
DISCONNECTED
```

Do not equate "all bytes were accepted by the Linux serial driver" with "the physical plot is finished."

Use an HP 7475A query whose response is processed only after prior queued plot commands execute, if supported as documented. Verify the precise behavior in the programming manual.

Expose at least three progress concepts:

- bytes transmitted
- estimated vector distance completed
- device completion state

The percentage may be an estimate. Label estimates as such.

---

# 12. Emergency Stop / Pause / Resume

Implement:

## Pause

- immediately stop feeding new HP-GL to the device
- allow already-buffered movement to finish
- change status to PAUSED
- resume at the exact next unsent chunk

## Cancel

- stop further transmission
- use the documented HP 7475A abort/clear mechanism if a safe immediate command exists
- otherwise clearly distinguish:
  - "Stop sending"
  - "Abort/reset device"

Do not invent an undocumented emergency-stop command.

## Physical emergency guidance

Display a small note that hardware already buffered may continue briefly after software pause/cancel.

---

# 13. SVG Upload

This is a core feature.

The main Plot page must support:

- drag-and-drop SVG
- file picker
- paste SVG text
- recent SVG jobs

Allowed initial formats:

- `.svg`
- `.hpgl`
- `.plt` when it contains compatible HP-GL

SVG files must never be executed as browser HTML.

---

# 14. SVG Security

Treat uploaded SVGs as untrusted XML.

Reject or neutralize:

- `<script>`
- event-handler attributes such as `onclick`
- external URLs/resources
- remote images
- remote stylesheets
- XML external entities
- `foreignObject`
- JavaScript URLs
- dangerous data URLs
- file URLs
- references that could cause server-side network access
- entity-expansion attacks

Set a configurable maximum file size.

Do not allow the SVG processing pipeline to fetch external resources.

Do not pass user-supplied strings to a shell.

If a vpype CLI subprocess is used, invoke it with an argument array and `shell=False`. Prefer a direct Python library integration where practical.

---

# 15. Supported SVG Geometry

At minimum correctly support common vector geometry:

- `<path>`
- `<line>`
- `<polyline>`
- `<polygon>`
- `<rect>`
- rounded rectangles where converter support is reliable
- `<circle>`
- `<ellipse>`
- nested `<g>` transforms
- `transform`
- `viewBox`
- width/height
- stroke colors
- SVG/Inkscape layers where possible

Use vpype's SVG reader/geometry engine rather than writing a fragile SVG parser from scratch.

The application must detect unsupported/non-plottable content and tell the user instead of silently dropping it.

Examples:

- SVG text that is not converted to outlines
- embedded raster `<image>`
- filters
- gradients
- masks
- patterns
- unsupported clipping behavior

Display a warning such as:

```text
This SVG contains 3 text elements and 1 raster image.
These elements cannot be plotted as vector lines in the current pipeline.
Convert text to paths or enable the relevant conversion option.
```

---

# 16. SVG Text

Implement one of these approaches, in this order of preference:

1. reliable text-to-path conversion using explicitly available fonts
2. otherwise require text to be converted to paths before plotting

Never silently omit text.

If text-to-path is implemented:

- enumerate usable fonts
- do not fetch fonts from the internet
- preserve font-size and transforms
- make the exact font used visible in job metadata

---

# 17. Fills

A pen plotter draws paths; it does not reproduce SVG raster-style fills automatically.

Provide explicit fill behavior:

- Ignore fills
- Outline only
- Hatch fill

Hatching should be a controllable feature, not an accidental side effect.

Hatch controls:

- angle
- spacing
- cross-hatch on/off
- second angle
- inset/edge behavior

If robust hatching is too large for the first implementation, make **Ignore fills** and **Outline only** complete and reliable first, then implement hatching as Phase 2.

---

# 18. SVG-to-HP-GL Pipeline

Use vpype wherever it provides reliable functionality.

A typical pipeline should conceptually support:

```text
read
→ normalize/flatten geometry
→ filter invalid geometry
→ simplify
→ merge compatible lines
→ re-loop closed paths
→ sort paths
→ layout to selected page
→ map layers to pens
→ write HP-GL using hp7475a device config
```

vpype documentation gives a real-world pattern similar to:

```bash
vpype read input.svg linesimplify reloop linemerge linesort layout a4 write --device hp7475a output.hpgl
```

Do not simply run this exact command for every job. Build a controlled conversion service with user-selected options.

Use the built-in `hp7475a` device configuration and verify it against the HP documentation.

The vpype device configuration already contains:

- HP 7475A plotter-unit scaling
- six-pen support
- paper definitions
- origin/orientation metadata
- plotting ranges

Avoid duplicating this data in multiple places. Build a single normalized paper/device model and tests around it.

---

# 19. Plot Optimization

Expose plotter-oriented optimization controls.

Defaults should favor a good balance between fidelity and plot time.

Options:

- line simplify
- line merge
- path sorting / minimize pen-up travel
- re-loop closed paths
- remove paths below minimum length
- flatten curves with a user-configurable tolerance where required
- optional multipass
- preserve source order toggle
- optimize each pen/layer separately

Before and after optimization show:

- number of paths
- pen-down distance
- pen-up travel distance
- estimated plot time
- estimated reduction in pen-up travel
- generated HP-GL byte size

If calculating an exact time is not possible, explicitly label it "estimated."

---

# 20. Six-Pen Layer Mapping

The HP 7475A has six pen positions. Make this a first-class UI feature.

Create pen slots:

```text
Pen 1
Pen 2
Pen 3
Pen 4
Pen 5
Pen 6
```

For each physical pen store optional metadata:

- display name
- actual ink color
- pen type
- tip size
- notes
- active/empty

Example:

```text
Pen 1 — Black 0.3 mm technical
Pen 2 — Red 0.3 mm
Pen 3 — Blue 0.3 mm
Pen 4 — Green 0.3 mm
Pen 5 — Orange 0.3 mm
Pen 6 — Purple 0.3 mm
```

The SVG analyzer should find usable separation data:

- Inkscape layers
- SVG groups
- stroke colors

Allow the user to map each detected vector layer/color to:

- Pen 1–6
- disabled / do not plot

Allow manual layer ordering.

Do not infer that the visual SVG color perfectly matches the physical pen color. It is only a mapping aid.

---

# 21. Plot Setup Screen

The main screen should be a visual plot preparation workspace.

Layout suggestion:

```text
┌─────────────────────────────────────────────────────────────┐
│ HP 7475A    ● Connected     /dev/serial/by-id/...          │
├───────────────────────┬─────────────────────────────────────┤
│ File / settings       │                                     │
│                       │          PAGE PREVIEW               │
│ A4                    │                                     │
│ Landscape             │     SVG + margins + hard clip       │
│ Fit to page           │                                     │
│ Margin 10 mm          │     optional pen-up travel lines    │
│                       │                                     │
│ Pen mappings          │                                     │
│ 1 Black ← layer 1     │                                     │
│ 2 Red   ← layer 2     │                                     │
│ ...                   │                                     │
├───────────────────────┴─────────────────────────────────────┤
│ Paths 1,462 | Draw 84.2 m | Travel 21.4 m | ~18m 34s       │
│ [Optimize] [Generate HPGL]              [PLOT]              │
└─────────────────────────────────────────────────────────────┘
```

---

# 22. Physical Page Setup

Support:

- A4
- A3
- ANSI A / Letter
- ANSI B / Tabloid

Controls:

- page size
- orientation
- fit to page
- actual size / 100%
- custom scale %
- X position
- Y position
- center horizontally
- center vertically
- margin
- rotate 90°
- rotate 180°
- rotate 270°
- flip horizontal
- flip vertical

The preview must show:

- physical paper bounds
- actual HP 7475A plottable/hard-clip bounds
- geometry bounds
- origin
- margin
- out-of-bounds geometry highlighted as an error

Do not allow a job to start if required geometry exceeds safe hard-clip limits unless the user deliberately changes scaling/position.

---

# 23. Preview

Provide an interactive SVG preview with:

- zoom
- pan
- fit view
- physical page
- plotter hard-clip area
- per-pen colors
- hide/show individual pen layers
- geometry bounds
- optional pen-up travel visualization
- start point
- end point
- plot direction indicators as an optional overlay

The preview should be based on the **post-processed geometry that will actually generate HP-GL**, not merely the original uploaded SVG.

This is important: what the user sees should match what the plotter will do.

---

# 24. HP-GL Inspector

Create an advanced panel for generated HP-GL.

Features:

- preview generated HP-GL text
- download `.hpgl`
- copy HP-GL
- byte count
- command count
- pen selections used
- coordinate extents
- detect unsupported/suspicious commands
- validate semicolon termination where applicable
- show initialization/finalization commands
- show warnings

Also allow uploading an existing HP-GL file.

For uploaded HP-GL:

- parse/validate before transmitting
- reject or require explicit confirmation for commands outside the known HP 7475A safe subset
- show bounding coordinates where possible
- show pens used
- show approximate geometry preview where practical

Never blindly transmit arbitrary uploaded bytes to a serial device.

---

# 25. Manual Control Page

Create a manual-control screen for setup and diagnostics.

Controls:

## Connection

- Connect
- Disconnect
- Identify
- Query error
- Query actual position
- Clear displayed diagnostic log

## Pen

- Select Pen 1
- Select Pen 2
- Select Pen 3
- Select Pen 4
- Select Pen 5
- Select Pen 6
- Return/store pen
- Pen up
- Pen down

## Jog

Directional controls:

```text
        ↑
    ↖   ↑   ↗
← ←     •     → →
    ↙   ↓   ↘
        ↓
```

Selectable jog distances:

- 0.1 mm
- 1 mm
- 5 mm
- 10 mm

Never permit a jog beyond known hard-clip boundaries.

## Position

- current X/Y
- go to X/Y
- move to page center
- move to safe park position
- move to P1/P2 only when verified safe

## Test drawings

Provide user-triggered tests:

- tiny square
- triangle
- circle
- horizontal/vertical line
- six-pen swatch/test pattern
- full-page safe-boundary test with generous margin

All tests must show a confirmation preview before moving hardware.

---

# 26. Plotting Velocity

The HP 7475A supports velocity control. Verify the valid range and exact behavior in the manual before enforcing values.

Expose:

- Default
- Slow
- Medium
- Fast
- Custom

Store velocity per job.

Do not expose pen-force controls as if they work unless verified for the HP 7475A. If an HP-GL command is ignored by this model, omit it from the normal UI.

---

# 27. Job Queue

Implement a real server-side queue.

A single physical plotter can execute one job at a time.

Job fields should include:

- UUID
- filename
- created time
- started time
- completed time
- state
- source SVG path/hash
- generated optimized SVG path
- generated HP-GL path
- page size
- orientation
- transform settings
- pen mappings
- optimization settings
- serial settings snapshot
- plot velocity
- geometry statistics
- bytes
- error/warning messages

Operations:

- enqueue
- start
- pause
- resume
- cancel
- duplicate job
- re-plot
- delete history entry
- download original SVG
- download processed SVG
- download HP-GL

Do not auto-start a queued physical plot unless the user has explicitly enabled an auto-run queue option.

---

# 28. Job Progress Screen

While plotting show:

- filename
- current pen
- current layer
- elapsed time
- estimated remaining time
- transmitted bytes
- total bytes
- estimated completed path distance
- current plotter position when available
- current buffer availability when available
- pause
- resume
- cancel
- disconnect warning

Keep progress updates efficient. Do not flood the serial port with status queries so aggressively that they interfere with plotting.

---

# 29. Reconnection Behavior

Handle USB disconnects gracefully.

If the FTDI adapter disappears:

- stop the sender
- mark job `DISCONNECTED`, not `COMPLETED`
- do not silently continue against a new `/dev/ttyUSB0`
- display reconnect instructions
- attempt to rediscover the same stable USB serial ID if configured
- require safe user confirmation before resuming an uncertain partially plotted job

A plot that loses connection halfway through cannot always be safely resumed from an arbitrary HP-GL byte offset. Be conservative.

---

# 30. Diagnostics

Create a diagnostics page.

Display:

- selected serial device
- stable by-id path
- VID/PID
- serial number
- baud/data/parity/stop
- flow-control mode
- port open status
- last device response
- bytes TX
- bytes RX
- buffer queries
- serial timeout count
- retries
- parser errors
- last HP-GL error
- recent driver events

Provide an optional verbose protocol log.

Do not store massive raw logs forever. Rotate/cap them.

Add a button to download a diagnostics bundle containing:

- application version
- sanitized settings
- recent logs
- selected serial metadata
- recent error information

Do not include secrets.

---

# 31. Application Logging

Use structured logging.

Important events:

- server start
- serial discovery
- connect/disconnect
- serial settings
- successful device identification
- job queued
- conversion started/completed
- plot started
- pause/resume
- cancellation
- transport timeout
- retry
- plotter error
- disconnect
- completion

Never log uploaded SVG contents unless verbose debugging is explicitly enabled.

---

# 32. Settings

Persist:

## Device

- preferred serial device
- auto-connect
- serial settings
- flow control

## Plotter

- default paper
- default orientation
- default margin
- default velocity
- safe park position

## Pens

- six pen profiles

## SVG

- default optimization settings
- curve tolerance
- minimum segment length
- default fill behavior

## UI

- default preview overlays

Provide "Restore safe defaults."

---

# 33. API Design

Use clear REST endpoints and a WebSocket.

Illustrative API:

```text
GET    /api/health

GET    /api/serial/ports
GET    /api/device
POST   /api/device/connect
POST   /api/device/disconnect
POST   /api/device/identify
GET    /api/device/status
GET    /api/device/error

POST   /api/device/pen/{number}
POST   /api/device/pen-up
POST   /api/device/pen-down
POST   /api/device/move
POST   /api/device/park

POST   /api/files/svg
POST   /api/files/hpgl
GET    /api/files/{id}/analysis

POST   /api/jobs
GET    /api/jobs
GET    /api/jobs/{id}
POST   /api/jobs/{id}/prepare
POST   /api/jobs/{id}/start
POST   /api/jobs/{id}/pause
POST   /api/jobs/{id}/resume
POST   /api/jobs/{id}/cancel
DELETE /api/jobs/{id}

GET    /api/settings
PUT    /api/settings

WS     /api/ws/status
```

Adjust as needed.

All physical-device mutation endpoints must be serialized through the single hardware worker.

Do not let two HTTP requests write to the serial port concurrently.

---

# 34. Serial Concurrency

This is important.

Implement one serialized hardware execution path.

Good pattern:

```text
HTTP/UI
   ↓
command queue
   ↓
single HP7475A worker
   ↓
serial transport
   ↓
plotter
```

Use locks/queues so that:

- manual jog cannot interleave bytes with a plot job
- a status query does not corrupt a pending response parser
- two jobs cannot stream simultaneously
- a device disconnect cannot race a write

---

# 35. Fake Plotter Emulator

Build a software fake HP 7475A for development and automated testing.

Use a Linux pseudo-terminal pair (`pty`) so the same serial driver can connect to a fake `/dev/pts/...`.

The emulator should minimally simulate:

- identification response
- buffer-space query
- actual-position response
- error response
- basic HP-GL parsing
- pen selection
- PU/PD movement
- finite input buffer
- delayed execution
- XON/XOFF behavior if implemented
- intentional timeout mode
- disconnect mode
- malformed response mode

This allows OpenClaw to test the application thoroughly without repeatedly moving the real plotter.

---

# 36. Tests

## Unit tests

Cover:

- serial port discovery
- stable device selection
- serial setting validation
- response parsing
- buffer-space calculation
- chunk sizing
- partial serial writes
- timeouts
- retries
- disconnect
- HP-GL validation
- pen mapping
- paper transforms
- geometry bounds
- out-of-range detection
- SVG sanitization
- unsupported SVG reporting
- job-state transitions

## SVG fixtures

Include:

- simple path
- rectangle
- circle
- ellipse
- polyline
- nested transforms
- viewBox scaling
- multiple colors
- six layers
- text
- embedded raster image
- malicious script
- external reference
- huge coordinates
- A4 portrait
- A4 landscape
- A3
- Letter
- Tabloid

## Golden HP-GL tests

For known SVG fixtures, generate HP-GL and validate:

- expected device
- expected paper
- expected pen selection
- coordinate bounds
- initialization
- pen-up final state
- no unsupported HP-GL/2-only instructions
- deterministic output where practical

## Integration tests

Use the fake serial plotter to prove:

- identify works
- a job streams without buffer overflow
- pause works
- resume works
- cancel works
- timeout becomes FAILED
- USB disconnect becomes DISCONNECTED
- completion waits for actual device completion

## Frontend tests

Test:

- upload flow
- page settings
- pen mapping
- warnings
- start confirmation
- active progress
- pause/resume/cancel
- connection errors

Use Playwright for a minimal end-to-end suite if practical.

---

# 37. Hardware Acceptance Test

Once fake-device tests pass, create a user-run hardware test procedure.

Do not automatically run physical tests.

Procedure:

1. Confirm paper loaded.
2. Confirm a working pen is installed in Pen 1.
3. Confirm plotter is online/LINE.
4. Confirm rear-panel serial settings.
5. Select FTDI device.
6. Open serial connection.
7. Identify plotter.
8. Query error state.
9. Query position.
10. Generate a small safe square.
11. Preview it.
12. Ask for user confirmation.
13. Plot it.
14. Wait for physical completion.
15. Return pen safely.
16. Mark hardware test passed.

Only after this should the documentation recommend attempting a large SVG.

---

# 38. Start Confirmation

Before every physical plot, show a compact confirmation:

```text
HP 7475A Plot Job

File: geometric-study.svg
Paper: A4
Orientation: Landscape
Scale: 93.2%
Pens: 1, 2, 4
Paths: 1,284
Estimated draw distance: 42.8 m
Estimated time: 11m 20s

No geometry exceeds the HP 7475A hard-clip area.

[Cancel] [Start Plot]
```

If warnings exist, make them visible.

Examples:

- missing physical pen assignment
- source contained ignored fills
- source contained omitted text
- geometry very dense
- expected job longer than 1 hour
- selected pen profile is marked empty

---

# 39. HP-GL Safety Validator

Before sending generated or uploaded HP-GL:

- tokenize/parse it
- maintain an allowlist of commands confirmed supported by HP 7475A
- calculate coordinates/extents when possible
- detect pen numbers outside 0–6
- reject unreasonable numeric values
- reject embedded unexpected control bytes
- reject HP-GL/2/PCL escape sequences not expected by this device
- enforce maximum job size
- verify a pen-up/final-safe-state suffix

Generated HP-GL should be trusted more than uploaded raw HP-GL, but validate both.

Do not expose a browser endpoint that sends arbitrary strings directly to the serial port.

A raw-command console may exist only behind an explicitly enabled developer/diagnostic setting and must display a hardware-risk warning.

---

# 40. Local Security

Default bind address:

```text
127.0.0.1
```

Do not expose physical plotter controls to the LAN by default.

If the user enables LAN mode:

- authentication is required
- use strong generated credentials or passkey/session auth
- protect state-changing endpoints
- use origin checks
- configure CORS narrowly
- document HTTPS/reverse proxy options
- never expose a raw serial command endpoint publicly

The app controls moving hardware, so network exposure is not equivalent to hosting a read-only dashboard.

---

# 41. Storage

Use a local app data directory, for example:

```text
~/.local/share/hp7475a-web/
```

Store:

```text
database.sqlite
uploads/
processed/
hpgl/
logs/
```

Use unique IDs and hashes; never trust the client-provided filename as a filesystem path.

Clean up old temporary files.

Provide configurable retention:

- keep all job history
- or prune source/processed files after N days

---

# 42. UI Pages

Implement these main pages:

## Plot

Primary upload → configure → preview → plot workflow.

## Manual Control

Pen/jog/setup tools.

## Jobs

Current queue and history.

## Device

Serial adapter and HP 7475A connection state.

## Pens

Configure six physical pen profiles.

## Settings

Defaults and application options.

## Diagnostics

Hardware/protocol troubleshooting.

---

# 43. UI Quality

The app should look like a modern instrument/control application.

Requirements:

- desktop-first
- clear connected/disconnected state
- large obvious Plot button
- dangerous actions visually differentiated
- no ambiguous icon-only controls for critical hardware actions
- keyboard accessible
- usable at 1280×720
- good at 1920×1080
- no modal spam
- progress remains visible while a plot is active
- prevent browser double-click from starting the same plot twice

The current active plot should be visible globally in the UI.

---

# 44. Useful Extra Features

After the core requirements are complete and tested, add these where feasible.

## A. Six-pen test sheet

Generate a small chart that:

- selects each pen
- draws a line
- labels it with pen number using HP-GL text only if verified
- returns pens safely

## B. Plot statistics

Per pen:

- path count
- pen-down distance
- pen-up travel
- estimated time

## C. Replot one pen only

Useful when one color failed.

Allow duplicating a completed job and selecting only specific pen layers.

## D. Multipass

Allow a selected pen/layer to run multiple passes.

## E. Hatch fills

As described above.

## F. Simple plotter-native generators

Generate:

- grid
- circles
- calibration square
- line test
- pen swatches
- page boundary

## G. Optimized SVG download

Let the user download the exact vector geometry after optimization.

---

# 45. Things NOT to Do

Do not:

- send entire large files without flow control
- assume `/dev/ttyUSB0`
- assume every DB9↔DB25 cable is wired the same way
- assume RTS/CTS exists physically
- assume a serial port opening proves a plotter is connected
- use HP-GL/2 commands just because modern printers support them
- silently omit unsupported SVG elements
- silently crop geometry
- execute scripts in uploaded SVG
- allow external SVG resource fetching
- use `shell=True` with uploaded filenames/options
- let multiple requests write concurrently to serial
- mark a job complete before the physical device finishes
- automatically move the plotter on server startup
- automatically start a physical test
- automatically expose the app to the network
- hide hardware communication failures behind generic HTTP 500 errors
- implement a fake Plot button that only downloads HP-GL

---

# 46. Development Phases

Work through these phases sequentially and do not claim completion until the acceptance checks pass.

## Phase 1 — Research and environment

- Read HP manuals.
- Verify HP 7475A command subset.
- Verify serial configuration/handshake methods.
- Verify exact buffer-space query.
- Verify completion-query behavior.
- Inspect Linux serial devices.
- Verify FTDI adapter is visible.
- Document findings in `docs/hardware-notes.md`.

## Phase 2 — Serial transport

- Device discovery.
- Port open/close.
- Settings.
- Identification.
- Response parser.
- Flow control.
- Fake plotter emulator.
- Tests.

## Phase 3 — HP 7475A driver

- Basic commands.
- Paper/device metadata.
- Pen controls.
- motion safeguards.
- errors.
- position.
- completion detection.
- tests.

## Phase 4 — SVG pipeline

- secure upload
- analysis
- vpype integration
- layers/colors
- optimization
- paper layout
- bounds checks
- HP-GL export
- tests

## Phase 5 — Job runner

- database
- queue
- buffer-safe streamer
- state machine
- pause/resume/cancel
- history
- WebSocket status
- tests

## Phase 6 — Frontend

- Plot screen
- preview
- pen mapping
- Device screen
- Manual Control
- Jobs
- Settings
- Diagnostics

## Phase 7 — Fake-device E2E

Run automated end-to-end jobs using PTY fake plotter.

Fix all failures.

## Phase 8 — Physical hardware test

Prepare, but do not initiate without user action.

## Phase 9 — Deployment

- README
- systemd
- environment config
- permissions
- production frontend build
- startup/restart behavior

---

# 47. Acceptance Criteria

The project is complete only when all of these are true.

## Device

- [ ] Linux app enumerates serial devices.
- [ ] FTDI adapter is recognizable.
- [ ] Stable `/dev/serial/by-id/` path is preferred.
- [ ] User can configure 9600 8N1 or alternatives.
- [ ] User can connect/disconnect.
- [ ] Connection test performs a real HP 7475A query.
- [ ] UI shows actual device response.
- [ ] Permission failures have useful diagnostics.

## Transport

- [ ] Large HP-GL cannot overflow the plotter buffer under the preferred mode.
- [ ] Short writes are handled.
- [ ] Timeouts are handled.
- [ ] Retries are bounded.
- [ ] Disconnects are handled.
- [ ] One writer owns serial access.
- [ ] Job completion means the device actually completed queued work.

## SVG

- [ ] SVG can be uploaded from the browser.
- [ ] Common SVG shapes are supported.
- [ ] SVG transforms are honored.
- [ ] Unsupported content is reported.
- [ ] SVG is sanitized.
- [ ] No remote resource fetching occurs.
- [ ] Geometry is converted to HP-GL using an HP 7475A-specific profile.
- [ ] A4 works.
- [ ] A3 works.
- [ ] Letter/ANSI A works.
- [ ] Tabloid/ANSI B works.

## Preview

- [ ] Page boundary is visible.
- [ ] Hard-clip/plottable area is visible.
- [ ] Post-processed geometry is displayed.
- [ ] Out-of-bounds geometry is blocked or explicitly resolved.
- [ ] Layers/pens can be toggled.
- [ ] Pen-up travel can be visualized.

## Six pens

- [ ] UI supports all six physical pen slots.
- [ ] SVG layers/colors can map to pens 1–6.
- [ ] Layers can be disabled.
- [ ] Pen order can be controlled.
- [ ] Pen profiles persist.

## Plot jobs

- [ ] Jobs have persistent history.
- [ ] Generated HP-GL can be downloaded.
- [ ] Optimized SVG can be downloaded.
- [ ] User must confirm before physical plotting.
- [ ] Progress is displayed.
- [ ] Pause works.
- [ ] Resume works.
- [ ] Cancel works.
- [ ] Failed/disconnected jobs are not marked complete.
- [ ] Replot works.

## Manual controls

- [ ] Pen selection.
- [ ] Pen up/down.
- [ ] safe jogging.
- [ ] actual position.
- [ ] device error query.
- [ ] small test shapes.
- [ ] boundary checks prevent unsafe jogs.

## Tests

- [ ] Unit tests pass.
- [ ] SVG security tests pass.
- [ ] HP-GL validation tests pass.
- [ ] fake plotter PTY tests pass.
- [ ] frontend critical-flow tests pass.

## Documentation

- [ ] Linux installation is documented.
- [ ] `dialout`/permissions are documented.
- [ ] serial settings are documented.
- [ ] cable/handshake troubleshooting is documented.
- [ ] first hardware plot procedure is documented.
- [ ] systemd deployment is documented.

---

# 48. Definition of Done for OpenClaw

Do not stop after creating scaffolding or a plan.

For each phase:

1. Implement it.
2. Run tests.
3. Inspect the results.
4. Fix failures.
5. Continue.

Maintain a `TASKS.md` file with:

```text
TODO
IN PROGRESS
BLOCKED
DONE
```

Never move a task to DONE merely because code was generated. A task is DONE only when it has been exercised/tested.

If hardware is unavailable for a test, use the fake PTY plotter and mark the real-device validation separately as:

```text
READY FOR USER HARDWARE TEST
```

Do not fabricate a passing physical hardware test.

---

# 49. Initial Deliverables

Create all of these:

```text
README.md
TASKS.md
docs/architecture.md
docs/hardware-notes.md
docs/serial-troubleshooting.md
docs/svg-support.md
docs/hpgl-safety.md
backend/
frontend/
fixtures/
deploy/systemd/
```

The README must include a short first-run path approximately like:

```bash
git clone ...
cd hp7475a-web

# backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e .

# frontend
cd ../frontend
npm install
npm run build

# run
cd ..
./scripts/dev.sh
```

Adjust this to the actual implementation.

---

# 50. Final OpenClaw Reporting

At the end, report:

## Completed

List features actually implemented.

## Tests

Show exact commands run and pass/fail counts.

## Hardware detection

Show the serial devices found, without inventing results.

## Remaining physical validation

List only things requiring the real HP 7475A.

## Run instructions

Give exact commands to launch the app.

## Browser URL

Give the local URL, typically:

```text
http://127.0.0.1:8000
```

or whichever port the implementation actually uses.

## First real plot

Give the shortest safe sequence to:

1. connect
2. identify
3. upload a simple SVG
4. preview
5. confirm
6. plot

---

# 51. Priority Order

When tradeoffs are required, use this priority:

1. Hardware safety
2. Reliable serial communication
3. Correct HP 7475A behavior
4. Buffer-safe long plots
5. Accurate physical layout
6. SVG fidelity
7. Six-pen workflow
8. Clear UI
9. Plot optimization
10. Convenience features

A beautiful interface that can corrupt a plot because it overflows the serial buffer is not acceptable.

Build the reliable hardware layer first, then the complete web workflow on top of it.
