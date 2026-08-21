"""Vectorizer service unit tests (goals 950c719c + a7f70dae).

The SLD subprocess is MOCKED at the ``subprocess.Popen`` level — no real
23s vectorization in unit tests. Covers: input validation, CLI arg
construction (single-color + multi-color), success/failure/timeout/cancel
paths, stage-line streaming, stderr-tail capture, concurrency semaphore.
"""

from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import vectorizer
from app.services.vectorizer import (
    VectorizeError,
    run_vectorization,
    validate_colors,
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

def test_validate_colors_bounds():
    assert validate_colors(1) == 1
    assert validate_colors(8) == 8
    for bad in (0, -1, 9, 100):
        with pytest.raises(ValueError, match="colors must be"):
            validate_colors(bad)

# ---------------------------------------------------------------- fake process

SVG = "<svg xmlns='http://www.w3.org/2000/svg'/>"

class FakeProc:
    """Mimics subprocess.Popen enough for run_vectorization."""

    def __init__(self, out="", returncode=0, write_to=None, wait_delay=0.0):
        self.stdout = io.StringIO(out)
        self.returncode = returncode
        self._write_to = write_to
        self._wait_delay = wait_delay
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = -9
            return self.returncode
        if self._wait_delay:
            if timeout is not None and self._wait_delay > timeout:
                time.sleep(min(timeout, 0.2))
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            time.sleep(self._wait_delay)
        if self._write_to:
            Path(self._write_to).write_text(SVG)
        return self.returncode

def _popen_fake(out="", returncode=0, write_to=None, wait_delay=0.0):
    """Patch target: vectorizer.subprocess.Popen -> FakeProc instance."""
    def fake(cmd, **kw):
        return FakeProc(out=out, returncode=returncode, write_to=write_to,
                        wait_delay=wait_delay)
    return fake

# ---------------------------------------------------------------- success path

def test_run_success_writes_output_and_returns_meta(tmp_path):
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        res = run_vectorization(tmp_path, b"imgbytes", "cat.png")

    assert res.id
    assert res.svg_path.endswith("output.svg")
    assert Path(res.svg_path).is_file()
    assert res.duration_s >= 0
    # input saved under the job dir
    job_dir = Path(res.svg_path).parent
    assert (job_dir / "input.png").read_bytes() == b"imgbytes"
    # CLI invoked with the run subcommand
    cmd = mp.call_args[0][0]
    assert cmd[0] == vectorizer.SLD_CLI
    assert cmd[1] == "run"

def test_run_passes_thresh_and_multiple_lines(tmp_path):
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        run_vectorization(tmp_path, b"x", "a.png", thresh=0.6, multiple_lines=True)
    cmd = mp.call_args[0][0]
    assert "--thresh" in cmd and cmd[cmd.index("--thresh") + 1] == "0.6"
    assert "--multiple-lines" in cmd

def test_run_omits_optional_flags_by_default(tmp_path):
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        run_vectorization(tmp_path, b"x", "a.png")
    cmd = mp.call_args[0][0]
    assert "--thresh" not in cmd
    assert "--multiple-lines" not in cmd

def test_run_multicolor_uses_wrapper_script(tmp_path):
    def fake(cmd, **kw):
        # multicolor CLI: IN.png OUT.svg --colors K (positional output)
        out = Path(cmd[3])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        res = run_vectorization(tmp_path, b"x", "a.png", colors=4)
    assert Path(res.svg_path).is_file()
    cmd = mp.call_args[0][0]
    assert cmd[0] == vectorizer.SLD_PYTHON
    assert cmd[1] == str(vectorizer.MULTICOLOR_SCRIPT)
    assert cmd[cmd.index("--colors") + 1] == "4"
    assert "--thresh" not in cmd and "--multiple-lines" not in cmd

def test_run_uses_data_dir_as_cwd_and_cache_env(tmp_path):
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        run_vectorization(tmp_path, b"x", "a.png")
    kw = mp.call_args[1]
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["TORCH_HOME"] == "/tmp/torch_home"
    assert kw["env"]["NUMBA_CACHE_DIR"] == "/tmp/numba_cache"
    assert kw["env"]["PYTHONPYCACHEPREFIX"] == "/tmp/pycache"

def test_run_streams_stage_lines(tmp_path):
    seen: list[str] = []
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(out="noise\nSTAGE loading image\nSTAGE layer 1/2 (#ff0000)\n",
                        write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake):
        run_vectorization(tmp_path, b"x", "a.png", on_stage=seen.append)
    assert seen == ["loading image", "layer 1/2 (#ff0000)"]

def test_run_default_timeouts_by_mode(tmp_path):
    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake) as mp:
        run_vectorization(tmp_path, b"x", "a.png")
        assert "timeout" not in mp.call_args[1]  # Popen takes no timeout kwarg
    # explicit defaults are exported for the API layer
    assert vectorizer.DEFAULT_TIMEOUT_SINGLE == 1800.0
    assert vectorizer.DEFAULT_TIMEOUT_MULTI == 3600.0

# ---------------------------------------------------------------- failure paths

def test_run_nonzero_exit_raises_with_stderr_tail(tmp_path):
    with patch.object(vectorizer.subprocess, "Popen",
                      side_effect=_popen_fake(out="boom\nCUDA error at line 42\n",
                                              returncode=1)):
        with pytest.raises(VectorizeError) as ei:
            run_vectorization(tmp_path, b"x", "a.png")
    assert "exit 1" in str(ei.value)
    assert "CUDA error" in ei.value.stderr_tail

def test_run_missing_output_raises(tmp_path):
    with patch.object(vectorizer.subprocess, "Popen",
                      side_effect=_popen_fake(returncode=0)):
        with pytest.raises(VectorizeError, match="no SVG output"):
            run_vectorization(tmp_path, b"x", "a.png")

def test_run_timeout_raises_and_kills(tmp_path):
    def fake(cmd, **kw):
        return FakeProc(wait_delay=5.0)  # wait() sleeps past any timeout

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake):
        with pytest.raises(VectorizeError, match="timed out after 1s"):
            run_vectorization(tmp_path, b"x", "a.png", timeout_s=1)

def test_run_cli_missing_raises(tmp_path):
    def fake(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake):
        with pytest.raises(VectorizeError, match="not found"):
            run_vectorization(tmp_path, b"x", "a.png")

def test_run_cancel_event_kills_process(tmp_path):
    ev = threading.Event()
    ev.set()

    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(out="STAGE working\n", write_to=str(out))

    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake):
        with pytest.raises(VectorizeError, match="cancelled"):
            run_vectorization(tmp_path, b"x", "a.png", cancel_event=ev)

def test_run_rejects_bad_image_type(tmp_path):
    with pytest.raises(ValueError):
        run_vectorization(tmp_path, b"x", "a.svg")

def test_run_rejects_bad_colors(tmp_path):
    with pytest.raises(ValueError, match="colors must be"):
        run_vectorization(tmp_path, b"x", "a.png", colors=12)

# ---------------------------------------------------------------- concurrency

def test_semaphore_limits_concurrency(tmp_path):
    """Two concurrent runs proceed; a third blocks until one releases."""
    calls = {"active": 0, "max": 0}
    lock = threading.Lock()

    def fake(cmd, **kw):
        out = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(write_to=str(out), wait_delay=0.05)

    def track_wait(orig_wait):
        def wait(timeout=None):
            with lock:
                calls["active"] += 1
                calls["max"] = max(calls["max"], calls["active"])
            try:
                return orig_wait(timeout)
            finally:
                with lock:
                    calls["active"] -= 1
        return wait

    real_sem = vectorizer._SEM
    vectorizer._SEM = threading.Semaphore(2)
    try:
        procs: list[FakeProc] = []
        def fake2(cmd, **kw):
            out = Path(cmd[cmd.index("--output-path") + 1])
            p = FakeProc(write_to=str(out), wait_delay=0.05)
            p.wait = track_wait(p.wait)
            procs.append(p)
            return p

        with patch.object(vectorizer.subprocess, "Popen", side_effect=fake2):
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
