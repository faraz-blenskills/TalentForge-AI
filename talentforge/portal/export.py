"""DOCX export of a structured resume.

ATS-friendly by construction: one column, real paragraphs and list bullets, no tables, no text boxes, no
images. Templates only change font, colour, alignment and heading style.
"""
from __future__ import annotations

import io

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from . import styles


def _rgb(hex_: str) -> RGBColor:
    r, g, b = styles.hex_to_rgb(hex_)
    return RGBColor(r, g, b)


def _border(paragraph, color_hex: str, size: int = 6, side: str = "bottom", space: int = 1) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color_hex.lstrip("#").upper())
    pbdr.append(el)


def _spacing(run, twentieths: int) -> None:
    rpr = run._r.get_or_add_rPr()
    sp = OxmlElement("w:spacing")
    sp.set(qn("w:val"), str(twentieths))
    rpr.append(sp)


class _Style:
    def __init__(self, template_id: str, accent: str):
        self.id = template_id
        self.accent = accent
        self.font = styles.TEMPLATES[template_id]["font"]
        self.centered = template_id == "classic"

    def heading(self, doc: Document, text: str) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(11)
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text.upper())
        run.bold = True
        run.font.name = self.font
        if self.id == "classic":
            run.font.size = Pt(11)
            run.font.color.rgb = _rgb("#141413")
            _border(p, self.accent, 6)
        elif self.id == "modern":
            run.font.size = Pt(11.5)
            run.font.color.rgb = _rgb(self.accent)
            _border(p, self.accent, 8)
        else:                                   # executive
            run.font.size = Pt(10)
            run.font.color.rgb = _rgb(self.accent)
            _spacing(run, 30)
            _border(p, self.accent, 4)


def _line(doc: Document, st: _Style, left: str, right: str = "", bold: bool = False, size: float = 10.5,
          color: str | None = None, italic: bool = False) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run(left)
    r.bold = bold
    r.italic = italic
    r.font.size = Pt(size)
    r.font.name = st.font
    if color:
        r.font.color.rgb = _rgb(color)
    if right:
        r2 = p.add_run("    " + right)
        r2.font.size = Pt(size - 1)
        r2.font.name = st.font
        r2.font.color.rgb = _rgb("#555555")


def _bullet(doc: Document, st: _Style, text: str) -> None:
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(0)
    run = p.add_run(text)
    run.font.size = Pt(10)
    run.font.name = st.font


def resume_to_docx(r: dict, template: str = "modern", accent: str = "") -> bytes:
    tid, acc = styles.resolve(template, accent)
    st = _Style(tid, acc)
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(46 if tid != "executive" else 40)
        s.top_margin = s.bottom_margin = Pt(40)
    normal = doc.styles["Normal"]
    normal.font.name = st.font
    normal.font.size = Pt(10.5)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), st.font)

    b = r["basics"]
    p = doc.add_paragraph()
    if st.centered:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if tid == "executive":
        _border(p, acc, 24, side="top", space=6)
    name = b["name"] or "Your Name"
    run = p.add_run(name.upper() if tid == "executive" else name)
    run.bold = True
    run.font.name = st.font
    run.font.size = Pt({"classic": 22, "modern": 24, "executive": 20}[tid])
    if tid != "classic":
        run.font.color.rgb = _rgb(acc)
    if tid == "executive":
        _spacing(run, 20)
    p.paragraph_format.space_after = Pt(2)

    def centered(par):
        if st.centered:
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        return par

    if b["headline"]:
        _line(doc, st, b["headline"], size=11.5, color="#444444", italic=tid == "classic")
        centered(doc.paragraphs[-1])
    contact = "  |  ".join(x for x in [b["email"], b["phone"], b["location"], *b["links"]] if x)
    if contact:
        _line(doc, st, contact, size=10, color="#444444")
        centered(doc.paragraphs[-1])

    if r["summary"]:
        st.heading(doc, "Summary")
        para = doc.add_paragraph(r["summary"])
        para.paragraph_format.space_after = Pt(0)
    if r["skills"]:
        st.heading(doc, "Skills")
        para = doc.add_paragraph(", ".join(r["skills"]))
        para.paragraph_format.space_after = Pt(0)
    if r["experience"]:
        st.heading(doc, "Experience")
        for e in r["experience"]:
            dates = " - ".join(x for x in (e["start"], e["end"]) if x)
            _line(doc, st, " | ".join(x for x in (e["role"], e["company"], e["location"]) if x), dates, bold=True)
            for t in e["bullets"]:
                _bullet(doc, st, t)
    if r["projects"]:
        st.heading(doc, "Projects")
        for pr in r["projects"]:
            _line(doc, st, pr["name"], pr["description"], bold=True)
            for t in pr["bullets"]:
                _bullet(doc, st, t)
    if r["education"]:
        st.heading(doc, "Education")
        for e in r["education"]:
            _line(doc, st, " | ".join(x for x in (e["degree"], e["institution"]) if x), e["year"], bold=True)
            if e["details"]:
                _line(doc, st, e["details"], size=10, color="#444444")
    if r["certifications"]:
        st.heading(doc, "Certifications")
        for c in r["certifications"]:
            _bullet(doc, st, c)
    for ex in r.get("extras", []):
        if ex["items"]:
            st.heading(doc, ex["title"])
            for t in ex["items"]:
                _bullet(doc, st, t)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
