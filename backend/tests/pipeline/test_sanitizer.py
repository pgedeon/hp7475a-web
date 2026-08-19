"""Sanitizer tests — BUILD_SPEC §14/§36: every malicious fixture neutralized."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.services.pipeline.sanitizer import sanitize_svg
from conftest import svg_bytes


def _report(name: str, **kw):
    return sanitize_svg(svg_bytes(name), **kw)


def test_benign_passes_clean():
    clean, report = _report("benign.svg")
    assert report.ok
    assert report.removals == []
    root = ET.fromstring(clean)
    assert root.tag.endswith("svg")


@pytest.mark.parametrize("name", ["malicious-script.svg", "malicious-foreignobject.svg"])
def test_dangerous_elements_removed_and_reported(name):
    clean, report = _report(name)
    assert report.ok
    assert report.removals, "removals must list what was neutralized"
    joined = " ".join(report.removals).lower()
    assert "script" in joined or "foreignobject" in joined
    root = ET.fromstring(clean)
    locals_ = [el.tag.rsplit("}", 1)[-1] for el in root.iter()]
    assert "script" not in locals_ and "foreignObject" not in locals_


def test_event_handlers_stripped():
    clean, report = _report("malicious-onclick.svg")
    assert report.ok
    joined = " ".join(report.removals).lower()
    assert "onclick" in joined
    root = ET.fromstring(clean)
    for el in root.iter():
        assert not any(a.lower().startswith("on") for a in el.attrib)


def test_javascript_and_data_text_html_urls_neutralized():
    clean, report = _report("malicious-javascript-href.svg")
    assert report.ok
    joined = " ".join(report.removals).lower()
    assert "dangerous url" in joined
    root = ET.fromstring(clean)
    for el in root.iter():
        for attr in el.attrib.values():
            v = attr.strip().lower()
            assert not v.startswith("javascript:")
            assert "text/html" not in v


def test_xxe_doctype_rejected():
    clean, report = _report("malicious-xxe-doctype.svg")
    assert report.rejected
    assert clean == b""
    assert any("entity" in r.lower() for r in report.reasons)


def test_external_use_removed():
    clean, report = _report("malicious-external-use.svg")
    assert report.ok
    joined = " ".join(report.removals).lower()
    assert "use (external ref)" in joined
    root = ET.fromstring(clean)
    for el in root.iter():
        if el.tag.endswith("use"):
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
            assert href.strip().startswith("#"), "external <use> must be gone"


def test_oversized_rejected():
    clean, report = _report("benign.svg", max_bytes=10)
    assert report.rejected
    assert clean == b""


def test_unparseable_rejected_fail_closed():
    for junk in (b"", b"not xml at all", b"<svg><unclosed>", b"\x00\x01\x02"):
        clean, report = sanitize_svg(junk)
        assert report.rejected, junk
        assert clean == b""


def test_doctype_without_entities_stripped():
    data = (
        b'<?xml version="1.0"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm">'
        b'<rect x="1" y="1" width="4" height="4" fill="none" stroke="black"/></svg>'
    )
    clean, report = sanitize_svg(data)
    assert report.ok
    assert any("DOCTYPE" in r for r in report.removals)
    assert b"DOCTYPE" not in clean


def test_non_svg_root_rejected():
    clean, report = sanitize_svg(b"<html><body>x</body></html>")
    assert report.rejected
    assert any("root" in r.lower() for r in report.reasons)


# -- page-background stripping (2026-08-19 user report: frame around plots) --

_BG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210">'
       "%s</svg>")


def test_white_background_rect_stripped():
    svg = _BG % ('<rect x="0" y="0" width="297" height="210" fill="white"/>'
                 '<polyline points="10,10 50,50"/>')
    clean, report = sanitize_svg(svg.encode())
    assert report.ok
    assert any("page background" in r for r in report.removals)
    assert b"fill=\"white\"" not in clean
    assert b"polyline" in clean  # real geometry survives


def test_style_fill_background_stripped():
    svg = _BG % ('<rect width="297" height="210" '
                 'style="fill:#ffffff;fill-opacity:1"/>'
                 '<circle cx="50" cy="50" r="10"/>')
    clean, report = sanitize_svg(svg.encode())
    assert any("page background" in r for r in report.removals)
    assert b"circle" in clean


def test_visible_border_in_stroked_group_survives():
    svg = _BG % ('<rect x="0" y="0" width="297" height="210" fill="white"/>'
                 '<g fill="none" stroke="black">'
                 '<rect x="10" y="10" width="277" height="190"/></g>')
    clean, report = sanitize_svg(svg.encode())
    assert any("page background" in r for r in report.removals)
    # the stroked group border must survive (inheritance-aware)
    assert b'width="277"' in clean


def test_partial_fill_only_rect_survives():
    svg = _BG % ('<rect x="100" y="100" width="50" height="30" fill="red"/>'
                 '<polyline points="1,1 2,2"/>')
    clean, report = sanitize_svg(svg.encode())
    assert not any("page background" in r for r in report.removals)
    assert b'width="50"' in clean


def test_stroked_full_page_rect_survives():
    svg = _BG % ('<rect x="0" y="0" width="297" height="210" '
                 'fill="none" stroke="black"/>')
    clean, report = sanitize_svg(svg.encode())
    assert not any("page background" in r for r in report.removals)
    assert b'width="297"' in clean


def test_no_viewbox_no_strip():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="297mm" '
           'height="210mm"><rect x="0" y="0" width="297" height="210" '
           'fill="white"/></svg>')
    clean, report = sanitize_svg(svg.encode())
    assert not any("page background" in r for r in report.removals)


def test_strip_is_idempotent():
    svg = _BG % ('<rect x="0" y="0" width="297" height="210" fill="white"/>'
                 '<polyline points="10,10 50,50"/>')
    once, _ = sanitize_svg(svg.encode())
    twice, report2 = sanitize_svg(once)
    assert not any("page background" in r for r in report2.removals)
    assert once == twice
