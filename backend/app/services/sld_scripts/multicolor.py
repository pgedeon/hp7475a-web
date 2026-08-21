"""Multi-color raster → layered SVG via SLD-Vectorization internals.

Runs INSIDE the sld venv (needs torch/skimage/svgwrite deps). Invoked by the
plotter backend as a subprocess:

    ~/sld-venv/bin/python multicolor.py IN.png OUT.svg --colors K

Pipeline: quantize RGB to K dominant non-white clusters → per-cluster binary
mask → full SLD pipeline per layer (medial axis → stroke ordering with the
intersection classifier → bezier fitting) → merge into ONE svg with a
``<g stroke="#hex" ...>`` group per color. The app's analyzer/colormap are
inheritance-aware, so by-color pen mapping works unchanged.

Progress: prints ``STAGE <text>`` lines on stdout (parsed by the backend).
Exit 0 on success; nonzero with traceback on stderr otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image
from skimage import io
from skimage.transform import rescale

sys.path.insert(0, "/home/pgedeon/SLD-Vectorization/src")

from SLDvec.fitting import fit_all_curves
from SLDvec.ordering import get_predictor, get_stroke_order
from SLDvec.preprocessing import load_image, potrace_vectorize
from SLDvec.skeleton import get_medial_axis


def stage(msg: str) -> None:
    print(f"STAGE {msg}", flush=True)


def hexcolor(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(c) for c in rgb)


def plan_layers(rgb: np.ndarray, k: int) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Quantize to clusters; return (selected cluster ids, label map, palette).

    Background = brightest cluster (closest to white). Tiny clusters are
    dropped; at most *k* layers survive, sorted by pixel count desc.
    """
    img8 = rgb.astype(np.uint8)
    n_slots = min(k + 1, 16)
    q = Image.fromarray(img8).quantize(colors=n_slots, method=Image.MEDIANCUT)
    labels = np.array(q)
    pal = np.array(q.getpalette()[: n_slots * 3], dtype=np.float64).reshape(-1, 3)

    counts = np.bincount(labels.ravel(), minlength=len(pal))
    lum = pal @ np.array([0.299, 0.587, 0.114])
    bg = int(np.argmax(lum))

    order = [i for i in np.argsort(-counts) if i != bg]
    min_px = max(100, int(0.004 * labels.size))
    selected = [i for i in order if counts[i] >= min_px][:k]
    return selected, labels, pal


def splines_to_paths(splines, scale_ratio: float) -> list[str]:
    """Replicate SLDvec.utils.svg.export_svg geometry as raw path d-strings.

    ``splines`` is a ragged list (strokes have differing bezier counts), so
    convert per-stroke — a global np.array would raise on inhomogeneous
    shapes (caught live e2e, job 61f36b287cb8).
    """
    out: list[str] = []
    for stroke in splines:
        beziers = np.array(stroke, dtype=np.float64) / scale_ratio
        parts = [f"M {beziers[0][0][0]:.4f} {beziers[0][0][1]:.4f}"]
        for bez in beziers:
            parts.append(
                f"C {bez[1][0]:.4f} {bez[1][1]:.4f} "
                f"{bez[2][0]:.4f} {bez[2][1]:.4f} "
                f"{bez[3][0]:.4f} {bez[3][1]:.4f}"
            )
        if (beziers[0][0] == beziers[-1][-1]).all():
            parts.append("Z")
        out.append(" ".join(parts))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--colors", type=int, default=3)
    args = ap.parse_args()

    t0 = time.monotonic()
    stage("loading image")
    gray, orig_shape, scale_ratio = load_image(str(args.input))
    rgb = io.imread(str(args.input))
    if rgb.ndim == 3 and rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, axis=-1)
    if scale_ratio != 1:
        rgb = rescale(rgb, scale_ratio, anti_aliasing=True, channel_axis=-1)

    stage(f"quantizing to <= {args.colors} colors")
    selected, labels, pal = plan_layers(rgb, args.colors)
    if not selected:
        print("no ink layers found", file=sys.stderr, flush=True)
        return 2

    predictor = get_predictor()

    h, w = orig_shape[0], orig_shape[1]
    groups: list[tuple[str, list[str]]] = []
    for i, cid in enumerate(selected):
        hexc = hexcolor(pal[cid])
        stage(f"layer {i + 1}/{len(selected)} ({hexc}): medial axis")
        mask = labels == cid
        layer_gray = np.where(mask, 0.0, 1.0)
        binary = (layer_gray > 0.5).astype(int)

        curves = potrace_vectorize(binary)
        if not curves:
            continue
        G = get_medial_axis(curves, multiple_lines=True)[1]

        stage(f"layer {i + 1}/{len(selected)} ({hexc}): ordering strokes")
        node_lists, term = get_stroke_order(
            G=G, image=layer_gray, model=predictor, force_single_line=False
        )

        stage(f"layer {i + 1}/{len(selected)} ({hexc}): fitting curves")
        splines = fit_all_curves(G, terminating_node=term, node_lists=node_lists)
        groups.append((hexc, splines_to_paths(splines, scale_ratio)))

    stage("exporting svg")
    parts = [
        f'<?xml version="1.0" encoding="utf-8" ?>\n'
        f'<svg baseProfile="tiny" version="1.2" width="{w}" height="{h}" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]
    for hexc, paths in groups:
        parts.append(
            f'<g stroke="{hexc}" stroke-width="2" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round">'
        )
        parts.extend(f'<path d="{d}"/>' for d in paths)
        parts.append("</g>")
    parts.append("</svg>")
    args.output.write_text("\n".join(parts), encoding="utf-8")

    stage(f"done in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
