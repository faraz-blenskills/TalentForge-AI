"""DOCX export of a structured resume (ATS-friendly: single column, real headings, no tables)."""
from __future__ import annotations

import io

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor


def _heading(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(text.upper())
    run.bold = True
    run.font.size = Pt(10.5)
    run.font.color.rgb = RGBColor(0x14, 0x14, 0x13)


def _line(doc: Document, left: str, right: str = "", bold: bool = False) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run(left)
    r.bold = bold
    r.font.size = Pt(10.5)
    if right:
        r2 = p.add_run("   " + right)
        r2.font.size = Pt(9.5)
        r2.font.color.rgb = RGBColor(0x69, 0x69, 0x69)


def _bullet(doc: Document, text: str) -> None:
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(0)
    for r in p.runs:
        r.font.size = Pt(10)
    run = p.add_run(text)
    run.font.size = Pt(10)


def resume_to_docx(r: dict) -> bytes:
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(46)
        s.top_margin = s.bottom_margin = Pt(40)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    b = r["basics"]
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(b["name"] or "Your Name")
    run.bold = True
    run.font.size = Pt(20)
    if b["headline"]:
        _line(doc, b["headline"])
    contact = " | ".join(x for x in [b["email"], b["phone"], b["location"], *b["links"]] if x)
    if contact:
        _line(doc, contact)

    if r["summary"]:
        _heading(doc, "Summary")
        doc.add_paragraph(r["summary"])
    if r["skills"]:
        _heading(doc, "Skills")
        doc.add_paragraph(", ".join(r["skills"]))
    if r["experience"]:
        _heading(doc, "Experience")
        for e in r["experience"]:
            dates = " - ".join(x for x in (e["start"], e["end"]) if x)
            _line(doc, " | ".join(x for x in (e["role"], e["company"], e["location"]) if x), dates, bold=True)
            for t in e["bullets"]:
                _bullet(doc, t)
    if r["projects"]:
        _heading(doc, "Projects")
        for pr in r["projects"]:
            _line(doc, pr["name"], pr["description"], bold=True)
            for t in pr["bullets"]:
                _bullet(doc, t)
    if r["education"]:
        _heading(doc, "Education")
        for e in r["education"]:
            _line(doc, " | ".join(x for x in (e["degree"], e["institution"]) if x), e["year"], bold=True)
            if e["details"]:
                doc.add_paragraph(e["details"])
    if r["certifications"]:
        _heading(doc, "Certifications")
        for c in r["certifications"]:
            _bullet(doc, c)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
