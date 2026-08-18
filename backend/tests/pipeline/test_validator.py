"""Validator tests — BUILD_SPEC §24/§39: golden accepted, invalid fixtures
rejected with the right error class."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.pipeline.validator import MAX_JOB_BYTES, validate_hpgl
from conftest import HPGL_FIXTURES, hpgl_text


def test_golden_hpgl_valid():
    report = validate_hpgl(hpgl_text("golden.hpgl"), "a4")
    assert report.errors == []
    assert report.ok


@pytest.mark.parametrize(
    "name,needle",
    [
        ("invalid-bad-pen.hpgl", "pen"),
        ("invalid-out-of-range.hpgl", "hard-clip"),
        ("invalid-forbidden-output.hpgl", "output instruction"),
        ("invalid-esc-bytes.hpgl", "ESC"),
        ("invalid-unterminated.hpgl", "unterminated"),
    ],
)
def test_invalid_hpgl_rejected(name, needle):
    report = validate_hpgl(hpgl_text(name), "a4")
    assert not report.ok
    assert report.errors
    assert any(needle.lower() in e.lower() for e in report.errors), report.errors


def test_unknown_command_rejected():
    report = validate_hpgl("IN;SP1;PU0,0;PD1,1;XY99,99;SP0;", "a4")
    assert any("not in" in e for e in report.errors)


def test_in_df_only_at_start():
    report = validate_hpgl("IN;DF;SP1;PU0,0;PD1,1;IN;SP0;", "a4")
    assert any("only at the start" in e for e in report.errors)


def test_hpgl2_commands_rejected():
    report = validate_hpgl("IN;SP1;PU0,0;PD1,1;BR1,1;SP0;", "a4")
    assert any("not in" in e for e in report.errors)


def test_relative_mode_out_of_clip_detected():
    # PR moves tracked cumulatively: 0,0 -> +9000,+0 ok; +9000 more -> 18000 out
    report = validate_hpgl("IN;SP1;PU0,0;PR9000,0;PD9000,0;PD9000,0;PA;PU0,0;SP0;", "a4")
    assert any("hard-clip" in e for e in report.errors)


def test_soft_clip_tolerance_is_warning_not_error():
    # a4 x max 11040; 11100 is 60u beyond -> warning band (<=200u)
    report = validate_hpgl("IN;SP1;PA11100,100;PU0,0;SP0;", "a4")
    assert report.errors == []
    assert any("hard-clip" in w for w in report.warnings)


def test_pen_zero_allowed_pen_seven_rejected():
    ok = validate_hpgl("IN;SP0;PU0,0;SP0;", "a4")
    assert ok.ok
    bad = validate_hpgl("IN;SP7;PU0,0;SP0;", "a4")
    assert any("pen" in e for e in bad.errors)


def test_fs_pw_warn_ineffective():
    report = validate_hpgl("IN;SP1;FS4;PW0.3;PU0,0;PD1,1;PU0,0;SP0;", "a4")
    assert report.errors == []
    assert len([w for w in report.warnings if "no effect" in w]) == 2


def test_size_cap():
    unit = "PD1,1;"
    text = "IN;SP1;" + unit * (MAX_JOB_BYTES // len(unit) + 1) + "SP0;"
    report = validate_hpgl(text, "a4")
    assert any("byte cap" in e for e in report.errors)


def test_control_bytes_rejected():
    report = validate_hpgl("IN;SP1;PU0\x00,0;SP0;", "a4")
    assert any("control byte" in e for e in report.errors)


def test_paper_extents_used():
    # y=8000 beyond a4 (max 7721) by 279u > tolerance -> error; valid on a3 (max 11040)
    assert not validate_hpgl("IN;SP1;PA100,8000;SP0;", "a4").ok
    assert validate_hpgl("IN;SP1;PA100,8000;SP0;", "a3").ok
