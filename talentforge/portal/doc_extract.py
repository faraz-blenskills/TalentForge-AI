"""Text extraction from PDF / DOCX / TXT resumes.

Goals: read the WHOLE document (all pages, tables, text boxes, headers), keep reading order for one- and
two-column layouts, and keep inline bullet glyphs so list items can be split later.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

BULLET = "•"
_CID_RE = re.compile(r"\(cid:\d+\)")
_BAD_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# glyphs that PDFs/Word use for bullets or decoration
_BULLET_GLYPHS = "\x7f●▪■◦‣·➢➤➔➜✓✔✦★►▶○◆◇❖→"
_BULLET_TRANS = {ord(c): BULLET for c in _BULLET_GLYPHS}
_PUA_RE = re.compile("[-]")


def clean_glyphs(text: str) -> str:
    """Turn font-specific bullet glyphs into a normal bullet and remove control characters."""
    text = _CID_RE.sub(BULLET, text)
    text = _PUA_RE.sub(BULLET, text)
    text = text.translate(_BULLET_TRANS)
    text = _BAD_CHARS_RE.sub("", text)
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return text


# ------------------------------------------------------------------------------------------- PDF
def _page_lines(page) -> list[str]:
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False,
                               use_text_flow=False, extra_attrs=["size"])
    if not words:
        return []
    width = float(page.width)

    def line_key(w):
        return round(w["top"] / 3.0)

    # --- decide whether the page has two text columns
    split_x = None
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(line_key(w), []).append(w)
    if len(rows) >= 12:
        best = None
        for x in range(int(width * 0.30), int(width * 0.70), 3):
            crossing = sum(1 for r in rows.values() if any(w["x0"] < x < w["x1"] for w in r))
            if best is None or crossing < best[1]:
                best = (x, crossing)
        if best:
            x, crossing = best
            left = sum(1 for w in words if w["x1"] <= x)
            right = sum(1 for w in words if w["x0"] >= x)
            two_cols = (crossing <= max(1, int(len(rows) * 0.12)) and left >= len(words) * 0.15
                        and right >= len(words) * 0.15)
            # columns must also be vertically substantial on both sides
            if two_cols:
                left_rows = {line_key(w) for w in words if w["x1"] <= x}
                right_rows = {line_key(w) for w in words if w["x0"] >= x}
                if len(left_rows) >= 6 and len(right_rows) >= 6:
                    split_x = x

    def render(ws: list[dict]) -> list[str]:
        by_row: dict[int, list[dict]] = {}
        for w in ws:
            by_row.setdefault(line_key(w), []).append(w)
        out = []
        for k in sorted(by_row):
            row = sorted(by_row[k], key=lambda w: w["x0"])
            txt = row[0]["text"]
            for prev, w in zip(row, row[1:]):
                gap = w["x0"] - prev["x1"]
                txt += ("   " if gap > 0.9 * w["size"] else " ") + w["text"]
            out.append(txt)
        return out

    if split_x is None:
        return render(words)
    full = [w for w in words if w["x0"] < split_x < w["x1"]]
    left = [w for w in words if w not in full and w["x1"] <= split_x + 2]
    right = [w for w in words if w not in full and w not in left]
    # full-width lines that sit above the columns (name / contact) come first
    col_top = min(w["top"] for w in left + right) if (left or right) else 0
    head = [w for w in full if w["top"] <= col_top + 1]
    rest_full = [w for w in full if w not in head]
    return render(head) + render(left + [w for w in rest_full if w["x0"] < split_x]) + render(right)


def extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append("\n".join(_page_lines(page)))
        text = "\n".join(pages)
        if len(text.strip()) >= 40:
            return clean_glyphs(text)
    except Exception:
        pass
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return clean_glyphs("\n".join((p.extract_text() or "") for p in reader.pages))


# ------------------------------------------------------------------------------------------ DOCX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _para_text(p: ET.Element) -> str:
    out: list[str] = []

    def walk(el: ET.Element, in_box: bool = False):
        for ch in el:
            tag = ch.tag
            if tag == _MC + "Fallback":
                continue
            if tag == _W + "txbxContent":
                continue                         # text boxes are emitted as their own paragraphs
            if tag == _W + "t":
                out.append(ch.text or "")
            elif tag == _W + "tab":
                out.append("   ")
            elif tag in (_W + "br", _W + "cr"):
                out.append("\n")
            elif tag == _W + "numPr":
                continue
            else:
                walk(ch)
    walk(p)
    return "".join(out)


def _iter_blocks(el: ET.Element, seen_boxes: set | None = None):
    """Yield text lines from paragraphs, tables, content controls and text boxes in document order."""
    seen_boxes = set() if seen_boxes is None else seen_boxes
    for ch in el:
        tag = ch.tag
        if tag == _MC + "Fallback":
            continue
        if tag == _W + "p":
            # list numbering paragraphs are bullets
            ppr = ch.find(f"./{_W}pPr")
            style = ppr.find(f"./{_W}pStyle") if ppr is not None else None
            style_id = (style.get(f"{_W}val") or "") if style is not None else ""
            is_list = (ppr is not None and ppr.find(f"./{_W}numPr") is not None) or \
                bool(re.match(r"(?i)list(bullet|number|paragraph|continue)", style_id.replace(" ", "")))
            t = _para_text(ch)
            for line in t.split("\n"):
                if line.strip():
                    yield (BULLET + " " + line.strip()) if is_list else line.strip()
            for box in ch.iter(_W + "txbxContent"):
                # Word stores each text box twice (Choice + Fallback); emit every distinct box once
                key = "\n".join(_para_text(p) for p in box.iter(_W + "p"))
                if key and key not in seen_boxes:
                    seen_boxes.add(key)
                    yield from _iter_blocks(box, seen_boxes)
        elif tag == _W + "tbl":
            for tr in ch.findall(f"./{_W}tr"):
                cells = tr.findall(f"./{_W}tc")
                cell_lines = [list(_iter_blocks(tc, seen_boxes)) for tc in cells]
                short = [c for c in cell_lines if c]
                if len(short) > 1 and all(len(c) == 1 and len(c[0]) < 90 for c in short):
                    yield "   ".join(c[0] for c in short)
                else:
                    for c in cell_lines:
                        yield from c
        elif tag in (_W + "sdt", _W + "sdtContent", _W + "body", _W + "customXml", _W + "smartTag"):
            yield from _iter_blocks(ch, seen_boxes)
        elif tag == _W + "txbxContent":
            yield from _iter_blocks(ch, seen_boxes)


def extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        lines: list[str] = []
        # headers often hold the name / contact line
        for n in sorted(x for x in names if re.match(r"word/header\d*\.xml$", x)):
            for ln in _iter_blocks(ET.fromstring(z.read(n))):
                if ln not in lines:
                    lines.append(ln)
        body = ET.fromstring(z.read("word/document.xml"))
        lines += list(_iter_blocks(body))
        for n in sorted(x for x in names if re.match(r"word/footer\d*\.xml$", x)):
            for ln in _iter_blocks(ET.fromstring(z.read(n))):
                if ln not in lines and not re.fullmatch(r"(page )?\d+( of \d+)?", ln.lower()):
                    lines.append(ln)
    return clean_glyphs("\n".join(lines))


# ------------------------------------------------------------------------------------------ entry
def extract_text(data: bytes, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return extract_pdf(data)
    if name.endswith(".docx"):
        return extract_docx(data)
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return clean_glyphs(data.decode(enc))
        except (UnicodeDecodeError, UnicodeError):
            continue
    return clean_glyphs(data.decode("utf-8", errors="ignore"))
