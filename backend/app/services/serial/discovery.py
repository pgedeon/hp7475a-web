"""Serial port discovery for Linux hosts (BUILD_SPEC §5-§6).

Enumerates candidate serial ports via ``serial.tools.list_ports`` preferring
stable ``/dev/serial/by-id/`` paths, flags FTDI adapters (VID 0x0403), and
checks write permission with a ``dialout``-group hint. Pure discovery — this
module never opens a port.
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass

from serial.tools import list_ports as _list_ports

logger = logging.getLogger(__name__)

#: FTDI vendor ID (FT232/FT232R USB-serial bridges; BUILD_SPEC §1).
FTDI_VENDOR_ID = 0x0403

#: Stable-alias directory preferred for device persistence (BUILD_SPEC §5).
DEFAULT_BY_ID_DIR = "/dev/serial/by-id"

_PERMISSION_HINT = (
    "No write permission. Add your user to the dialout group, then log out/in: "
    'sudo usermod -aG dialout "$USER"'
)


@dataclass(frozen=True)
class PortInfo:
    """One candidate serial port, enriched with discovery metadata."""

    device: str
    by_id_path: str | None
    description: str
    vid: int | None
    pid: int | None
    ftdi: bool
    writable: bool
    hint: str

    @property
    def preferred_path(self) -> str:
        """``by-id`` path when available, else the raw device node."""
        return self.by_id_path or self.device


def list_ports(comports=None, by_id_dir: str = DEFAULT_BY_ID_DIR) -> list[PortInfo]:
    """Enumerate serial ports with by-id mapping, FTDI flag, permission check.

    Args:
        comports: callable returning ``ListPortInfo``-like objects; defaults to
            ``serial.tools.list_ports.comports`` (injectable for tests).
        by_id_dir: directory of stable ``by-id`` symlinks to scan.

    Returns:
        PortInfo list in enumeration order.
    """
    comports = comports or _list_ports.comports
    by_id = _by_id_index(by_id_dir)
    infos: list[PortInfo] = []
    for p in comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        writable = os.path.exists(p.device) and os.access(p.device, os.W_OK)
        infos.append(
            PortInfo(
                device=p.device,
                by_id_path=by_id.get(os.path.realpath(p.device)),
                description=getattr(p, "description", "") or "",
                vid=vid,
                pid=pid,
                ftdi=vid == FTDI_VENDOR_ID,
                writable=writable,
                hint="" if writable else _PERMISSION_HINT,
            )
        )
    logger.debug("discovered %d serial port(s)", len(infos))
    return infos


def _by_id_index(by_id_dir: str) -> dict[str, str]:
    """Map realpath(device) -> by-id symlink path for stable identification."""
    index: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(by_id_dir, "*"))):
        index[os.path.realpath(path)] = path
    return index
