# HP 7475A Hardware & Protocol Notes

> Status: **VERIFIED FROM MANUALS** (2026-08-18). Sources:
> - [HP 7475A Operation and Interconnection Manual](https://pearl-hifi.com/06_Lit_Archive/15_Mfrs_Publications/20_HP_Agilent/HP_7475A_Plotter/HP_7475A_Op_Interconnect.pdf) ("Op. Manual")
> - [HP 7475A Interfacing and Programming Manual](https://ia803104.us.archive.org/23/items/HP7475AInterfacingandProgrammingManual/HP_7475AInterfacingandProgrammingManual.pdf) ("Prog. Manual")
> - Cross-checked against vpype 1.15 built-in `hp7475a` device config (`vpype_config.toml`).

Everything below was extracted from the manuals before implementation. Do not
change protocol behavior without re-checking these sources.

---

## 1. Interface & default serial configuration

- RS-232-C (DB25 on plotter; DB9 on the USB adapter side requires a suitable
  DB9↔DB25 cable — see §8).
- Common PC configuration documented by HP: **9600 baud, 8 data bits, no
  parity, 1 stop bit** (Op. Manual). The plotter's rear-panel DIP switches must
  match whatever the host uses — they are the source of truth, the app only
  mirrors them.
- Input buffer: **1024 bytes** (Prog. Manual §10-14).

## 2. Plotter units & paper sizes

- Plotter unit: **0.02488 mm** (1/40.2 mm ≈ 0.00098 in). Verified identical in
  vpype `hp7475a.plotter_unit_length`.
- Six pens (carousel). Pen select `SP n` with n = 0..6 (0 = pen away/stall).
- Hard-clip limits (plotter units), Prog. Manual §7-2, identical in vpype:

| Paper  | X range     | Y range    | Paper size      | Note |
|--------|-------------|------------|-----------------|------|
| A4     | 0 – 11040   | 0 – 7721   | 297×210 mm      | Metric DIP mode |
| A3     | 0 – 16158   | 0 – 11040  | 420×297 mm      | Metric DIP mode |
| ANSI A | 0 – 10365   | 0 – 7962   | 11×8.5 in       | Imperial DIP mode |
| ANSI B | 0 – 16640   | 0 – 10365  | 17×11 in        | Imperial DIP mode |

- Coordinates are absolute plotter units with origin at hard-clip lower-left,
  Y axis up (vpype `y_axis_up: true`).

## 3. Identification & queries (HP-GL output instructions)

All output instructions respond over RS-232-C **as they are parsed from the
plotter's input buffer** (Prog. Manual §10 / Ch.7 notes) — i.e. **in sequence
with surrounding plotting commands**. This is the basis for completion
detection (§5).

| Instruction | Response | Notes |
|-------------|----------|-------|
| `OI;` | `7475A<CR>` | Identification string is literally `7475A` + terminator |
| `OA;` | `X,Y,P<CR>` | Actual position + pen status (0=up, 1=down), plotter units |
| `OC;` | `X,Y,P<CR>` | Commanded position (last valid motion command) |
| `OE;` | `n<CR>` | HP-GL error number 0–8 (0 = no error); clears ERROR light |
| `OO;` | `0,1,0,0,1,0,0,0<CR>` | Options (arcs+circles, pen select) |
| `OS;` | `status<CR>` | Decimal status byte, see §5 |

Output terminator: **CR** by default (RS-232-C), settable via `ESC .M`.

## 4. RS-232-C device control instructions (escape sequences)

Syntax: `ESC . <letter> [params] :` — note the **trailing colon** terminator
for parameterized forms (Prog. Manual Ch.10).

| Sequence | Name | Purpose |
|----------|------|---------|
| `ESC .B:` | Output buffer space | Response: decimal **0–1024** = free bytes for graphic instructions. **Core of the software-checking handshake.** |
| `ESC .E:` | Output extended error | RS-232 I/O error number 0 or 10–16 (16 = **input buffer overflow**); clears ERROR light if no HP-GL errors |
| `ESC .@:` | Configure buffer/handshake | `ESC .@:` default restores 1024-byte buffer, hardwire handshake enabled, monitor off. Second param bits: bit0=hardwire(DTR) handshake enable, bit2=monitor mode type, bit3=monitor enable, bit4=block mode |
| `ESC .H p1;p2;p3:` | Handshake mode 1 | Block size (0–9999, ≥1024→1024), enquiry char, ack string |
| `ESC .I p1;p2;p3:` | Handshake mode 2 | Same params; also Xoff threshold / Xon trigger for XON/XOFF |
| `ESC .M ...` | Output format | Turnaround delay, output trigger char, echo terminate, output terminator, output initiator |
| `ESC .N ...` | Intercharacter delay / immediate response string | |

Defaults of interest: `ESC .I:` (or `.H:`) disables XON/XOFF and ENQ/ACK;
block size 80; **if the computer sends ENQ, the plotter answers ACK regardless
of buffer space** ("dummy handshake" — NOT overflow-safe; never rely on it).

## 5. Status byte (OS) & completion detection

Status byte bits (Prog. Manual §7-7):

| Bit | Value | Meaning |
|-----|-------|---------|
| 0   | 1     | Pen down |
| 1   | 2     | P1/P2 changed |
| 2   | 4     | Digitized point available |
| 3   | 8     | Initialized (power-up; cleared by reading OS) |
| 4   | 16    | Ready for data (pinch wheels down) |
| 5   | 32    | Error (cleared by OE output / IN) |
| 6   | 64    | RSV (always 0 for OS) |
| 7   | 128   | not used |

Power-up status = 24 (8+16). `Ready for data` bit **set** = plotting may
continue; cleared while the plotter is busy with pen carriage motion in
progress (i.e. wheels up / not ready). Completion detection strategy:

1. Stream the job in buffer-safe chunks (§6).
2. Append `OA;` after the final `PU` park command — because output
   instructions are answered **in parse order**, the reply only arrives after
   all previously buffered plotting has executed.
3. Poll `OS;` (bounded rate, e.g. ≤1 Hz) during long plots to surface
   `Ready`/`Error` bits without flooding the port.

Both are documented behaviors; the OA-queued technique is the primary
completion gate, OS polling is a UX nicety.

## 6. Buffer-safe streaming (software-checking handshake) — preferred mode

Per Prog. Manual §10-20..10-29:

1. Query `ESC .B:` → free bytes N (0–1024).
2. Send at most `min(N − safety_margin, chunk)` HP-GL bytes (margin default
   32; never exceed N).
3. Re-query; repeat. If N == 0, wait (bounded retry) and re-query.
4. **Always end HP-GL chunks on instruction boundaries** (after `;`), so a
   partially-parsed instruction can never be split across a re-query.

Failure handling: response timeout → bounded retries (default 3) → job FAILED
with diagnostic; malformed reply → same. `ESC .E:` value 16 at any point →
buffer overflow occurred → abort + user-facing error.

### Alternative modes

- **XON/XOFF**: plotter sends DC3/DC1 based on Xoff threshold; configure via
  `ESC .I` (enquiry char must be NUL for XON/XOFF mode). Host side uses
  pySerial `xonxoff=True`. Still keep chunked writes; pySerial/OS buffers make
  this best-effort on USB adapters.
- **Hardwire (DTR)**: pin 20 acts as "buffer space available" flag when
  enabled (`ESC .@` bit0). Requires those lines to actually be wired through
  the DB9↔DB25 cable — cannot be assumed.
- **Diagnostic chunk/delay**: fixed chunk (e.g. 64 B) + inter-chunk delay
  (e.g. 50 ms). For troubleshooting only.

## 7. Velocity

`VS v` — pen velocity; range 0.38–38.1 cm/s in **0.38 cm/s increments**
(rounded by plotter); `VS;` alone = default 38.1 cm/s. Use for slowing down on
transparency film etc. (Prog. Manual §3-3). No pen-force control is exposed by
this model — the UI must not pretend otherwise.

## 8. Cable / wiring

- Plotter side: DB25 (female per Op. Manual). Adapter side: DB9.
- A **straight-through** DB9↔DB25 "modem" cable is typically correct for
  DTE↔DCE, but vintage setups vary; the Op. Manual's interconnect chapter
  shows the required lines: TD, RD, DTR (pin 20), SG at minimum; RTS/CTS only
  if used.
- The app cannot detect wiring; it can only diagnose symptoms (no response,
  framing errors via `ESC .E` = 15).

## 9. Init / park / abort semantics

- `IN;` — initialize: clears error state, P1/P2, scaling; **does not move the
  pen**.
- Pen park: `SP0;` returns the current pen to the carousel (a real motion).
  Final `PU x,y;` to park position before `SP0;` per vpype's
  `final_pu_params`.
- Abort semantics: the 7475A predates HP-GL/2 `NR`. There is **no documented
  software "instant stop everything" command**. Safest cancel = stop feeding
  new data + let buffered motion finish + `IN;SP0;` afterwards. The UI must
  distinguish "stop sending" from "reset device" (spec §12).

## 10. Environment observations on this host (2026-08-18)

- Linux (WSL2 kernel 6.6.87), Python 3.12.3, pyserial 3.5, vpype 1.15.0
  installed in project venv.
- **No `/dev/ttyUSB*` present** — the FTDI adapter + plotter are attached to a
  different machine. All automated testing therefore runs against the PTY fake
  plotter (spec §35); real-device validation stays `READY FOR USER HARDWARE
  TEST`.
