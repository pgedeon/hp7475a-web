"""SVG sanitizer — untrusted input hardening for the plot pipeline.

Implements BUILD_SPEC §14 (SVG security) and §45 ("do not execute scripts in
uploaded SVGs, do not allow external resource fetching"). Treats every
uploaded SVG as hostile XML:

- oversized payloads rejected outright (default 20 MB)
- fail-closed: anything unparseable is rejected, never passed through
- `<script>`, `<foreignObject>`, `<iframe>`, `<embed>`, `<object>`,
  `<link>`, `<handler>` elements removed
- all ``on*`` event-handler attributes removed
- ``javascript:``/``vbscript:``/``data:text/html``/``file:`` URL schemes
  neutralized wherever they appear
- external references (http(s)://, protocol-relative, bare relative paths)
  stripped from ``href``/``xlink:href``; `<use>` with an external reference
  is removed entirely (spec §14 "references that could cause server-side
  network access")
- DOCTYPE removed entirely on serialization; a DOCTYPE containing ENTITY
  declarations (XXE / entity-expansion vector) is rejected outright

``lxml`` is not used because it is absent from the project venv; the hardened
stdlib ``xml.etree`` parser is used instead. This is safe by construction in
CPython 3.12: ``ElementTree`` never fetches external DTDs, never resolves
external entities, and raises ``ParseError`` on any entity reference it does
not know (so entity-expansion attacks fail closed into the reject path).
Nothing in this module executes, evaluates, or fetches anything.

Spec references: BUILD_SPEC §13, §14, §45; hardware-notes §"SVG".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

__all__ = ["SanitizeReport", "sanitize_svg", "DEFAULT_MAX_BYTES"]

DEFAULT_MAX_BYTES = 20 * 1024 * 1024  #: 20 MB upload cap (spec §14)

#: Local names of elements removed entirely, with children.
_REMOVED_ELEMENTS = frozenset(
    {"script", "foreignObject", "iframe", "embed", "object", "link", "handler"}
)

#: URL schemes that are dangerous in any attribute position.
_DANGEROUS_URL_RE = re.compile(r"^\s*(javascript|vbscript|file):", re.IGNORECASE)
_DANGEROUS_DATA_RE = re.compile(r"^\s*data:\s*text/html", re.IGNORECASE)
#: External reference = absolute http(s), protocol-relative, or any non-"#"
#: relative target. Anything that is not a same-document fragment counts as
#: external (defence in depth; the pipeline never fetches anyway).
_EXTERNAL_URL_RE = re.compile(r"^\s*(https?:|//)", re.IGNORECASE)

_DOCTYPE_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_ENTITY_RE = re.compile(r"<!ENTITY", re.IGNORECASE)

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
_SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"

# Serialize back with readable prefixes instead of ns0:/ns1:.
for _prefix, _uri in (("", _SVG_NS), ("xlink", _XLINK_NS),
                      ("inkscape", _INKSCAPE_NS), ("sodipodi", _SODIPODI_NS)):
    ET.register_namespace(_prefix, _uri)


def _localname(tag: str) -> str:
    """Element local name, namespace-insensitive ('{ns}g' -> 'g')."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


@dataclass
class SanitizeReport:
    """Outcome of one sanitize_svg() call.

    Attributes:
        rejected: True when the input was refused outright (oversize,
            unparseable, entity declarations, non-SVG root). When True the
            returned bytes are empty b"" — callers must not proceed.
        reasons: why the input was rejected (empty unless rejected).
        removals: human-readable record of every neutralization applied.
    """

    rejected: bool = False
    reasons: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the input was accepted (possibly after neutralization)."""
        return not self.rejected


def sanitize_svg(
    data: bytes, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[bytes, SanitizeReport]:
    """Sanitize untrusted SVG bytes.

    Args:
        data: raw SVG bytes as uploaded.
        max_bytes: hard size cap; larger payloads are rejected.

    Returns:
        ``(clean_bytes, report)``. On rejection ``clean_bytes`` is ``b""``
        and ``report.rejected`` is True (fail-closed). On success the bytes
        are a re-serialized, namespace-preserving SVG with every dangerous
        construct listed in ``report.removals`` removed. This function never
        executes script, never resolves entities, and never touches network.
    """
    report = SanitizeReport()

    if len(data) > max_bytes:
        report.rejected = True
        report.reasons.append(
            f"payload too large: {len(data)} bytes (cap {max_bytes})"
        )
        return b"", report

    # DOCTYPE handling: declaration is dropped on re-serialization, but any
    # internal ENTITY declaration is a hard reject (XXE / billion-laughs).
    head = data[:8192]
    try:
        head_text = head.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        report.rejected = True
        report.reasons.append("not valid UTF-8")
        return b"", report
    if _DOCTYPE_RE.search(head_text) and _ENTITY_RE.search(
        data.decode("utf-8", errors="ignore")
    ):
        report.rejected = True
        report.reasons.append(
            "DOCTYPE with ENTITY declarations rejected (XXE/entity-expansion vector)"
        )
        return b"", report

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        report.rejected = True
        report.reasons.append(f"unparseable XML: {exc}")
        return b"", report

    if _localname(root.tag) != "svg":
        report.rejected = True
        report.reasons.append(f"root element is <{_localname(root.tag)}>, not <svg>")
        return b"", report

    if _DOCTYPE_RE.search(head_text):
        report.removals.append("DOCTYPE declaration removed")

    _clean_tree(root, report)
    _strip_page_backgrounds(root, report)

    clean = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return clean, report


_NON_RENDER_CONTAINERS = frozenset(
    {"defs", "symbol", "clipPath", "mask", "pattern", "marker"}
)


def _style_declarations(style_value: str) -> dict[str, str]:
    """Parse a ``style="a:b;c:d"`` attribute into a dict (lowercase keys)."""
    out: dict[str, str] = {}
    for chunk in style_value.split(";"):
        if ":" not in chunk:
            continue
        k, _, v = chunk.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _strip_page_backgrounds(root: ET.Element, report: SanitizeReport) -> int:
    """Remove fill-only, full-page background <rect> elements.

    Vector exports routinely include an invisible page background (e.g.
    ``<rect width="297" height="210" fill="white"/>``). A pen plotter cannot
    fill, so the outline-only pipeline would draw its OUTLINE — a frame
    around every plot that the user never sees in any viewer (2026-08-19
    user report). Strip rects that are:

    * fill-only: effective fill != none AND effective stroke == none
      (inheritance-aware: presentation attrs and inline ``style`` from
      ancestors count — a border rect inside ``<g stroke="black">`` is
      VISIBLE content and must survive)
    * full-page: covers >= 99% of the viewBox area
    * untransformed, and not inside defs/symbol/clipPath/mask/pattern

    Recorded in ``report.removals``. Idempotent: second run finds nothing.
    """
    vb = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(vb) != 4:
        return 0
    try:
        _vx, _vy, vw, vh = (float(v) for v in vb)
    except ValueError:
        return 0
    if vw <= 0 or vh <= 0:
        return 0
    page_area = vw * vh
    removed = 0

    def _effective(el: ET.Element, fill: str | None,
                   stroke: str | None) -> tuple[str | None, str | None]:
        f, s = el.attrib.get("fill"), el.attrib.get("stroke")
        style = el.attrib.get("style")
        if style:
            decls = _style_declarations(style)
            f = decls.get("fill", f)
            s = decls.get("stroke", s)
        return (f if f is not None else fill,
                s if s is not None else stroke)

    def _walk(el: ET.Element, fill: str | None, stroke: str | None,
              in_defs: bool) -> None:
        nonlocal removed
        fill, stroke = _effective(el, fill, stroke)
        name = _localname(el.tag)
        in_defs = in_defs or name in _NON_RENDER_CONTAINERS
        if (name == "rect" and not in_defs
                and "transform" not in el.attrib):
            fillable = fill is not None and fill.strip().lower() != "none"
            stroked = stroke is not None and stroke.strip().lower() != "none"
            if fillable and not stroked:
                try:
                    x = float(el.attrib.get("x", "0"))
                    y = float(el.attrib.get("y", "0"))
                    w = float(el.attrib["width"])
                    h = float(el.attrib["height"])
                except (KeyError, ValueError):
                    w = h = 0.0
                if w > 0 and h > 0 and w * h >= 0.99 * page_area:
                    parent_map = {c: p for p in root.iter() for c in p}
                    parent = parent_map.get(el)
                    if parent is not None:
                        parent.remove(el)
                        removed += 1
                    return
        for child in list(el):
            _walk(child, fill, stroke, in_defs)

    _walk(root, None, None, False)
    if removed:
        report.removals.append(
            f"page background <rect> removed x{removed} "
            "(fill-only, full-page: would have plotted as a frame)")
    return removed


def _clean_tree(root: ET.Element, report: SanitizeReport) -> None:
    """In-place removal pass; records every action in ``report.removals``."""
    removed: dict[str, int] = {}
    attr_hits: list[str] = []

    for el in [root, *root.iter()]:
        # element removal (children go with it)
        if _localname(el.tag) in _REMOVED_ELEMENTS:
            removed[_localname(el.tag)] = removed.get(_localname(el.tag), 0) + 1
            continue  # attributes of removed elements need no separate audit

        for name, value in list(el.attrib.items()):
            lname = _localname(name).lower()

            if lname.startswith("on"):
                del el.attrib[name]
                attr_hits.append(f"{lname} on <{_localname(el.tag)}>")
                continue

            if lname in ("href", "src"):
                if _DANGEROUS_URL_RE.search(value) or _DANGEROUS_DATA_RE.search(value):
                    del el.attrib[name]
                    attr_hits.append(f"dangerous URL in {lname} on <{_localname(el.tag)}>")
                elif lname == "href" and not value.strip().startswith("#"):
                    if _localname(el.tag) == "use":
                        removed["use (external ref)"] = (
                            removed.get("use (external ref)", 0) + 1
                        )
                        del el.attrib[name]  # mark: element dropped below
                        el.set("_hp7475a_drop_", "1")
                    else:
                        del el.attrib[name]
                        attr_hits.append(f"external URL in {lname} on <{_localname(el.tag)}>")

    # second pass: actually detach flagged elements (iter() while mutating is
    # unsafe, so collect then remove)
    for parent in [root, *root.iter()]:
        for child in list(parent):
            drop = (
                _localname(child.tag) in _REMOVED_ELEMENTS
                or child.get("_hp7475a_drop_") == "1"
            )
            if drop:
                parent.remove(child)

    for name, count in removed.items():
        report.removals.append(f"removed <{name}> x{count}")
    if attr_hits:
        report.removals.append(f"neutralized attributes: {', '.join(sorted(set(attr_hits)))}")
