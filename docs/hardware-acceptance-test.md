# Hardware Acceptance Test — HP 7475A (USER-INITIATED ONLY)

These steps exercise the **physical plotter**. They are deliberately manual:
automated tests never move real hardware. Prereq: controller running on the
plotter host, plotter powered, paper + pens loaded, DIP switches matched
(9600 8N1, paper mode per sheet loaded).

## 1. Discovery & connect (no motion)

- [ ] Device → wizard lists the FTDI port (by-id path shown, no permission warning)
- [ ] Connect succeeds; **Identify shows `7475A`**
- [ ] Status shows: Ready (bit 16) set, Error clear
- [ ] OH check reports hard-clip matching loaded paper:
      A4 `0,0,11040,7721` · A3 `0,0,16158,11040` · A `0,0,10365,7962` · B `0,0,16640,10365`

## 2. Manual controls (gentle motion — keep hands clear)

- [ ] Pen Up works; Pen 1 selects (carousel rotates)
- [ ] Jog +10 mm X, +10 mm Y moves as expected; a 100 mm move lands visibly inside paper
- [ ] Jog far corner (e.g. 500,400 mm) **clamps** at hard-clip (warning shown, no grinding)
- [ ] Park returns pen + carriage home; pen stored (SP0)

## 3. Test plot (fixtures/hpgl/test-square.hpgl — small square, pen 1)

- [ ] Upload HP-GL → validator passes → Create job → Prepare → READY
- [ ] Start-confirmation modal shows paper + pen summary
- [ ] Start → SENDING visible with byte progress (buffer handshake live in Diagnostics)
- [ ] Plot draws a clean square; job transitions PLOTTING → COMPLETING → COMPLETED
      **only after** the pen physically finishes (queued-OA completion proof)
- [ ] Pause during a longer plot: motion continues briefly (drains buffer) then stops; Resume continues; Cancel stops cleanly

## 4. Multi-pen plot (fixtures/hpgl/test-pens.hpgl — 6 small marks, pens 1–6)

- [ ] Carousel selects each pen in turn; marks land at expected spots
- [ ] Preview colors match physical pens used

## 5. SVG pipeline on real device

- [ ] Upload fixtures/svg/benign.svg → sanitize report clean → layers detected
- [ ] Map 3 layers to pens 1/2/3 → Prepare → preview matches expectation
- [ ] Plot completes; drawn output matches preview within ~1 mm

## 6. Fault handling (physical)

- [ ] Unplug USB mid-job → job DISCONNECTED (not COMPLETED), reconnect instructions shown; reconnecting lists the same by-id port
- [ ] Lift pinch wheels mid-job → status shows Ready cleared; restore continues/COMPLETING per timing
- [ ] Query OE + ESC .E after all tests → both report 0

## 7. Sign-off

- [ ] All above checked on hardware: ______ (date, operator)
- [ ] Any deviations noted in GitHub issue labeled `hardware-test`
