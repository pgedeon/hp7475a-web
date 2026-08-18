"""Discovery tests: by-id preference, FTDI flag, permission hint
(BUILD_SPEC §5-§6)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.serial import discovery


def _port(device, vid=None, pid=None, description=""):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


def test_by_id_preferred_and_ftdi_flagged(tmp_path):
    dev = tmp_path / "ttyUSB0"
    dev.write_bytes(b"")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    stable = by_id / "usb-FTDI_FT232R_USB_UART_A10OCJBA-if00-port0"
    stable.symlink_to(dev)

    infos = discovery.list_ports(
        comports=lambda: [_port(str(dev), vid=0x0403, pid=0x6001,
                                description="FT232R USB UART")],
        by_id_dir=str(by_id),
    )

    assert len(infos) == 1
    info = infos[0]
    assert info.ftdi is True
    assert info.by_id_path == str(stable)
    assert info.preferred_path == str(stable)
    assert info.vid == 0x0403 and info.pid == 0x6001


def test_non_ftdi_and_no_by_id(tmp_path):
    dev = tmp_path / "ttyACM0"
    dev.write_bytes(b"")
    infos = discovery.list_ports(
        comports=lambda: [_port(str(dev), vid=0x2341, description="Arduino")],
        by_id_dir=str(tmp_path / "empty"),
    )
    assert infos[0].ftdi is False
    assert infos[0].by_id_path is None
    assert infos[0].preferred_path == str(dev)


def test_permission_failure_gives_dialout_hint(tmp_path, monkeypatch):
    dev = tmp_path / "ttyUSB0"
    dev.write_bytes(b"")
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: False)
    infos = discovery.list_ports(comports=lambda: [_port(str(dev))],
                                 by_id_dir=str(tmp_path))
    assert infos[0].writable is False
    assert "dialout" in infos[0].hint


def test_writable_port_has_no_hint(tmp_path):
    dev = tmp_path / "ttyUSB0"
    dev.write_bytes(b"")
    infos = discovery.list_ports(comports=lambda: [_port(str(dev))],
                                 by_id_dir=str(tmp_path))
    assert infos[0].writable is True
    assert infos[0].hint == ""


def test_missing_device_node_not_writable(tmp_path):
    infos = discovery.list_ports(
        comports=lambda: [_port(str(tmp_path / "ghost"))],
        by_id_dir=str(tmp_path),
    )
    assert infos[0].writable is False
    assert "dialout" in infos[0].hint


def test_unknown_vid_none_is_not_ftdi(tmp_path):
    dev = tmp_path / "ttyS0"
    dev.write_bytes(b"")
    infos = discovery.list_ports(comports=lambda: [_port(str(dev), vid=None)],
                                 by_id_dir=str(tmp_path))
    assert infos[0].ftdi is False
