"""Whole-document resume parser.

Reads every line of the extracted text, recognises section headings in many spellings, and returns a
structured resume plus a parse report. Nothing is silently dropped: content that does not fit a standard
section is kept as an "extra" section so it still appears in the preview.
"""
from __future__ import annotations

import re

BUL = "•"

# --------------------------------------------------------------------------------------- headings
SECTION_PHRASES: dict[str, list[str]] = {
    "summary": [
        "summary", "professional summary", "career summary", "executive summary", "profile",
        "professional profile", "personal profile", "career objective", "objective", "professional objective",
        "about me", "about", "personal statement", "overview", "summary of qualifications", "career profile"],
    "skills": [
        "skills", "technical skills", "core skills", "key skills", "core competencies", "competencies",
        "technologies", "technical proficiency", "technical expertise", "tools", "tools & technologies",
        "tech stack", "skill set", "areas of expertise", "expertise", "technical summary", "skills & tools",
        "skills summary", "it skills", "programming skills", "soft skills", "technical competencies",
        "software skills", "key competencies", "skills & abilities", "professional skills"],
    "experience": [
        "experience", "work experience", "professional experience", "employment", "employment history",
        "work history", "career history", "internship", "internships", "internship experience",
        "industrial experience", "industrial training", "relevant experience", "professional background",
        "experience summary", "work experience & internships", "internships & training", "practical experience",
        "work & internship experience", "training & internships", "employment record"],
    "education": [
        "education", "educational qualifications", "educational qualification", "academic qualifications",
        "academic qualification", "academics", "academic background", "academic details", "qualifications",
        "education & qualifications", "education details", "educational background", "scholastic",
        "academic profile", "educational details", "education & training"],
    "projects": [
        "projects", "academic projects", "personal projects", "key projects", "project experience",
        "project work", "projects undertaken", "selected projects", "technical projects", "notable projects",
        "major projects", "mini projects", "projects & research", "project details", "projects & achievements"],
    "certifications": [
        "certifications", "certification", "certificates", "licenses", "licences", "courses", "training",
        "certifications & training", "courses & certifications", "professional development",
        "training & certifications", "certifications & courses", "online courses", "licenses & certifications",
        "certifications & achievements", "workshops", "trainings", "training & workshops",
        "certifications & licenses", "courses & training"],
    "extras": [
        "achievements", "accomplishments", "awards", "honors", "honours", "awards & achievements",
        "achievements & awards", "recognition", "scholarships", "extracurricular", "extracurricular activities",
        "extra curricular activities", "activities", "leadership", "positions of responsibility", "volunteering",
        "volunteer experience", "co-curricular activities", "hobbies", "interests", "hobbies & interests",
        "languages", "language proficiency", "publications", "research", "patents", "references",
        "declaration", "personal details", "personal information", "additional information", "other",
        "achievements & activities", "leadership & activities", "strengths", "affiliations", "memberships",
        "responsibilities", "community involvement", "key achievements", "accomplishments & awards"],
}
_PHRASE_TO_SECTION = {p: k for k, v in SECTION_PHRASES.items() for p in v}
_WORD_TO_SECTION: dict[str, str] = {}
for _k in ("summary", "skills", "experience", "education", "projects", "certifications", "extras"):
    for _p in SECTION_PHRASES[_k]:
        for _w in re.findall(r"[a-z]+", _p):
            _WORD_TO_SECTION.setdefault(_w, _k)
# generic connective words allowed inside a heading
_FILLER = {"and", "of", "the", "in", "for", "&", "/", "details", "detail", "background", "summary", "history",
           "professional", "personal", "technical", "key", "core", "relevant", "academic", "additional"}
# words that decide the section when a heading mixes words from several sections
_SECTION_WORD_PRIORITY = [
    ("experience", "experience"), ("internship", "experience"), ("internships", "experience"),
    ("employment", "experience"), ("projects", "projects"), ("project", "projects"),
    ("education", "education"), ("academic", "education"), ("academics", "education"),
    ("qualifications", "education"), ("qualification", "education"),
    ("certifications", "certifications"), ("certification", "certifications"), ("certificates", "certifications"),
    ("courses", "certifications"), ("training", "certifications"), ("licenses", "certifications"),
    ("skills", "skills"), ("competencies", "skills"), ("technologies", "skills"), ("tools", "skills"),
    ("expertise", "skills"), ("summary", "summary"), ("objective", "summary"), ("profile", "summary"),
]
# labels that may start a heading line followed by content ("SKILLS: Python, SQL")
_INLINE_OK = {"skills", "technical skills", "core skills", "key skills", "summary", "professional summary",
              "objective", "career objective", "profile", "languages", "hobbies", "interests", "declaration",
              "soft skills", "it skills", "programming skills", "skill set", "technical proficiency"}

_BULLET_START_RE = re.compile(r"^\s*(?:[•\*–—>»~]|-(?=\s)|\d{1,2}[.)](?=\s)|[a-z]\)(?=\s))\s*")
_WS_RE = re.compile(r"[ \t]+")


def strip_bullet(line: str) -> str:
    prev = None
    while prev != line:
        prev = line
        line = _BULLET_START_RE.sub("", line, count=1)
    return line.strip()


def is_bullet(line: str) -> bool:
    return bool(_BULLET_START_RE.match(line)) and bool(strip_bullet(line))


def _norm_heading(s: str) -> str:
    s = s.lower().replace("&amp;", "&")
    s = re.sub(r"[^a-z&/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def classify_heading(line: str, allow_inline: bool = True) -> tuple[str, str, str] | None:
    """Return (section_key, display_title, remainder) when `line` is a section heading."""
    raw = line.strip()
    if raw and is_bullet(raw):
        rest = strip_bullet(raw)
        if rest and rest.isupper() and len(rest) >= 4:      # "• EDUCATION" style headings
            raw = rest
        else:
            return None
    # trailing metadata after a wide gap: "Work Experience   3 mos", "Projects   (4)"
    gap = re.split(r"\s{3,}", raw)
    if len(gap) >= 2 and all(re.fullmatch(r"\(?\d+\)?|\d+\s*(?:yrs?|years?|mos?|months?)\b.*", g.strip(), re.I)
                             for g in gap[1:]):
        raw = gap[0].strip()
    if not raw or len(raw) > 90 or is_bullet(raw):
        return None
    inline_rest = ""
    head = raw
    if ":" in raw and allow_inline:
        left, right = raw.split(":", 1)
        if right.strip() and _norm_heading(left) in _INLINE_OK and len(left.split()) <= 4:
            head, inline_rest = left, right.strip()
        elif right.strip():
            return None
    head = head.strip(" :-–—_=*#|.")
    words = head.split()
    if not words or len(words) > 7 or re.search(r"\d", head) or "@" in head:
        return None
    norm = _norm_heading(head)
    if not norm:
        return None
    toks = [t for t in re.split(r"[ ]+", norm) if t]
    # case guard: headings are UPPER, Title Case, or end with ':' / are short
    letters = [w for w in re.findall(r"[A-Za-z]+", head)]
    is_upper = head.isupper()
    is_title = all(w[0].isupper() for w in letters if len(w) > 2) if letters else False
    if not (is_upper or is_title or raw.endswith(":") or len(words) <= 2):
        return None
    if norm in _PHRASE_TO_SECTION:
        return _PHRASE_TO_SECTION[norm], head.title() if is_upper else head, inline_rest
    vocab_ok = all(t in _WORD_TO_SECTION or t in _FILLER for t in toks)
    if vocab_ok and any(t in _WORD_TO_SECTION and t not in _FILLER for t in toks):
        for w, sec in _SECTION_WORD_PRIORITY:
            if w in toks:
                return sec, head.title() if is_upper else head, inline_rest
        first = next(t for t in toks if t in _WORD_TO_SECTION)
        return _WORD_TO_SECTION[first], head.title() if is_upper else head, inline_rest
    return None


# --------------------------------------------------------------------------------------- dates
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE = (rf"(?:{_MONTH}\.?,?\s*['’]?\s*(?:19|20)?\d{{2}}|\d{{1,2}}\s*[/.\-]\s*(?:19|20)\d{{2}}|"
         rf"(?:19|20)\d{{2}})")
_END = rf"(?:{_DATE}|present|current|currently|now|ongoing|till\s+date|to\s+date|till\s+now|today|date)"
RANGE_RE = re.compile(rf"(?P<s>{_DATE})\s*(?:-|–|—|\bto\b|\buntil\b|\btill\b)\s*(?P<e>{_END})", re.I)
SINGLE_DATE_RE = re.compile(rf"\b(?P<d>{_DATE})\b", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s\-.]?)?(?:\(?\d{2,5}\)?[\s\-.]?)?\d{3,5}[\s\-.]?\d{3,5}(?!\d)")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:[a-z0-9\-]+\.)+(?:com|in|io|dev|net|org|me|ai|co|app)(?:/[^\s,|;]*)?", re.I)

ROLE_RE = re.compile(
    r"\b(engineer|developer|intern|internship|analyst|scientist|manager|lead|head|director|consultant|associate|"
    r"executive|officer|trainee|apprentice|architect|administrator|designer|specialist|coordinator|assistant|"
    r"programmer|tester|researcher|technician|supervisor|founder|co-founder|volunteer|instructor|mentor|"
    r"representative|advisor|analyst|member|fellow|ambassador|president|secretary|captain)\b", re.I)
COMPANY_RE = re.compile(
    r"\b(inc|ltd|limited|pvt|private|llp|llc|corp|corporation|technologies|technology|systems|labs|solutions|"
    r"services|software|consulting|bank|finserv|group|studio|analytics|infotech|enterprises|industries|"
    r"foundation|university|institute|college|company|co\.|partners|ventures|networks|digital|global)\b", re.I)
INSTITUTION_RE = re.compile(
    r"universit|institut|college|school|academy|polytechnic|vidyal|vidyap|mahavidyal|\biit\b|\bnit\b|\biiit\b|"
    r"board|campus|faculty|department of", re.I)
DEGREE_RE = re.compile(
    r"\b(ph\.?\s?d|doctorate|m\.?\s?tech|m\.?\s?e\.?|m\.?\s?sc|m\.?\s?c\.?\s?a|mca|mba|m\.?\s?com|master'?s?|"
    r"post\s?graduate|b\.?\s?tech|b\.?\s?e\.?|b\.?\s?sc|bca|b\.?\s?com|b\.?\s?a\.?|bba|bachelor'?s?|"
    r"undergraduate|diploma|associate degree|hsc|ssc|h\.s\.c|s\.s\.c|12th|10th|xii|class x|higher secondary|"
    r"secondary school|intermediate|a-levels?|pgdm|pg diploma)\b", re.I)
GRADE_RE = re.compile(r"\b(?:(?:current|overall|final|aggregate|cumulative)\s+)?"
                      r"(?:cgpa|sgpa|gpa|percentage|percent|grade|marks|score)\b[^|,;]*|\b\d{1,2}(?:\.\d{1,2})?\s*%"
                      r"|\b\d(?:\.\d{1,2})?\s*/\s*(?:10|4)(?:\.0)?\b", re.I)

LOC_WORDS = {"remote", "hybrid", "on-site", "onsite", "wfh", "india", "usa", "uk", "us", "uae", "canada",
             "germany", "singapore"}


def find_range(text: str) -> tuple[str, str, str] | None:
    m = RANGE_RE.search(text)
    if not m:
        return None
    rest = (text[:m.start()] + " " + text[m.end():])
    rest = re.sub(r"[()\[\]]", " ", rest)
    return m.group("s").strip(), m.group("e").strip(), rest


def _fmt_end(e: str) -> str:
    return "Present" if re.fullmatch(r"present|current|currently|now|ongoing|till\s+date|to\s+date|till\s+now|today|date", e, re.I) else e


def _clean_piece(p: str) -> str:
    p = p.strip(" ,;:-–—|")
    if p.count("(") > p.count(")") and p.startswith("("):
        p = p.lstrip("( ")
    if p.count(")") > p.count("(") and p.endswith(")"):
        p = p.rstrip(") ")
    return p.strip()


def _split_pieces(text: str) -> list[str]:
    parts = re.split(r"\s*\|\s*|\s+[–—]\s+|\s+-\s+|\s+@\s+|\s+at\s+|\s{3,}|\s*•\s*", text)
    return [c for c in (_clean_piece(x) for x in parts if x) if c]


def _looks_location(p: str) -> bool:
    w = p.split()
    if p.lower() in LOC_WORDS or any(x.lower() in LOC_WORDS for x in w):
        return True
    return 1 <= len(w) <= 4 and "," in p and not ROLE_RE.search(p) and not COMPANY_RE.search(p) and not re.search(r"\d", p)


# --------------------------------------------------------------------------------------- normalisation
_PAGE_RE = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:/|of)\s*\d+\s*$|^\s*page\s+\d+\s*$", re.I)
_DECOR_RE = re.compile(r"^[\W_]{3,}$")


def normalize_lines(text: str) -> list[str]:
    raw = [_WS_RE.sub(lambda m: "   " if "\t" in m.group(0) or len(m.group(0)) >= 3 else " ", l).rstrip()
           for l in text.replace("\r", "\n").split("\n")]
    lines = [l for l in raw if not _PAGE_RE.match(l) and not _DECOR_RE.match(l.strip() or "a")
             and not (l.strip() and not strip_bullet(l))]      # lines that are only bullet glyphs
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] and _is_continuation(out[-1], s):
            out[-1] = out[-1].rstrip() + " " + s
        else:
            out.append(s)
    return out


def _is_continuation(prev: str, cur: str) -> bool:
    """A wrapped line that belongs to the previous bullet / paragraph line."""
    if not is_bullet(prev) and len(prev) < 60:
        return False
    if is_bullet(cur) or classify_heading(cur) or RANGE_RE.search(cur) or "|" in cur:
        return False
    if re.match(r"^[A-Z][A-Za-z0-9 &/.\-]{1,30}:\s", cur):
        return False                      # "Tech Stack: ..." style label lines
    if prev.rstrip().endswith((".", "!", "?", ":")) and not cur[:1].islower():
        return False
    if cur[:1].islower():
        return True
    if re.search(r"\b(on|of|and|in|for|to|the|a|an|with|at|by|using|from|into|over)\s*$", prev, re.I):
        return True
    return is_bullet(prev) and len(prev) >= 70 and len(cur.split()) <= 14 and not prev.rstrip().endswith((".", "!", "?"))


def split_sections(lines: list[str]) -> tuple[list[str], list[dict]]:
    header: list[str] = []
    sections: list[dict] = []
    cur = None
    for ln in lines:
        if not ln:
            if cur is not None:
                cur["lines"].append("")
            continue
        h = classify_heading(ln)
        if h:
            key, title, rest = h
            cur = {"key": key, "title": title, "lines": []}
            sections.append(cur)
            if rest:
                cur["lines"].append(rest)
            continue
        (cur["lines"] if cur is not None else header).append(ln)
    for s in sections:
        while s["lines"] and not s["lines"][-1]:
            s["lines"].pop()
    return [l for l in header], sections


# --------------------------------------------------------------------------------------- field parsers
def _split_items(text: str) -> list[str]:
    """Split a run-on list on bullets, semicolons, pipes, 3+ spaces and top-level commas."""
    text = text.strip()
    if not text:
        return []
    parts = re.split(rf"\s*{BUL}\s*|\s*;\s*|\s{{3,}}|\s*\|\s*", text)
    return [p.strip(" .,-") for p in parts if p and p.strip(" .,-")]


def _split_commas(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip(" .-") for p in out if p.strip(" .-")]


def parse_skills(lines: list[str]) -> tuple[list[str], list[str]]:
    skills: list[str] = []
    leftovers: list[str] = []
    seen: set[str] = set()
    body = [l for l in lines if l.strip()]
    for idx, ln in enumerate(body):
        s = strip_bullet(ln)
        if not s:
            continue
        # a short label on its own line followed by a multi-item row is a category name, not a skill
        nxt = strip_bullet(body[idx + 1]) if idx + 1 < len(body) else ""
        if (nxt and len(s.split()) <= 4 and not re.search(r"[,;|:" + BUL + r"]|\s{3,}", s)
                and (len(_split_items(nxt)) >= 2 or len(_split_commas(nxt)) >= 2)):
            continue
        label_split = re.match(r"^([A-Za-z][A-Za-z &/+\-]{1,38}):\s*(.+)$", s)
        if label_split:
            s = label_split.group(2)
        for chunk in _split_items(s):
            for item in _split_commas(chunk):
                item = item.strip()
                if not item or len(item) > 48:
                    if item:
                        leftovers.append(item)
                    continue
                if len(item.split()) > 6:
                    leftovers.append(item)
                    continue
                key = item.lower()
                if key not in seen:
                    seen.add(key)
                    skills.append(item)
    return skills, leftovers


def parse_experience(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    cur: dict | None = None
    pending: list[str] = []          # header lines waiting to be turned into an entry

    def flush_header():
        nonlocal cur, pending
        if not pending:
            return
        text = " | ".join(pending)
        pending = []
        fr = find_range(text)
        start = end = ""
        rest = text
        if fr:
            start, end, rest = fr[0], _fmt_end(fr[1]), fr[2]
        pieces = _split_pieces(rest)
        if len(pieces) == 1 and "," in pieces[0]:
            pieces = [x.strip() for x in pieces[0].split(",") if x.strip()]
        role = company = location = ""
        rest_p = list(pieces)
        for p in pieces:
            if ROLE_RE.search(p) and not role:
                role = p
                rest_p.remove(p)
        for p in list(rest_p):
            if _looks_location(p) and not location and (company or len(rest_p) > 1):
                location = p
                rest_p.remove(p)
        for p in list(rest_p):
            if COMPANY_RE.search(p) and not company:
                company = p
                rest_p.remove(p)
        if not company and rest_p:
            company = rest_p.pop(0)
        if not role and rest_p:
            role = rest_p.pop(0)
        if not location and rest_p:
            location = rest_p.pop(0)
        cur = {"role": role[:160], "company": company[:160], "location": location[:120],
               "start": start, "end": end, "bullets": []}
        entries.append(cur)

    for ln in lines:
        if not ln:
            continue
        if is_bullet(ln):
            flush_header()
            if cur is None:
                cur = {"role": "", "company": "", "location": "", "start": "", "end": "", "bullets": []}
                entries.append(cur)
            cur["bullets"].append(strip_bullet(ln)[:500])
            continue
        s = ln.strip()
        has_range = bool(RANGE_RE.search(s))
        short = len(s) <= 130 and not s.endswith(".")
        headerish = has_range or "|" in s or (short and (ROLE_RE.search(s) or COMPANY_RE.search(s)))
        if pending:
            pending.append(s) if (headerish or short) and len(pending) < 3 else None
            if not (headerish or short):
                flush_header()
                if cur is not None:
                    cur["bullets"].append(s[:500])
            continue
        if cur is None or cur["bullets"] or headerish or not entries:
            if headerish or short:
                pending.append(s)
            elif cur is not None:
                cur["bullets"].append(s[:500])
            else:
                pending.append(s)
        else:
            # description sentence under a header without bullet glyph
            cur["bullets"].append(s[:500])
    flush_header()
    # multi-line headers: a trailing "date only" entry should be merged into the previous one
    merged: list[dict] = []
    for e in entries:
        if merged and not (e["role"] or e["company"]) and (e["start"] or e["end"]) and not merged[-1]["start"]:
            merged[-1]["start"], merged[-1]["end"] = e["start"], e["end"]
            merged[-1]["bullets"] += e["bullets"]
        else:
            merged.append(e)
    return [e for e in merged if e["role"] or e["company"] or e["bullets"]][:25]


def parse_education(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    cur: dict | None = None

    def new():
        nonlocal cur
        cur = {"degree": "", "institution": "", "year": "", "details": ""}
        entries.append(cur)
        return cur

    for ln in lines:
        s = strip_bullet(ln)
        if not s:
            continue
        fr = find_range(s)
        when = ""
        rest = s
        if fr:
            when = f"{fr[0]} - {_fmt_end(fr[1])}"
            rest = fr[2]
        else:
            ys = re.findall(r"(?:19|20)\d{2}", s)
            if ys and len(s) < 160:
                when = ys[-1]
        grades = [g.group(0).strip(" :-") for g in GRADE_RE.finditer(rest)]
        for g in grades:
            rest = rest.replace(g, " ")
        pieces = _split_pieces(rest)
        if len(pieces) == 1 and "," in pieces[0]:
            pieces = [x.strip() for x in pieces[0].split(",") if x.strip()]
        pieces = [p for p in pieces if not re.fullmatch(r"(?:19|20)\d{2}", p)]
        deg = next((p for p in pieces if DEGREE_RE.search(p)), "")

        def _is_inst(p: str) -> bool:
            # an institution is a name, not a sentence ("Selected as coordinator ... in University")
            return bool(INSTITUTION_RE.search(p)) and p != deg and len(p.split()) <= 9 and not re.match(
                r"^(selected|awarded|member|served|worked|completed|elected|appointed|secured|was|led|"
                r"organised|organized|participated)\b", p, re.I)

        inst = next((p for p in pieces if _is_inst(p)), "")
        other = [p for p in pieces if p not in (deg, inst)]
        if deg:
            if cur is None or cur["degree"] or (cur["institution"] and inst):
                new()
            cur["degree"] = deg
            if inst:
                cur["institution"] = cur["institution"] or inst
        elif inst:
            if cur is None or cur["institution"]:
                new()
            cur["institution"] = inst
        elif cur is None:
            new()
        if when and not cur["year"]:
            cur["year"] = when
        extra = [p for p in other if p] + grades
        if extra:
            cur["details"] = " | ".join(x for x in [cur["details"], *extra] if x)[:300]
    return [e for e in entries if e["degree"] or e["institution"]][:15]


def parse_projects(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        if not ln:
            continue
        if is_bullet(ln):
            if cur is None:
                cur = {"name": "", "description": "", "bullets": []}
                out.append(cur)
            cur["bullets"].append(strip_bullet(ln)[:500])
            continue
        s = ln.strip()
        label = re.match(r"^(tech(?:nologies)?(?: stack)?|tools?|stack|built with|technologies used)\s*:\s*(.+)$", s, re.I)
        if label and cur is not None:
            cur["description"] = (cur["description"] + " | " if cur["description"] else "") + label.group(2).strip()
            continue
        chunks = [c for c in re.split(r"\s{3,}|,\s*", s) if c.strip()]
        if (cur is not None and len(chunks) >= 3 and all(len(c.split()) <= 3 for c in chunks)
                and not re.search(r"[.!?]$", s) and not RANGE_RE.search(s)):
            # a bare technology row ("Python   Pandas   Docker") belongs to the project above it
            cur["description"] = (cur["description"] + " | " if cur["description"] else "") + ", ".join(
                c.strip() for c in chunks)
            continue
        short = len(s) <= 150 and not s.endswith(".")
        if short and (cur is None or cur["bullets"] or not cur["description"] or "|" in s):
            fr = find_range(s)
            rest = fr[2] if fr else s
            pieces = _split_pieces(rest)
            name = pieces[0] if pieces else s
            desc_parts = pieces[1:]
            if fr:
                desc_parts.append(f"{fr[0]} - {_fmt_end(fr[1])}")
            cur = {"name": name[:160], "description": " | ".join(desc_parts)[:400], "bullets": []}
            out.append(cur)
        elif cur is not None:
            cur["bullets"].append(s[:500])
        else:
            cur = {"name": s[:160], "description": "", "bullets": []}
            out.append(cur)
    return [p for p in out if p["name"] or p["bullets"]][:25]


def parse_list_section(lines: list[str]) -> list[str]:
    items: list[str] = []
    body = [l for l in lines if l.strip()]
    starts = sum(1 for l in body if is_bullet(l))
    inline = sum(1 for l in body if BUL in strip_bullet(l))
    if body and starts < len(body) / 2 and inline:
        # wrapped paragraph whose items are separated by inline bullets: re-join before splitting
        lines = [" ".join(l.strip() for l in body)]
    for ln in lines:
        s = strip_bullet(ln)
        if not s:
            continue
        for part in _split_items(s):
            part = part.strip()
            if len(part) >= 3:
                items.append(part[:240])
    return items


def parse_header(header: list[str]) -> dict:
    lines = [l.strip() for l in header if l.strip()]
    blob = " \n".join(lines)
    email = EMAIL_RE.search(blob)
    links: list[str] = []
    for m in URL_RE.finditer(blob):
        u = m.group(0).rstrip(".,")
        if EMAIL_RE.search(u) or "@" in blob[max(0, m.start() - 1):m.start()]:
            continue
        if u.lower() not in [x.lower() for x in links]:
            links.append(u)
    phone = ""
    for m in PHONE_RE.finditer(re.sub(EMAIL_RE, " ", blob)):
        digits = re.sub(r"\D", "", m.group(0))
        if 9 <= len(digits) <= 14:
            phone = m.group(0).strip()
            break

    name = ""
    name_line_idx = -1
    for i, l in enumerate(lines[:4]):
        seg = re.split(r"\s*[|•]\s*|\s{3,}", l)[0].strip()
        words = seg.split()
        if (1 < len(words) <= 5 and not EMAIL_RE.search(seg) and not re.search(r"\d", seg)
                and all(re.match(r"^[A-Za-z.'\-]+$", w) for w in words)
                and not (ROLE_RE.search(seg) and len(words) > 3)):
            name = seg.title() if seg.isupper() else seg
            name_line_idx = i
            break

    headline = ""
    location = ""
    summary_lines: list[str] = []
    sep = r"\s*[|•]\s*|\s{3,}"
    place_comma = re.compile(
        r"[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+){0,2}(?:,\s*[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+){0,2}){1,2}")
    place_single = re.compile(r"[A-Z][a-z]{2,}(?: [A-Z][a-z]{2,})?")
    generic = {"curriculum vitae", "resume", "cv", "contact", "profile", "summary"}

    def is_contact_seg(sg: str) -> bool:
        low = sg.lower()
        return bool(EMAIL_RE.search(sg) or "linkedin" in low or "github" in low or URL_RE.fullmatch(sg.strip())
                    or re.fullmatch(r"[+\d\s\-().]{9,}", sg) or re.fullmatch(r"(?:ph|phone|mob|mobile|tel)[:.\s].*", low))

    def as_location(sg: str, contact_line: bool) -> bool:
        sg = sg.strip()
        if not sg or sg.lower() in generic or ROLE_RE.search(sg) or COMPANY_RE.search(sg):
            return False
        if place_comma.fullmatch(sg):
            return True
        return contact_line and bool(place_single.fullmatch(sg)) and not sg.isupper()

    for i, l in enumerate(lines[:8]):
        if i == name_line_idx:
            rest = re.split(sep, l, maxsplit=1)
            if len(rest) < 2:
                continue
            l = rest[1]
        segs = [x.strip() for x in re.split(sep, l) if x.strip()]
        contact_line = any(is_contact_seg(sg) for sg in segs)
        plain = [sg for sg in segs if not is_contact_seg(sg)]
        if contact_line:
            for sg in plain:
                if not location and as_location(sg, True):
                    location = sg
                elif not headline and ROLE_RE.search(sg) and len(sg) <= 140:
                    headline = sg
        else:
            if len(l.split()) > 18:
                summary_lines.append(l)
            elif len(plain) == 1 and not location and as_location(plain[0], False):
                location = plain[0]
            elif not headline and plain and len(" | ".join(plain)) <= 150 and i != name_line_idx:
                headline = " | ".join(plain)
    return {"name": name, "email": email.group(0) if email else "", "phone": phone, "location": location,
            "links": links[:5], "headline": headline[:160], "summary_lines": summary_lines}


# --------------------------------------------------------------------------------------- driver
def parse_document(text: str, fallback_name: str = "") -> tuple[dict, dict]:
    from .doc_extract import clean_glyphs      # pasted text may still contain (cid:NNN) or symbol-font bullets
    lines = normalize_lines(clean_glyphs(text))
    header_lines, sections = split_sections(lines)
    hdr = parse_header(header_lines)

    resume = {
        "basics": {"name": hdr["name"] or fallback_name, "headline": hdr["headline"], "email": hdr["email"],
                   "phone": hdr["phone"], "location": hdr["location"], "links": hdr["links"]},
        "summary": "", "experience": [], "education": [], "skills": [], "projects": [],
        "certifications": [], "extras": [],
    }
    leftovers: list[str] = []
    detected: list[dict] = []

    for sec in sections:
        key, body = sec["key"], [l for l in sec["lines"]]
        content = [l for l in body if l]
        detected.append({"key": key, "title": sec["title"], "lines": len(content)})
        if not content:
            continue
        if key == "summary":
            para = " ".join(strip_bullet(l) for l in content)
            resume["summary"] = (resume["summary"] + " " + para).strip() if resume["summary"] else para
        elif key == "skills":
            sk, left = parse_skills(content)
            for s in sk:
                if s.lower() not in {x.lower() for x in resume["skills"]}:
                    resume["skills"].append(s)
            leftovers += left
        elif key == "experience":
            resume["experience"] += parse_experience(content)
        elif key == "education":
            resume["education"] += parse_education(content)
        elif key == "projects":
            resume["projects"] += parse_projects(content)
        elif key == "certifications":
            resume["certifications"] += parse_list_section(content)
        else:
            items = parse_list_section(content)
            if items:
                resume["extras"].append({"title": sec["title"], "items": items})

    if not resume["summary"] and hdr["summary_lines"]:
        resume["summary"] = " ".join(hdr["summary_lines"])[:1200]
    # unclassified skill-like sentences are kept, not lost
    if leftovers:
        resume["extras"].append({"title": "Additional skills and notes", "items": leftovers[:20]})

    warnings: list[str] = []
    if not sections:
        # no headings at all: keep every line so the preview is still complete
        body = [l for l in lines if l and l not in header_lines[:3]]
        if body:
            resume["extras"].append({"title": "Resume content", "items": [strip_bullet(l) for l in body][:120]})
        warnings.append("No section headings were recognised, so the content was kept as plain text. "
                        "Assign it to sections in the builder.")
    else:
        for k, label in (("experience", "work experience"), ("education", "education"), ("skills", "skills")):
            if not any(d["key"] == k for d in detected):
                if k == "experience" and any(d["key"] == "projects" for d in detected):
                    continue
                warnings.append(f"No {label} section was found.")
    if not resume["basics"]["email"]:
        warnings.append("No email address was found.")
    if not resume["basics"]["phone"]:
        warnings.append("No phone number was found.")

    report = {
        "sections": detected,
        "counts": {
            "experience": len(resume["experience"]), "projects": len(resume["projects"]),
            "education": len(resume["education"]), "skills": len(resume["skills"]),
            "certifications": len(resume["certifications"]),
            "extra_sections": len(resume["extras"]),
            "lines_read": len([l for l in lines if l]),
        },
        "warnings": warnings,
    }
    return resume, report
