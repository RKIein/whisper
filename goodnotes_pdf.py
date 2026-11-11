"""
GoodNotes → PDF — the slides of a GoodNotes notebook with your handwriting on top.

Reads a GoodNotes auto-backup (.goodnotes = zip, read-only) and draws it as a PDF.
The format isn't documented; this is what Robin's backups (Nov 2026) contain:

  index.events.pb   log of length-prefixed protobuf records. Kinds used here:
                      2  page background: id(2), attachment ref(4), PDF page no.(5), size(8)
                      6  attachment: ref(1) → file id(2) in attachments/
                     54  page: notes file id(2), background ref(3.1), sort key(4.1, "46r9401")
  attachments/<id>  the slide PDF (and the audio recordings)
  notes/<page>      the page's elements, same record framing; empty = nothing written
                      7  stroke: colour RGBA(4), tool(3), geometry(2) = Apple “bv41”
                         (LZ4) → “tpl” tuple: start point + quadratic segments of
                         (x, y, half-width), plus a precomputed outline (not needed)
                      8  text box: frame(3) = origin + size, text(6) = RTF

Draws pen strokes (width from the stroke itself), highlighter (translucent), text
boxes and the slide behind each page. Element kinds it doesn't know (shapes, images,
stickers) are skipped and counted. Pure Python apart from PyMuPDF for drawing.
"""

import html
import re
import struct
import zipfile
from dataclasses import dataclass, field

# ─── Protobuf wire format (no schema) ───────────────────────


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def fields(b: bytes) -> list[tuple[int, object]]:
    """[(field number, value)] — value is int, float (fixed32) or bytes."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        f, t = key >> 3, key & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 5:
            v = struct.unpack("<f", b[i:i + 4])[0]
            i += 4
        elif t == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]
            i += 8
        elif t == 2:
            n, i = _varint(b, i)
            v = b[i:i + n]
            i += n
        else:
            raise ValueError(f"wire type {t}")
        out.append((f, v))
    return out


def first(b: bytes, num: int, default=None):
    for f, v in fields(b):
        if f == num:
            return v
    return default


def records(b: bytes):
    """Length-prefixed records (the framing of *.pb and notes/* files)."""
    i = 0
    while i < len(b):
        n, j = _varint(b, i)
        yield b[j:j + n]
        i = j + n


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


# ─── Apple compression (“bv41” = LZ4 block) ─────────────────

def lz4_block(src: bytes, size: int) -> bytes:
    """Decompress one raw LZ4 block."""
    dst, i = bytearray(), 0
    while i < len(src):
        token = src[i]
        i += 1
        n = token >> 4
        if n == 15:
            while True:
                b = src[i]
                i += 1
                n += b
                if b != 255:
                    break
        dst += src[i:i + n]
        i += n
        if i >= len(src):
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        m = token & 15
        if m == 15:
            while True:
                b = src[i]
                i += 1
                m += b
                if b != 255:
                    break
        m += 4
        start = len(dst) - offset
        if offset >= m:
            dst += dst[start:start + m]
        else:                                   # overlapping copy
            for k in range(m):
                dst.append(dst[start + k])
    if len(dst) != size:
        raise ValueError(f"LZ4: {len(dst)} bytes, expected {size}")
    return bytes(dst)


def apple_decompress(blob: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(blob):
        tag = blob[i:i + 4]
        if tag == b"bv41":
            raw, comp = struct.unpack("<II", blob[i + 4:i + 12])
            out += lz4_block(blob[i + 12:i + 12 + comp], raw)
            i += 12 + comp
        elif tag == b"bv4-":
            raw = struct.unpack("<I", blob[i + 4:i + 8])[0]
            out += blob[i + 8:i + 8 + raw]
            i += 8 + raw
        elif tag == b"bv4$":
            break
        else:
            raise ValueError(f"unsupported compression {tag!r}")
    return bytes(out)


def tpl(data: bytes) -> list:
    """GoodNotes' ‘tpl’ tuple: a type signature like 'vA(v)A(u)…', then the values.
    v = uint16, u = float32, A(x) = uint32 count + items."""
    if data[:4] != b"tpl\x00":
        raise ValueError("not a tpl blob")
    end = data.index(b"\x00", 8)
    body, o, out = data[end + 1:], 0, []
    for arr, scalar in re.findall(r"A\((.)\)|(.)", data[8:end].decode("ascii")):
        if scalar:
            out.append(struct.unpack("<H", body[o:o + 2])[0])
            o += 2
            continue
        n = struct.unpack("<I", body[o:o + 4])[0]
        o += 4
        size, code = (2, "H") if arr == "v" else (4, "f")
        out.append(list(struct.unpack(f"<{n}{code}", body[o:o + size * n])))
        o += size * n
    return out


# ─── Notebook model ─────────────────────────────────────────

@dataclass
class Stroke:
    start: tuple                     # (x, y, half-width)
    segments: list                   # [(ctrl, end)], each (x, y, half-width)
    color: tuple = (0, 0, 0)
    opacity: float = 1.0
    highlighter: bool = False


@dataclass
class TextBox:
    rect: tuple                      # x0, y0, x1, y1
    text: str
    size: float = 12.0
    color: tuple = (0, 0, 0)


@dataclass
class Page:
    width: float
    height: float
    pdf: str | None = None           # attachments/<id> holding the slide
    pdf_page: int = 0                # 1-based
    strokes: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    skipped: int = 0                 # elements of kinds not drawn


TOOL_PEN = 1


def parse_stroke(el: bytes) -> Stroke:
    f = dict(fields(el))
    geom = tpl(apple_decompress(f[2]))
    start, flat = geom[2], geom[3]
    pts = [tuple(flat[k:k + 3]) for k in range(0, len(flat) - len(flat) % 3, 3)]
    segments = list(zip(pts[0::2], pts[1::2]))
    rgba = dict(fields(f[4])) if isinstance(f.get(4), bytes) else {}
    color = tuple(float(rgba.get(k, 0.0)) for k in (1, 2, 3))
    alpha = float(rgba.get(4, 1.0))
    tool = f.get(3, TOOL_PEN)
    highlighter = tool != TOOL_PEN or alpha < 0.99
    return Stroke(tuple(start[:3]), segments, color,
                  opacity=min(alpha, 0.35) if highlighter else alpha, highlighter=highlighter)


_RTF_COLOR = re.compile(r"\\red(\d+)\\green(\d+)\\blue(\d+)")


def rtf_text(rtf: str) -> tuple[str, float, tuple]:
    """(plain text, font size in pt, colour) from GoodNotes' RTF."""
    colors = [(0, 0, 0)] + [tuple(int(c) / 255 for c in m) for m in _RTF_COLOR.findall(rtf)]
    size = re.search(r"\\fs(\d+)", rtf)
    cf = re.search(r"\\cf(\d+)", rtf)
    body = re.sub(r"\{\\(fonttbl|colortbl|\*)[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", rtf)
    body = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), body)
    body = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), body)
    body = body.replace("\\\n", "\n")
    body = re.sub(r"\\par(?![a-zA-Z])", "\n", body)
    body = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", body)
    body = body.replace("{", "").replace("}", "").replace("\\", "")
    text = "\n".join(line.strip() for line in body.strip().splitlines()).strip()
    color = colors[int(cf.group(1))] if cf and int(cf.group(1)) < len(colors) else (0, 0, 0)
    return text, (int(size.group(1)) / 2 if size else 12.0), color


def parse_textbox(el: bytes) -> TextBox | None:
    f = dict(fields(el))
    frame = f.get(3) or f.get(2)
    if not isinstance(frame, bytes) or not isinstance(f.get(6), bytes):
        return None
    fr = dict(fields(frame))
    origin, size = dict(fields(fr[1])), dict(fields(fr[2]))
    x, y = float(origin.get(1, 0)), float(origin.get(2, 0))
    w, h = float(size.get(1, 0)), float(size.get(2, 0))
    text, pt, color = rtf_text(_text(f[6]))
    return TextBox((x, y, x + w, y + h), text, pt, color) if text else None


def page_elements(data: bytes, page: Page):
    """Strokes and text boxes of one notes/<page> file (last version of each element wins)."""
    latest: dict[str, tuple[int, bytes]] = {}
    order = []
    for rec in records(data):
        for f, v in fields(rec):
            if not isinstance(v, bytes) or f in (1, 2):
                continue
            el_id = _text(first(v, 1, b"")) or f"#{len(order)}"
            if el_id not in latest:
                order.append(el_id)
            latest[el_id] = (f, v)
    for el_id in order:
        kind, el = latest[el_id]
        try:
            if kind == 7:
                page.strokes.append(parse_stroke(el))
            elif kind == 8:
                tb = parse_textbox(el)
                if tb:
                    page.texts.append(tb)
            else:
                page.skipped += 1
        except Exception:
            page.skipped += 1


def _uuid_plus_one(u: str) -> str:
    """A page's notes file is named after the page id + 1 (…F42 → notes/…F43)."""
    try:
        h = f"{int(u.replace('-', ''), 16) + 1:032X}"
    except ValueError:
        return u
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def read_notebook(path: str) -> list[Page]:
    """The notebook's pages in order, with slide reference, strokes and text boxes."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        events = z.read("index.events.pb") if "index.events.pb" in names else b""
        backgrounds, attachments, pages = {}, {}, {}
        for rec in records(events):
            for kind, body in fields(rec):
                if kind == 1 or not isinstance(body, bytes):
                    continue
                f = dict(fields(body))
                if kind == 2:
                    size = dict(fields(f[8])) if isinstance(f.get(8), bytes) else {}
                    backgrounds[_text(f.get(2))] = (_text(f.get(4)), int(f.get(5, 0) or 0),
                                                    float(size.get(1, 0)), float(size.get(2, 0)))
                elif kind == 6:
                    attachments[_text(f.get(1))] = _text(f.get(2))
                elif kind == 54:
                    bg = _text(first(f[3], 1, b"")) if isinstance(f.get(3), bytes) else ""
                    key = _text(first(f[4], 1, b"")) if isinstance(f.get(4), bytes) else ""
                    pages[_text(f.get(2))] = (key, bg)
                break                                       # one kind per record
        sizes = [(w, h) for _, _, w, h in backgrounds.values() if w and h]
        default = max(set(sizes), key=sizes.count) if sizes else (612.0, 792.0)
        out = []
        for page_id, (key, bg) in sorted(pages.items(), key=lambda kv: kv[1][0]):
            ref, pno, w, h = backgrounds.get(bg, ("", 0, 0, 0))
            file_id = attachments.get(ref, "")
            pdf = f"attachments/{file_id}" if file_id and f"attachments/{file_id}" in names else None
            page = Page(w or default[0], h or default[1], pdf, pno)
            for member in (f"notes/{_uuid_plus_one(page_id)}", f"notes/{page_id}"):
                if member in names:
                    data = z.read(member)
                    if data:
                        page_elements(data, page)
                    break
            out.append(page)
        return out


# ─── Drawing ────────────────────────────────────────────────

def _cubic(p0, c, p1):
    """Quadratic Bézier (p0, c, p1) → cubic control points."""
    c1 = (p0[0] + 2 / 3 * (c[0] - p0[0]), p0[1] + 2 / 3 * (c[1] - p0[1]))
    c2 = (p1[0] + 2 / 3 * (c[0] - p1[0]), p1[1] + 2 / 3 * (c[1] - p1[1]))
    return c1, c2


def render(pages: list[Page], zip_path: str, out_pdf: str) -> dict:
    """Draw the pages into out_pdf. Returns counts: pages, strokes, texts, skipped."""
    import pymupdf

    doc = pymupdf.open()
    sources: dict[str, object] = {}
    stats = {"pages": 0, "strokes": 0, "texts": 0, "skipped": 0}
    with zipfile.ZipFile(zip_path) as z:
        for p in pages:
            page = doc.new_page(width=p.width, height=p.height)
            if p.pdf:
                if p.pdf not in sources:
                    sources[p.pdf] = pymupdf.open(stream=z.read(p.pdf), filetype="pdf")
                src = sources[p.pdf]
                if 1 <= p.pdf_page <= src.page_count:
                    page.show_pdf_page(page.rect, src, p.pdf_page - 1)
            for s in p.strokes:
                shape = page.new_shape()
                if not s.segments:                       # a dot
                    shape.draw_circle(pymupdf.Point(s.start[0], s.start[1]), max(s.start[2], 0.5))
                    shape.finish(color=s.color, fill=s.color, width=0,
                                 stroke_opacity=s.opacity, fill_opacity=s.opacity)
                prev = s.start
                for c, end in s.segments:
                    c1, c2 = _cubic(prev, c, end)
                    shape.draw_bezier(pymupdf.Point(prev[0], prev[1]), pymupdf.Point(*c1),
                                      pymupdf.Point(*c2), pymupdf.Point(end[0], end[1]))
                    width = max((prev[2] + end[2]), 0.3)  # half-widths → full width
                    shape.finish(color=s.color, width=width, closePath=False,
                                 lineCap=0 if s.highlighter else 1, lineJoin=1,
                                 stroke_opacity=s.opacity)
                    prev = end
                shape.commit()
            for t in p.texts:                            # shrinks to fit, any Unicode
                r, g, b = (round(c * 255) for c in t.color)
                page.insert_htmlbox(pymupdf.Rect(*t.rect), html.escape(t.text).replace("\n", "<br>"),
                                    css=f"* {{font-family: sans-serif; font-size: {t.size}px; "
                                        f"color: rgb({r},{g},{b});}}")
            stats["pages"] += 1
            stats["strokes"] += len(p.strokes)
            stats["texts"] += len(p.texts)
            stats["skipped"] += p.skipped
    doc.save(out_pdf, garbage=3, deflate=True)
    doc.close()
    for src in sources.values():
        src.close()
    return stats


def notebook_to_pdf(zip_path: str, out_pdf: str) -> dict:
    return render(read_notebook(zip_path), zip_path, out_pdf)
