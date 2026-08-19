"""Split over-long PD/PU coordinate lists into bounded instructions.

PD continuation semantics: a new ``PD;`` continues from the current
position, so re-chunking the pair list is geometry-identical. PU with
multiple pairs passes through the same points pen-up. Keeps every
instruction within the streamer's boundary-only safe window (2026-08-19
mid-split parser corruption fix). Vpype-free so API routes import it.
"""
from __future__ import annotations

import re

MAX_CHARS = 240


def split_long_pd(hpgl: str, max_chars: int = MAX_CHARS) -> str:
    parts = hpgl.split(";")
    out: list[str] = []
    for cmd in parts:
        if cmd.startswith(("PD", "PU")) and len(cmd) > max_chars:
            pairs = re.findall(r"-?\d+,-?\d+", cmd)
            head = cmd[:2]
            chunk: list[str] = []
            n = 2
            for pair in pairs:
                if n + len(pair) + 1 > max_chars and chunk:
                    out.append(head + ",".join(chunk))
                    chunk = []
                    n = 2
                chunk.append(pair)
                n += len(pair) + 1
            if chunk:
                out.append(head + ",".join(chunk))
        else:
            out.append(cmd)
    return ";".join(out)
