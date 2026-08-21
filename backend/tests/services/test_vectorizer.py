"""Vectorizer service unit tests (goal 950c719c).

The SLD subprocess is MOCKED — no real 23s vectorization in unit tests.
Covers: input validation, CLI arg construction, success/failure/timeout
paths, stderr-tail capture, and the concurrency semaphore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import vectorizer
from app.services.vectorizer import (
    VectorizeError,
    run_vectorization,
    validate_image,
)


# ---------------------------------------------------------------- validation

def test_validate_image_ok():
    assert validate_image("cat.png", b"x") == ".png"
    assert validate_image("CAT.JPG", b"x") == ".jpg"
    assert validate_image("a.webp", b"x") == ".webp"
    assert validate_image("a.bmp", b"x") == ".bmp"
    assert validate_image("a.jpeg", b"x") == ".jpeg"


def test_validate_image_rejects_bad_ext():
    with pytest.raises(ValueError, match="unsupported image type"):
        validate_image("drawing.svg", b"x")
    with pytest.raises(ValueError, match="unsupported image type"):
        validate_image("noext", b"x")


def test_validate_image_rejects_oversize():
    big = b"x" * (vectorizer.MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError, match="too large"):
        validate_image("big.png", big)


# ---------------------------------------------------------------- success path

def _ok_proc(returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = "line1\nline2\n"
    m.stdout = ""
    return m


def test_run_success_writes_output_and_returns_meta(tmp_path):
    # The CLI "writes" the SVG by having the mock create it.
    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        out.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        return _ok_proc(0)

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run) as mr:
        res = run_vectorization(tmp_path, b"imgbytes", "cat.png")

    assert res.id
    assert res.svg_path.endswith("output.svg")
    assert Path(res.svg_path).is_file()
    assert res.duration_s >= 0
    # input saved under the job dir
    job_dir = Path(res.svg_path).parent
    assert (job_dir / "input.png").read_bytes() == b"imgbytes"
    # CLI invoked with the run subcommand
    cmd = mr.call_args[0][0]
    assert cmd[0] == vectorizer.SLD_CLI
    assert cmd[1] == "run"


def test_run_passes_thresh_and_multiple_lines(tmp_path):
    def fake_run(cmd, **kw):
        Path(cmd[cmd.index("--output-path") + 1]).write_text("<svg/>")
        return _ok_proc(0)

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run) as mr:
        run_vectorization(
            tmp_path, b"x", "a.png", thresh=0.6, multiple_lines=True
        )
    cmd = mr.call_args[0][0]
    assert "--thresh" in cmd and cmd[cmd.index("--thresh") + 1] == "0.6"
    assert "--multiple-lines" in cmd


def test_run_omits_optional_flags_by_default(tmp_path):
    def fake_run(cmd, **kw):
        Path(cmd[cmd.index("--output-path") + 1]).write_text("<svg/>")
        return _ok_proc(0)

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run) as mr:
        run_vectorization(tmp_path, b"x", "a.png")
    cmd = mr.call_args[0][0]
    assert "--thresh" not in cmd
    assert "--multiple-lines" not in cmd


def test_run_uses_data_dir_as_cwd_and_cache_env(tmp_path):
    def fake_run(cmd, **kw):
        Path(cmd[cmd.index("--output-path") + 1]).write_text("<svg/>")
        return _ok_proc(0)

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run) as mr:
        run_vectorization(tmp_path, b"x", "a.png")
    kw = mr.call_args[1]
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["TORCH_HOME"] == "/tmp/torch_home"
    assert kw["env"]["NUMBA_CACHE_DIR"] == "/tmp/numba_cache"
    assert kw["env"]["PYTHONPYCACHEPREFIX"] == "/tmp/pycache"


# ---------------------------------------------------------------- failure paths

def test_run_nonzero_exit_raises_with_stderr_tail(tmp_path):
    def fake_run(cmd, **kw):
        # no output file written
        return _ok_proc(1)

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
        with pytest.raises(VectorizeError) as ei:
            run_vectorization(tmp_path, b"x", "a.png")
    assert "exit 1" in str(ei.value)
    assert "line2" in ei.value.stderr_tail  # tail captured


def test_run_missing_output_raises(tmp_path):
    def fake_run(cmd, **kw):
        return _ok_proc(0)  # exit 0 but no file

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
        with pytest.raises(VectorizeError, match="no SVG output"):
            run_vectorization(tmp_path, b"x", "a.png")


def test_run_timeout_raises(tmp_path):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"", stderr="partial\nerr")

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
        with pytest.raises(VectorizeError, match="timed out"):
            run_vectorization(tmp_path, b"x", "a.png", timeout_s=5)


def test_run_cli_missing_raises(tmp_path):
    def fake_run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
        with pytest.raises(VectorizeError, match="not found"):
            run_vectorization(tmp_path, b"x", "a.png")


def test_run_rejects_bad_image_type(tmp_path):
    with pytest.raises(ValueError):
        run_vectorization(tmp_path, b"x", "a.svg")


# ---------------------------------------------------------------- concurrency

def test_semaphore_limits_concurrency(tmp_path):
    """Two concurrent runs proceed; a third blocks until one releases."""
    import threading

    calls = {"active": 0, "max": 0}
    lock = threading.Lock()

    real_sem = vectorizer._SEM

    def fake_run(cmd, **kw):
        with lock:
            calls["active"] += 1
            calls["max"] = max(calls["max"], calls["active"])
        import time
        time.sleep(0.05)
        with lock:
            calls["active"] -= 1
        Path(cmd[cmd.index("--output-path") + 1]).write_text("<svg/>")
        return _ok_proc(0)

    # Fresh semaphore(2) to test in isolation
    vectorizer._SEM = threading.Semaphore(2)
    try:
        with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
            threads = [
                threading.Thread(
                    target=run_vectorization, args=(tmp_path, b"x", f"a{i}.png")
                )
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    finally:
        vectorizer._SEM = real_sem

    assert calls["max"] <= 2, f"max concurrent {calls['max']} exceeded 2"
