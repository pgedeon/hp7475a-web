# Serial Troubleshooting — HP 7475A

Symptom → cause → fix, ordered by likelihood. All commands assume the FTDI
adapter shows up as `/dev/ttyUSB0`; prefer the stable path
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10OCJBA-if00-port0`.

## 1. No ports listed in the wizard

```bash
ls -l /dev/ttyUSB* /dev/serial/by-id/   # nothing? → adapter not bound
dmesg | tail -20                        # look for ftdi_sio attach lines
```

- Adapter unplugged / bad USB port → re-plug, check `lsusb` for
  `Future Technology Devices (FTDI)`.
- `brltty` grabs FT232R devices on some distros:
  `sudo systemctl disable --now brltty-udev.service brltty.service`.

## 2. Port listed but "permission denied"

```bash
sudo usermod -aG dialout $USER   # log out/in afterwards
```

The wizard's port list flags non-writable ports with a hint automatically.

## 3. Connects, but identify (OI) never answers

Check **both ends** of the serial configuration:

- Plotter rear-panel DIP switches must match the app settings. Factory-common
  config: **9600 baud, 8 data bits, no parity, 1 stop** (see Operation
  Manual). A baud mismatch usually yields garbage instead of silence.
- Wrong cable wiring. The 7475A is DTE (DB25). Most USB adapters are also
  DTE → you need a **null-modem (crossover) cable or adapter**, not a
  straight-through extension. TD↔RD crossed, SG common; DTR/DSR per the
  interconnect diagrams in the Op. Manual.
- Quick isolation: `OH;` reply = electrical + config OK.

## 4. Garbage replies (ÿÿ///… or random bytes)

- Baud/parity mismatch (plotter DIP vs app settings).
- Test 9600 8N1 first; only then try other rates.

## 5. Answers OI but plots never start / job stuck SENDING

- Buffer handshake failing: run **Diagnostics → Buffer Monitor**; a healthy
  idle plotter answers `ESC .B` with ~1024.
- If it answers 0 forever, the plotter's parser is stuck (e.g. swallowed a
  partial instruction from earlier experiments): power-cycle the plotter or
  send `IN;` via Manual Controls, then re-prepare the job.

## 6. Job goes DISCONNECTED mid-plot

- USB dropout or adapter replug. The job is preserved; reconnect via wizard.
  The wizard prefers the same stable by-id path. **Never** blindly resume a
  partially plotted job — re-plot instead (the paper already has ink).
- Check `dmesg` for `ttyUSB0: USB disconnect` entries; try a different cable
  (FTDI clones are notorious under load).

## 7. Status shows error bit (32)

- Query `OE` (HP-GL error) and `ESC .E` (I/O error) on the Diagnostics page —
  codes are decoded inline; meanings are in
  `backend/app/services/serial/protocol.py` with manual citations.
- Common: error 16 = input buffer overflow (host overran the handshake —
  file a bug, this controller should never cause it); 15 = framing/parity.

## 8. Everything answers, pen doesn't move

- Pinch wheels up / not "Ready for data" (status bit 16): load paper.
- Pen selected is 0 (`SP0` parks the pen — check Manual Controls).
- Viewport/paper mismatch: DIP switch set to imperial but plotting metric
  hard-clip coordinates lands off-page on A-size paper. Verify with `OH`:
  `0,0,11040,7721` = A4 metric, `0,0,10365,7962` = ANSI A imperial.

## 9. Reboot loop of the service under systemd

- `DeviceAllow=` restrictions too tight for your adapter → adjust the unit
  (see comments in `deploy/systemd/hp7475a-web.service`).
- Data dir not writable by the service user → `ReadWritePaths=` must match.
