"""GoodNotes backup → slides + notes PDF, on a synthetic notebook in the real format."""

import os
import struct
import zipfile

import pytest

import goodnotes_pdf as g

pymupdf = pytest.importorskip("pymupdf")


# ─── A tiny protobuf writer for building notebooks ──────────

def _v(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f(num: int, value) -> bytes:
    if isinstance(value, bool) or isinstance(value, int):
        return _v(num << 3) + _v(value)
    if isinstance(value, float):
        return _v(num << 3 | 5) + struct.pack("<f", value)
    if isinstance(value, str):
        value = value.encode()
    return _v(num << 3 | 2) + _v(len(value)) + value


def msg(*pairs) -> bytes:
    return b"".join(_f(n, v) for n, v in pairs)


def framed(*recs) -> bytes:
    return b"".join(_v(len(r)) + r for r in recs)


def tpl_blob(start, segments, compress=True) -> bytes:
    """GoodNotes stroke geometry: tpl tuple, wrapped in Apple's bv41 (LZ4) or bv4- (raw)."""
    flat = [x for seg in segments for p in seg for x in p]
    body = (struct.pack("<H", 2)
            + struct.pack("<I", 1 + len(segments)) + struct.pack(f"<{1 + len(segments)}H", 0, *[1] * len(segments))
            + struct.pack("<I", 3) + struct.pack("<3f", *start)
            + struct.pack("<I", len(flat)) + struct.pack(f"<{len(flat)}f", *flat))
    sig = b"vA(v)A(u)A(u)\x00"
    data = b"tpl\x00" + struct.pack("<I", 8 + len(sig) + len(body)) + sig + body
    if compress:
        lz4 = pytest.importorskip("lz4.block")
        comp = lz4.compress(data, store_size=False)
        return b"bv41" + struct.pack("<II", len(data), len(comp)) + comp + b"bv4$"
    return b"bv4-" + struct.pack("<I", len(data)) + data + b"bv4$"


RTF = (r"{\rtf1\ansi\ansicpg1252{\fonttbl\f0\fnil HelveticaNeue;}"
       "\n" r"{\colortbl;\red255\green255\blue255;\red3\green70\blue143;}"
       "\n" r"\pard\tx560\partightenfactor0" "\n\n" r"\f0\fs36 \cf2 Klausurrelevant \'97> }")


def uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012X}"


def build_notebook(path, pdf_pages=2, strokes=((100.0, 100.0, 1.5),), text=True, audio=None,
                   color=(0.9, 0.1, 0.1, 1.0), page0=b""):
    """Two slide pages in reverse sort order; page 'slide 2' carries the notes."""
    src = pymupdf.open()
    for n in range(pdf_pages):
        page = src.new_page(width=720, height=540)
        page.insert_text((60, 80), f"Slide {n + 1}", fontsize=40)
    pdf = src.tobytes()
    events = [msg((1, "R-ATT"), (6, msg((1, "ATT"), (2, "PDF1"))))]
    for n in range(pdf_pages):
        size = msg((1, 1320.0), (2, 990.0))
        events.append(msg((1, f"R-BG{n}"), (2, msg((2, f"BG{n}"), (4, "ATT"), (5, n + 1), (8, size)))))
    # page for slide 2 sorts *after* the one for slide 1 although it's logged first
    events.append(msg((1, "R-P1"), (54, msg((2, uuid(10)), (3, msg((1, "BG1"))), (4, msg((1, "46r9402")))))))
    events.append(msg((1, "R-P0"), (54, msg((2, uuid(20)), (3, msg((1, "BG0"))), (4, msg((1, "46r9401")))))))
    elements = []
    for k, start in enumerate(strokes):
        segs = [((start[0] + 50, start[1], 1.5), (start[0] + 100, start[1], 1.5))]
        rgba = msg((1, color[0]), (2, color[1]), (3, color[2]), (4, color[3]))
        el = msg((1, f"S{k}"), (2, tpl_blob(start, segs, compress=k % 2 == 0)), (3, 1), (4, rgba))
        elements += [msg((1, f"S{k}"), (2, msg((1, 1)))), msg((7, el))]
    if text:
        frame = msg((1, msg((1, 500.0), (2, 800.0))), (2, msg((1, 400.0), (2, 43.0))))
        elements += [msg((1, "T0"), (2, msg((1, 1)))), msg((8, msg((1, "T0"), (3, frame), (6, RTF))))]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("index.events.pb", framed(*events))
        z.writestr("attachments/PDF1", pdf)
        z.writestr(f"notes/{uuid(11)}", framed(*elements))       # page id + 1
        z.writestr(f"notes/{uuid(21)}", page0)                     # usually nothing written
        if audio:
            z.writestr(zipfile.ZipInfo(f"attachments/{audio[0]}", audio[2]), audio[1])


def test_lz4_block_matches_reference():
    lz4 = pytest.importorskip("lz4.block")
    for data in (b"abc" * 1000, os.urandom(3000) + b"x" * 500, bytes(range(256)) * 20):
        assert g.lz4_block(lz4.compress(data, store_size=False), len(data)) == data


def test_read_notebook_order_slides_notes(tmp_path):
    nb = str(tmp_path / "VL.goodnotes")
    build_notebook(nb, strokes=((100.0, 100.0, 1.5), (100.0, 300.0, 2.0)))
    pages = g.read_notebook(nb)
    assert [p.pdf_page for p in pages] == [1, 2]                  # by sort key, not log order
    assert (pages[0].width, pages[0].height) == (1320.0, 990.0)
    assert pages[0].strokes == [] and pages[0].texts == []
    s = pages[1].strokes
    assert len(s) == 2 and s[0].start == pytest.approx((100.0, 100.0, 1.5))
    assert s[0].segments[0][1] == pytest.approx((200.0, 100.0, 1.5))
    assert s[0].color == pytest.approx((0.9, 0.1, 0.1)) and not s[0].highlighter
    t = pages[1].texts[0]
    assert t.text == "Klausurrelevant —>" and t.size == 18 and t.color == pytest.approx((3 / 255, 70 / 255, 143 / 255))
    assert t.rect == pytest.approx((500.0, 800.0, 900.0, 843.0))


def test_render_draws_slides_strokes_and_text(tmp_path):
    nb, out = str(tmp_path / "VL.goodnotes"), str(tmp_path / "VL.pdf")
    build_notebook(nb)
    stats = g.notebook_to_pdf(nb, out)
    assert stats == {"pages": 2, "strokes": 1, "texts": 1, "skipped": 0}
    doc = pymupdf.open(out)
    assert doc.page_count == 2 and doc[0].rect.width == 1320
    assert "Slide 1" in doc[0].get_text() and "Slide 2" in doc[1].get_text()
    assert "Klausurrelevant" in doc[1].get_text()
    red = [d for d in doc[1].get_drawings() if d.get("color") and d["color"][0] > 0.8 and d["color"][1] < 0.2]
    assert red and red[0]["width"] == pytest.approx(3.0)            # half-widths 1.5 → 3 pt
    assert not [d for d in doc[0].get_drawings() if d.get("color") == red[0]["color"]]


def test_highlighter_is_translucent(tmp_path):
    nb = str(tmp_path / "HL.goodnotes")
    build_notebook(nb, text=False, color=(1.0, 0.9, 0.0, 0.5))
    stroke = g.read_notebook(nb)[1].strokes[0]
    assert stroke.highlighter and stroke.opacity <= 0.35


def test_unknown_elements_are_skipped_not_fatal(tmp_path):
    nb = str(tmp_path / "X.goodnotes")
    build_notebook(nb, text=False,
                   page0=framed(msg((1, "I0"), (2, msg((1, 1)))), msg((12, msg((1, "I0"))))))   # e.g. an image
    pages = g.read_notebook(nb)
    assert pages[0].skipped == 1
    assert g.notebook_to_pdf(nb, str(tmp_path / "x.pdf"))["skipped"] == 1


def test_rtf_text():
    text, size, color = g.rtf_text(RTF)
    assert text == "Klausurrelevant —>" and size == 18 and color[2] > 0.5
