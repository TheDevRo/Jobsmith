"""
resume_generator.py — Generate professional DOCX resume and cover letter files.

Uses python-docx to create clean, ATS-friendly formatted documents.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.shared import Pt, Inches, RGBColor, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml, OxmlElement
from .paths import project_root

logger = logging.getLogger(__name__)

RESUMES_DIR = project_root() / "resumes"

# -- Color palette --
COLOR_BLACK = RGBColor(0x22, 0x22, 0x22)
COLOR_DARK = RGBColor(0x33, 0x33, 0x33)
COLOR_GRAY = RGBColor(0x66, 0x66, 0x66)
COLOR_LIGHT_GRAY = RGBColor(0x99, 0x99, 0x99)
COLOR_ACCENT = RGBColor(0x2B, 0x57, 0x97)
COLOR_NAVY = RGBColor(0x1F, 0x3A, 0x5F)
COLOR_CHARCOAL = RGBColor(0x1F, 0x1F, 0x1F)
COLOR_RULE = RGBColor(0x99, 0x99, 0x99)


# Style tokens — each preset is a flat dict consumed by generate_resume_docx.
# All presets stay single-column, real text, no images/tables (ATS-safe).
_STYLES: dict[str, dict] = {
    "standard": {
        "body_font": "Calibri",
        "name_font": "Calibri",
        "name_size": 20,
        "name_uppercase": True,
        "name_letter_spacing": 2,
        "name_color": COLOR_BLACK,
        "accent_color": COLOR_ACCENT,
        "header_size": 11,
        "header_color": COLOR_ACCENT,
        "header_underline": True,
        "header_underline_color": "2B5797",
        "header_underline_size": "4",
        "header_letter_spacing": 0,
        "bullet_marker": "▸  ",
        "bullet_marker_size": 8,
        "bullet_marker_color": COLOR_ACCENT,
        "name_rule": True,
        "name_rule_color": "2B5797",
        "name_rule_size": "8",
        "margins": (0.5, 0.5, 0.7, 0.7),  # top, bottom, left, right (inches)
        "line_spacing": None,
        "entry_layout": "stacked",
        "hyperlinks": False,
    },
    "minimal": {
        "body_font": "Times New Roman",
        "name_font": "Times New Roman",
        "name_size": 16,
        "name_uppercase": False,
        "name_letter_spacing": 0,
        "name_color": COLOR_BLACK,
        "accent_color": COLOR_DARK,
        "header_size": 11,
        "header_color": COLOR_DARK,
        "header_underline": True,
        "header_underline_color": "999999",
        "header_underline_size": "4",
        "header_letter_spacing": 0,
        "bullet_marker": "-  ",
        "bullet_marker_size": 10.5,
        "bullet_marker_color": COLOR_DARK,
        "name_rule": True,
        "name_rule_color": "999999",
        "name_rule_size": "4",
        "margins": (0.75, 0.75, 1.0, 1.0),
        "line_spacing": None,
        "entry_layout": "stacked",
        "hyperlinks": False,
    },
    "modern": {
        # Body: Aptos (Office 365 default, falls back to Calibri on older systems)
        # Name: same family at larger weight — single-family typography, more polished
        "body_font": "Aptos",
        "name_font": "Aptos",
        "name_size": 22,
        "name_uppercase": False,
        "name_letter_spacing": 0,
        "name_color": COLOR_CHARCOAL,
        "accent_color": COLOR_NAVY,
        "header_size": 10,
        "header_color": COLOR_CHARCOAL,
        "header_underline": False,  # no full-width rule — cleaner hierarchy
        "header_underline_color": "1F3A5F",
        "header_underline_size": "4",
        "header_letter_spacing": 1.5,
        "bullet_marker": "•  ",
        "bullet_marker_size": 10.5,
        "bullet_marker_color": COLOR_DARK,
        "name_rule": True,
        "name_rule_color": "1F3A5F",
        "name_rule_size": "6",
        "margins": (0.6, 0.6, 0.8, 0.8),
        "line_spacing": 1.15,
        # Inline: "Title  ·  Company                Dates" on one line.
        "entry_layout": "inline",
        "hyperlinks": True,
    },
}


def _add_hyperlink(paragraph, url, text, font_name, size_pt, color_hex):
    """Insert a clickable hyperlink run into a paragraph."""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")

    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rPr.append(rFonts)

    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))  # half-points
    rPr.append(sz)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), color_hex)
    rPr.append(color)

    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _ensure_dir():
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)


def _set_font(run, name="Calibri", size=11, bold=False, italic=False, color=None):
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    if color:
        run.font.color.rgb = color


def _set_paragraph_spacing(para, before=0, after=0, line_spacing=None):
    pf = para.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if line_spacing:
        pf.line_spacing = Pt(line_spacing)


def _add_bottom_border(paragraph, color="999999", size="6"):
    """Add a thin bottom border line to a paragraph."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'  <w:bottom w:val="single" w:sz="{size}" w:space="1" w:color="{color}"/>'
        f'</w:pBdr>'
    )
    pPr.append(pBdr)


def _add_section_header(doc, title):
    """Add a styled section header with underline rule."""
    para = doc.add_paragraph()
    _set_paragraph_spacing(para, before=10, after=3)
    run = para.add_run(title.upper())
    _set_font(run, size=11, bold=True, color=COLOR_ACCENT)
    run.font.all_caps = True
    # Add a line under the header
    _add_bottom_border(para, color="2B5797", size="4")
    return para


def _parse_resume_sections(content: str) -> dict:
    """
    Parse AI-generated resume text into sections.

    Case-insensitive matching against a broad set of header variants.
    If no recognised sections are detected, the entire content is treated
    as a summary so the DOCX is always generated rather than silently empty.
    """
    # Map every accepted variant → canonical section key
    _HEADER_MAP: dict[str, str] = {
        # Summary variants
        "SUMMARY": "summary",
        "PROFESSIONAL SUMMARY": "summary",
        "PROFILE SUMMARY": "summary",
        "PROFILE": "summary",
        "ABOUT ME": "summary",
        # Skills variants
        "SKILLS": "skills",
        "TECHNICAL SKILLS": "skills",
        "CORE COMPETENCIES": "skills",
        "KEY SKILLS": "skills",
        "COMPETENCIES": "skills",
        # Experience variants
        "EXPERIENCE": "experience",
        "PROFESSIONAL EXPERIENCE": "experience",
        "WORK EXPERIENCE": "experience",
        "EMPLOYMENT HISTORY": "experience",
        "CAREER HISTORY": "experience",
        # Education variants
        "EDUCATION": "education",
        "EDUCATIONAL BACKGROUND": "education",
        "ACADEMIC BACKGROUND": "education",
        # Certification variants
        "CERTIFICATIONS": "certifications",
        "CERTIFICATES": "certifications",
        "LICENSES": "certifications",
        "LICENSES & CERTIFICATIONS": "certifications",
        # Misc — kept for completeness but not rendered specially
        "PROJECTS": "projects",
        "AWARDS": "awards",
        "ACHIEVEMENTS": "awards",
    }

    sections: dict[str, str] = {}
    current_section = "preamble"
    current_lines: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()
        # Strip markdown prefix characters the AI might add despite instructions
        cleaned = re.sub(r'^[#*_`]+\s*', '', stripped).strip()
        # Normalise: upper-case and strip trailing colon/whitespace
        cleaned_upper = cleaned.upper().rstrip(": ")

        if cleaned_upper in _HEADER_MAP:
            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = _HEADER_MAP[cleaned_upper]
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    # Remove the preamble key — it's just lines before the first header
    sections.pop("preamble", None)

    # Fallback: if the LLM returned freeform text with no recognisable headers,
    # treat the whole content as a summary so we always produce a DOCX.
    if not sections:
        logger.warning(
            "_parse_resume_sections: no section headers found — treating full "
            "content (%d chars) as summary. Check LLM output format.", len(content)
        )
        sections["summary"] = content.strip()

    return sections


def _parse_experience_entries(text: str) -> list[dict]:
    """
    Parse experience section into structured entries.
    Each entry: {title, company, dates, bullets[]}

    Supported formats (in priority order):
      Structured  — Title: / Company: / Dates: prefix lines (from prompt)
      Pipe        — "Job Title | Company Name | Jan 2020 - Present"
      At-notation — "Job Title at Company Name (2020-2023)"
      Comma+paren — "Job Title, Company Name (2020-2023)"
      Fallback    — any non-bullet line starts a new entry; raw text preserved
    """
    entries = []
    current: dict | None = None

    def _save_current():
        if current:
            entries.append(current)

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Strip markdown bold markers
        stripped = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)

        # ── Structured prefix format (highest priority) ──────────────────
        title_match   = re.match(r'^Title:\s*(.+)',   stripped, re.IGNORECASE)
        company_match = re.match(r'^Company:\s*(.+)', stripped, re.IGNORECASE)
        dates_match   = re.match(r'^Dates?:\s*(.+)',  stripped, re.IGNORECASE)

        if title_match:
            _save_current()
            current = {"title": title_match.group(1).strip(), "company": "", "dates": "", "bullets": []}
            continue

        if company_match and current:
            current["company"] = company_match.group(1).strip()
            continue

        if dates_match and current:
            current["dates"] = dates_match.group(1).strip()
            continue

        # ── Bullet points ─────────────────────────────────────────────────
        if stripped.startswith(("-", "•", "*", "–", "▸")):
            bullet = re.sub(r'^[-•*–▸]+\s*', '', stripped).strip()
            if bullet:
                if current is None:
                    # Orphan bullet before any entry — create a blank entry
                    current = {"title": "", "company": "", "dates": "", "bullets": []}
                current["bullets"].append(bullet)
            continue

        # ── Freeform header lines (no structured prefix) ─────────────────
        # Only try to parse as a header if it doesn't look like body text
        # (body text sentences are long; headers are short and concise).
        if len(stripped) < 120:
            # Format 1: "Title | Company | Dates"
            pipe_parts = [p.strip() for p in stripped.split("|")]
            if len(pipe_parts) >= 2:
                _save_current()
                current = {
                    "title":   pipe_parts[0],
                    "company": pipe_parts[1],
                    "dates":   pipe_parts[2] if len(pipe_parts) >= 3 else "",
                    "bullets": [],
                }
                continue

            # Format 2: "Title at Company (dates)" or "Title at Company"
            at_match = re.match(
                r'^(.+?)\s+at\s+(.+?)(?:\s+\(([^)]+)\))?$', stripped, re.IGNORECASE
            )
            if at_match:
                _save_current()
                current = {
                    "title":   at_match.group(1).strip(),
                    "company": at_match.group(2).strip(),
                    "dates":   at_match.group(3).strip() if at_match.group(3) else "",
                    "bullets": [],
                }
                continue

            # Format 3: "Title, Company (dates)"
            comma_paren = re.match(
                r'^(.+?),\s+(.+?)\s+\(([^)]+)\)$', stripped
            )
            if comma_paren:
                _save_current()
                current = {
                    "title":   comma_paren.group(1).strip(),
                    "company": comma_paren.group(2).strip(),
                    "dates":   comma_paren.group(3).strip(),
                    "bullets": [],
                }
                continue

        # ── Fallback: treat as a new entry title, preserving the raw text ─
        _save_current()
        current = {"title": stripped, "company": "", "dates": "", "bullets": []}

    _save_current()
    return entries


def _parse_education_entries(text: str) -> list[dict]:
    """
    Parse education section into structured entries: {degree, school, year}.

    Supported formats:
      Structured  — Degree: / School: / Year: prefix lines (from prompt)
      Freeform    — "B.S. Computer Science, Stanford University, 2020"
                    "B.S. Computer Science | Stanford University | 2020"
                    "B.S. Computer Science, Stanford University (2020)"
    """
    entries = []
    current: dict | None = None

    # Pattern to detect a 4-digit year
    _YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')

    def _extract_year(s: str) -> tuple[str, str]:
        """Return (text_without_year, year_string). year is '' if not found."""
        m = _YEAR_RE.search(s)
        if m:
            year = m.group()
            rest = (s[:m.start()] + s[m.end():]).strip(" ,()–-")
            return rest, year
        return s, ""

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        stripped = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)

        # ── Structured prefix lines ──────────────────────────────────────
        degree_match = re.match(r'^Degree:\s*(.+)', stripped, re.IGNORECASE)
        school_match = re.match(r'^School:\s*(.+)', stripped, re.IGNORECASE)
        year_match   = re.match(r'^Year:\s*(.+)',   stripped, re.IGNORECASE)

        if degree_match:
            if current:
                entries.append(current)
            current = {"degree": degree_match.group(1).strip(), "school": "", "year": ""}
            continue

        if school_match and current:
            current["school"] = school_match.group(1).strip()
            continue

        if year_match and current:
            current["year"] = year_match.group(1).strip()
            continue

        # Skip bullet points
        if stripped.startswith(("-", "•", "*", "–")):
            continue

        # ── Freeform line ─────────────────────────────────────────────────
        # Try pipe-separated first: "Degree | School | Year"
        pipe_parts = [p.strip() for p in stripped.split("|")]
        if len(pipe_parts) >= 2:
            if current:
                entries.append(current)
            rest, year = _extract_year(pipe_parts[-1])
            if year:
                school = pipe_parts[1].strip() if len(pipe_parts) >= 3 else rest
                current = {"degree": pipe_parts[0].strip(), "school": school, "year": year}
            else:
                current = {"degree": pipe_parts[0].strip(), "school": pipe_parts[1].strip(), "year": ""}
            continue

        # Try comma-separated: "Degree, School, Year" or "Degree, School (Year)"
        # Extract year first (could be in parens or as last comma token)
        paren_year = re.search(r'\((\d{4})\)', stripped)
        if paren_year:
            year = paren_year.group(1)
            without_year = stripped[:paren_year.start()].strip().rstrip(",")
            comma_parts = [p.strip() for p in without_year.split(",", 1)]
            if len(comma_parts) >= 2:
                if current:
                    entries.append(current)
                current = {"degree": comma_parts[0], "school": comma_parts[1], "year": year}
                continue

        comma_parts = [p.strip() for p in stripped.split(",")]
        if len(comma_parts) >= 2:
            last, year = _extract_year(comma_parts[-1])
            if year:
                school = ",".join(comma_parts[1:-1]).strip() or last
                if current:
                    entries.append(current)
                current = {"degree": comma_parts[0], "school": school, "year": year}
                continue

        # Last resort: treat the whole line as a degree field
        if current:
            entries.append(current)
        current = {"degree": stripped.lstrip("-•* "), "school": "", "year": ""}

    if current:
        entries.append(current)

    return entries


def generate_resume_docx(content: str, profile: dict, job: dict, config: dict | None = None) -> str:
    """
    Create a professional, ATS-friendly resume DOCX from AI-generated content.

    config.application_honesty.resume_style controls the visual theme:
      "standard" (default) — Calibri, accent blue headers
      "minimal"            — Times New Roman, plain headers, maximum ATS compatibility
      "modern"             — Aptos, navy accent, no header underlines, 1.15 line spacing
    Returns the file path to the generated document.
    """
    _ensure_dir()

    resume_style = "standard"
    if config:
        resume_style = (
            config.get("application_honesty", {}).get("resume_style", "standard").lower()
        )
    s = _STYLES.get(resume_style, _STYLES["standard"])

    _font_name    = s["body_font"]
    _name_font    = s["name_font"]
    _accent_color = s["accent_color"]
    _header_size  = s["header_size"]
    _name_size    = s["name_size"]

    doc = Document()

    # -- Page margins --
    top_m, bot_m, left_m, right_m = s["margins"]
    for section in doc.sections:
        section.top_margin    = Inches(top_m)
        section.bottom_margin = Inches(bot_m)
        section.left_margin   = Inches(left_m)
        section.right_margin  = Inches(right_m)

    # -- Set default paragraph style --
    style = doc.styles['Normal']
    style.font.name = _font_name
    style.font.size = Pt(11)
    style.font.color.rgb = COLOR_DARK
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(2)
    if s["line_spacing"]:
        style.paragraph_format.line_spacing = s["line_spacing"]

    # ==========================================
    # HEADER — Name and contact info
    # ==========================================
    name = profile.get("full_name", "")
    if name:
        name_para = doc.add_paragraph()
        name_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_spacing(name_para, before=0, after=2)
        display_name = name.upper() if s["name_uppercase"] else name
        name_run = name_para.add_run(display_name)
        _set_font(name_run, name=_name_font, size=_name_size, bold=True, color=s["name_color"])
        if s["name_letter_spacing"]:
            name_run.font.letter_spacing = Pt(s["name_letter_spacing"])

    # Contact line: email | phone | location | linkedin | portfolio | github
    # Each entry is (display_text, url_or_None) — urls become clickable when style enables it.
    contact_entries: list[tuple[str, str | None]] = []
    for field in ["email", "phone", "location"]:
        val = profile.get(field, "")
        if val:
            contact_entries.append((val, None))

    for field in ["linkedin", "portfolio", "github"]:
        url = profile.get(field, "")
        if not url:
            continue
        clean = re.sub(r"^https?://", "", url).rstrip("/")
        if clean:
            full_url = url if url.startswith("http") else f"https://{clean}"
            contact_entries.append((clean, full_url))

    if contact_entries:
        contact_para = doc.add_paragraph()
        contact_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_spacing(contact_para, before=0, after=2)
        separator = "  |  "
        link_color_hex = "{:02X}{:02X}{:02X}".format(*s["accent_color"])
        for i, (text, url) in enumerate(contact_entries):
            if i > 0:
                sep_run = contact_para.add_run(separator)
                _set_font(sep_run, name=_font_name, size=9, color=COLOR_GRAY)
            if url and s["hyperlinks"]:
                _add_hyperlink(contact_para, url, text, _font_name, 9, link_color_hex)
            else:
                run = contact_para.add_run(text)
                _set_font(run, name=_font_name, size=9, color=COLOR_GRAY)

    # Rule under header
    if s["name_rule"]:
        rule_para = doc.add_paragraph()
        _set_paragraph_spacing(rule_para, before=4, after=4)
        _add_bottom_border(rule_para, color=s["name_rule_color"], size=s["name_rule_size"])

    # ==========================================
    # PARSE AI CONTENT
    # ==========================================
    sections = _parse_resume_sections(content)

    def _section_header(title: str):
        """Add a section header respecting the chosen style."""
        para = doc.add_paragraph()
        _set_paragraph_spacing(para, before=10, after=3)
        run = para.add_run(title.upper())
        _set_font(run, name=_font_name, size=_header_size, bold=True, color=s["header_color"])
        run.font.all_caps = True
        if s["header_letter_spacing"]:
            run.font.letter_spacing = Pt(s["header_letter_spacing"])
        if s["header_underline"]:
            _add_bottom_border(para, color=s["header_underline_color"], size=s["header_underline_size"])
        return para

    # ==========================================
    # SUMMARY
    # ==========================================
    summary_text = sections.get("summary", "")
    if summary_text:
        _section_header("Summary")
        p = doc.add_paragraph()
        _set_paragraph_spacing(p, before=2, after=4)
        # Clean up any stray markdown
        clean_summary = re.sub(r'\*\*(.+?)\*\*', r'\1', summary_text).strip()
        run = p.add_run(clean_summary)
        _set_font(run, name=_font_name, size=10.5, color=COLOR_DARK)

    # ==========================================
    # SKILLS
    # ==========================================
    skills_text = sections.get("skills", "")
    if skills_text:
        _section_header("Technical Skills")

        # Parse skills — could be comma-separated, bullet list, or categorized
        skills_text = re.sub(r'\*\*(.+?)\*\*', r'\1', skills_text)
        # Remove bullet prefixes
        skills_text = re.sub(r'^[-*•]\s*', '', skills_text, flags=re.MULTILINE)
        # Collect non-empty lines
        skill_lines = [line.strip() for line in skills_text.split("\n") if line.strip()]

        # Check if skills are categorized (e.g., "Security: skill1, skill2")
        categorized = any(":" in line for line in skill_lines)

        if categorized:
            for line in skill_lines:
                p = doc.add_paragraph()
                _set_paragraph_spacing(p, before=1, after=1)
                if ":" in line:
                    category, skills = line.split(":", 1)
                    cat_run = p.add_run(category.strip() + ": ")
                    _set_font(cat_run, name=_font_name, size=10.5, bold=True, color=COLOR_DARK)
                    skills_run = p.add_run(skills.strip())
                    _set_font(skills_run, name=_font_name, size=10.5, color=COLOR_DARK)
                else:
                    run = p.add_run(line)
                    _set_font(run, name=_font_name, size=10.5, color=COLOR_DARK)
        else:
            # Join all into one comma-separated line
            all_skills = []
            for line in skill_lines:
                all_skills.extend(s.strip() for s in line.split(",") if s.strip())
            p = doc.add_paragraph()
            _set_paragraph_spacing(p, before=2, after=4)
            run = p.add_run(", ".join(all_skills))
            _set_font(run, name=_font_name, size=10.5, color=COLOR_DARK)

    # ==========================================
    # EXPERIENCE
    # ==========================================
    exp_text = sections.get("experience", "")
    if exp_text:
        _section_header("Professional Experience")
        entries = _parse_experience_entries(exp_text)

        # Inline layout uses full text-width tab; stacked keeps the legacy 6.1" stop.
        inline_right_tab = Inches(8.5 - left_m - right_m)
        stacked_right_tab = Inches(6.1)

        for entry in entries:
            if s["entry_layout"] == "inline":
                # One-line header: "Title  ·  Company                Dates"
                head = doc.add_paragraph()
                _set_paragraph_spacing(head, before=6, after=2)

                title_run = head.add_run(entry["title"])
                _set_font(title_run, name=_font_name, size=11, bold=True, color=COLOR_BLACK)

                if entry["company"]:
                    sep_run = head.add_run("  ·  ")
                    _set_font(sep_run, name=_font_name, size=10.5, color=s["accent_color"])
                    company_run = head.add_run(entry["company"])
                    _set_font(company_run, name=_font_name, size=10.5, color=COLOR_DARK)

                if entry["dates"]:
                    head.paragraph_format.tab_stops.add_tab_stop(
                        inline_right_tab, alignment=WD_ALIGN_PARAGRAPH.RIGHT
                    )
                    head.add_run("\t")
                    dates_run = head.add_run(entry["dates"])
                    _set_font(dates_run, name=_font_name, size=10, italic=True, color=COLOR_GRAY)
            else:
                # Stacked: title/dates on line 1, company in italic on line 2
                title_para = doc.add_paragraph()
                _set_paragraph_spacing(title_para, before=6, after=0)

                title_run = title_para.add_run(entry["title"])
                _set_font(title_run, name=_font_name, size=11, bold=True, color=COLOR_BLACK)

                if entry["dates"]:
                    title_para.paragraph_format.tab_stops.add_tab_stop(
                        stacked_right_tab, alignment=WD_ALIGN_PARAGRAPH.RIGHT
                    )
                    title_para.add_run("\t")
                    dates_run = title_para.add_run(entry["dates"])
                    _set_font(dates_run, name=_font_name, size=10, italic=True, color=COLOR_GRAY)

                if entry["company"]:
                    company_para = doc.add_paragraph()
                    _set_paragraph_spacing(company_para, before=0, after=2)
                    company_run = company_para.add_run(entry["company"])
                    _set_font(company_run, name=_font_name, size=10.5, italic=True, color=COLOR_GRAY)

            # Bullets — marker glyph and color come from the style preset
            for bullet in entry["bullets"]:
                bullet_para = doc.add_paragraph()
                _set_paragraph_spacing(bullet_para, before=1, after=1)
                bullet_para.paragraph_format.left_indent = Inches(0.25)
                bullet_para.paragraph_format.first_line_indent = Inches(-0.2)

                marker_run = bullet_para.add_run(s["bullet_marker"])
                _set_font(marker_run, name=_font_name, size=s["bullet_marker_size"], color=s["bullet_marker_color"])
                text_run = bullet_para.add_run(bullet)
                _set_font(text_run, name=_font_name, size=10.5, color=COLOR_DARK)

    # ==========================================
    # EDUCATION
    # ==========================================
    edu_text = sections.get("education", "")
    if edu_text:
        _section_header("Education")
        entries = _parse_education_entries(edu_text)

        for entry in entries:
            edu_para = doc.add_paragraph()
            _set_paragraph_spacing(edu_para, before=3, after=1)

            degree_run = edu_para.add_run(entry["degree"])
            _set_font(degree_run, name=_font_name, size=10.5, bold=True, color=COLOR_BLACK)

            if entry["school"]:
                school_text = f"  —  {entry['school']}"
                if entry["year"]:
                    school_text += f"  ({entry['year']})"
                school_run = edu_para.add_run(school_text)
                _set_font(school_run, name=_font_name, size=10.5, color=COLOR_GRAY)
            elif entry["year"]:
                year_run = edu_para.add_run(f"  ({entry['year']})")
                _set_font(year_run, name=_font_name, size=10.5, color=COLOR_GRAY)

    # ==========================================
    # CERTIFICATIONS
    # ==========================================
    cert_text = sections.get("certifications", "")
    if cert_text:
        _section_header("Certifications")
        for line in cert_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
            clean = line.lstrip("-*•– ").strip()
            if clean:
                p = doc.add_paragraph()
                _set_paragraph_spacing(p, before=1, after=1)
                p.paragraph_format.left_indent = Inches(0.25)
                p.paragraph_format.first_line_indent = Inches(-0.2)
                marker_run = p.add_run(s["bullet_marker"])
                _set_font(marker_run, name=_font_name, size=s["bullet_marker_size"], color=s["bullet_marker_color"])
                run = p.add_run(clean)
                _set_font(run, name=_font_name, size=10.5, color=COLOR_DARK)

    # ==========================================
    # REFERENCES — appended verbatim from profile, never sent to AI
    # ==========================================
    references = profile.get("references", []) or []
    valid_refs = [r for r in references if (r.get("name") or "").strip()]
    if valid_refs:
        _section_header("References")
        for ref in valid_refs:
            name_para = doc.add_paragraph()
            _set_paragraph_spacing(name_para, before=4, after=1)
            name_run = name_para.add_run(ref.get("name", "").strip())
            _set_font(name_run, name=_font_name, size=10.5, bold=True, color=COLOR_BLACK)
            position = (ref.get("position") or "").strip()
            if position:
                pos_run = name_para.add_run(f"  —  {position}")
                _set_font(pos_run, name=_font_name, size=10.5, italic=True, color=COLOR_GRAY)

            contact_bits = []
            email = (ref.get("email") or "").strip()
            phone = (ref.get("phone") or "").strip()
            if email:
                contact_bits.append(email)
            if phone:
                contact_bits.append(phone)
            if contact_bits:
                contact_para = doc.add_paragraph()
                _set_paragraph_spacing(contact_para, before=0, after=2)
                run = contact_para.add_run("  |  ".join(contact_bits))
                _set_font(run, name=_font_name, size=10, color=COLOR_DARK)

    # -- Save --
    job_id = job.get("id", "unknown")
    file_path = RESUMES_DIR / f"{job_id}_resume.docx"
    doc.save(str(file_path))
    logger.info("Resume saved to %s", file_path)
    return str(file_path)


def generate_cover_letter_docx(content: str, profile: dict, job: dict) -> str:
    """
    Create a professional cover letter DOCX.
    Returns the file path to the generated document.
    """
    _ensure_dir()
    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    # Default style
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(11)
    style.font.color.rgb = COLOR_DARK

    # Candidate header — same style as resume for brand consistency
    name = profile.get("full_name", "")
    if name:
        name_para = doc.add_paragraph()
        name_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_spacing(name_para, before=0, after=2)
        name_run = name_para.add_run(name.upper())
        _set_font(name_run, size=16, bold=True, color=COLOR_BLACK)
        name_run.font.letter_spacing = Pt(1.5)

    contact_parts = []
    for field in ["email", "phone", "location"]:
        val = profile.get(field, "")
        if val:
            contact_parts.append(val)
    if contact_parts:
        contact_para = doc.add_paragraph()
        contact_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_spacing(contact_para, before=0, after=2)
        contact_run = contact_para.add_run("  |  ".join(contact_parts))
        _set_font(contact_run, size=9, color=COLOR_GRAY)

    # Accent rule
    rule_para = doc.add_paragraph()
    _set_paragraph_spacing(rule_para, before=4, after=8)
    _add_bottom_border(rule_para, color="2B5797", size="6")

    # Date
    date_para = doc.add_paragraph()
    _set_paragraph_spacing(date_para, before=6, after=12)
    date_run = date_para.add_run(datetime.now().strftime("%B %d, %Y"))
    _set_font(date_run, size=11, color=COLOR_DARK)

    # Role reference
    ref_para = doc.add_paragraph()
    _set_paragraph_spacing(ref_para, before=0, after=12)
    ref_run = ref_para.add_run(f"Re: {job.get('title', '')} at {job.get('company', '')}")
    _set_font(ref_run, size=11, bold=True, color=COLOR_BLACK)

    # Body paragraphs
    # Clean up any markdown the AI might have included
    clean_content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
    clean_content = re.sub(r'^[#]+\s*', '', clean_content, flags=re.MULTILINE)

    for paragraph in clean_content.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # Skip lines that look like headers or salutations already in the AI content
        if paragraph.lower().startswith(("dear ", "to whom")):
            p = doc.add_paragraph()
            _set_paragraph_spacing(p, before=0, after=6)
            run = p.add_run(paragraph)
            _set_font(run, size=11, color=COLOR_DARK)
        elif paragraph.lower().startswith(("sincerely", "best regards", "regards", "thank you")):
            continue  # We add our own closing
        else:
            p = doc.add_paragraph()
            _set_paragraph_spacing(p, before=0, after=6)
            run = p.add_run(paragraph)
            _set_font(run, size=11, color=COLOR_DARK)

    # Closing
    doc.add_paragraph()
    closing = doc.add_paragraph()
    _set_paragraph_spacing(closing, before=6, after=2)
    run = closing.add_run("Sincerely,")
    _set_font(run, size=11, color=COLOR_DARK)

    name_para = doc.add_paragraph()
    _set_paragraph_spacing(name_para, before=2, after=0)
    run = name_para.add_run(profile.get("full_name", ""))
    _set_font(run, size=11, bold=True, color=COLOR_BLACK)

    # Save
    job_id = job.get("id", "unknown")
    file_path = RESUMES_DIR / f"{job_id}_cover_letter.docx"
    doc.save(str(file_path))
    logger.info("Cover letter saved to %s", file_path)
    return str(file_path)


# ===========================================================================
# PDF rendering (pure-Python via ReportLab — bundled, no external binary)
#
# The PDF path reuses the exact same parsers (_parse_resume_sections,
# _parse_experience_entries, _parse_education_entries) and the _STYLES presets
# the DOCX path uses, so the two formats stay visually consistent.
# ===========================================================================


def _resolve_resume_style(config: dict | None) -> dict:
    """Pick the _STYLES preset from config — shared by the DOCX/PDF paths."""
    resume_style = "standard"
    if config:
        resume_style = (
            config.get("application_honesty", {}).get("resume_style", "standard").lower()
        )
    return _STYLES.get(resume_style, _STYLES["standard"])


def _document_format(config: dict | None) -> str:
    """Return the configured output format: 'pdf' or 'docx' (default)."""
    if not config:
        return "docx"
    fmt = config.get("application_honesty", {}).get("document_format", "docx")
    return "pdf" if str(fmt).lower() == "pdf" else "docx"


def _pdf_esc(text: str) -> str:
    """Escape text for ReportLab's mini-markup Paragraph parser."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_PDF_SAFE_BULLET = "•"  # • — present in ReportLab's built-in WinAnsi fonts


def _pdf_bullet(marker: str) -> str:
    """The built-in PDF fonts can't render decorative glyphs like ▸/▪/‣ —
    they show up as a .notdef box. Map any non-ASCII marker char to a plain
    round bullet; keep ASCII markers (e.g. '- ') and the • bullet as-is."""
    return "".join(
        ch if (ch.isascii() or ch == _PDF_SAFE_BULLET) else _PDF_SAFE_BULLET
        for ch in marker
    )


def _pdf_font(style_font: str, bold: bool = False, italic: bool = False) -> str:
    """Map a preset font name to a built-in ReportLab font (no font files
    needed — keeps PDFs dependency-free and ATS-safe)."""
    if "times" in style_font.lower():
        if bold and italic:
            return "Times-BoldItalic"
        if bold:
            return "Times-Bold"
        if italic:
            return "Times-Italic"
        return "Times-Roman"
    if bold and italic:
        return "Helvetica-BoldOblique"
    if bold:
        return "Helvetica-Bold"
    if italic:
        return "Helvetica-Oblique"
    return "Helvetica"


def _rl_color(rgb):
    """Convert a python-docx RGBColor (3 ints) to a ReportLab color."""
    from reportlab.lib.colors import HexColor
    return HexColor("#%02X%02X%02X" % tuple(rgb))


def _rl_hex(hex6: str):
    """Convert a bare 6-char hex string ('2B5797') to a ReportLab color."""
    from reportlab.lib.colors import HexColor
    return HexColor("#" + hex6)


def _border_pt(eighths: str) -> float:
    """DOCX border sizes are in 1/8 pt units; convert to points for ReportLab."""
    try:
        return max(0.4, float(eighths) / 8.0)
    except (TypeError, ValueError):
        return 0.75


def _render_resume_pdf(content: str, profile: dict, job: dict, config: dict | None = None) -> str:
    """Render the resume to a styled, ATS-friendly PDF mirroring the DOCX
    layout. Returns the PDF file path."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )

    _ensure_dir()
    s = _resolve_resume_style(config)
    bullet_marker = _pdf_bullet(s["bullet_marker"])
    body = s["body_font"]
    top_m, bot_m, left_m, right_m = s["margins"]

    job_id = job.get("id", "unknown")
    file_path = RESUMES_DIR / f"{job_id}_resume.pdf"

    doc = SimpleDocTemplate(
        str(file_path), pagesize=LETTER,
        topMargin=top_m * inch, bottomMargin=bot_m * inch,
        leftMargin=left_m * inch, rightMargin=right_m * inch,
        title=f"{profile.get('full_name', 'Resume')} — Resume",
    )
    avail_w = LETTER[0] - (left_m + right_m) * inch

    accent = _rl_color(s["accent_color"])
    c_dark = _rl_color(COLOR_DARK)
    c_gray = _rl_color(COLOR_GRAY)
    c_black = _rl_color(COLOR_BLACK)
    story: list = []

    def para(text, font, size, color, *, align=TA_LEFT, bold=False,
             italic=False, sb=0, sa=2, leading=None, left_indent=0,
             first_indent=0):
        st = ParagraphStyle(
            "x", fontName=_pdf_font(font, bold, italic), fontSize=size,
            leading=leading or size * 1.2, textColor=color, alignment=align,
            spaceBefore=sb, spaceAfter=sa, leftIndent=left_indent,
            firstLineIndent=first_indent,
        )
        return Paragraph(text, st)

    # -- Header: name --
    name = profile.get("full_name", "")
    if name:
        disp = name.upper() if s["name_uppercase"] else name
        story.append(para(_pdf_esc(disp), s["name_font"], s["name_size"],
                          _rl_color(s["name_color"]), align=TA_CENTER,
                          bold=True, sa=2))

    # -- Header: contact line (clickable links when the preset enables them) --
    contact_bits: list[str] = []
    for field in ("email", "phone", "location"):
        val = profile.get(field, "")
        if val:
            contact_bits.append(_pdf_esc(val))
    link_hex = "#%02X%02X%02X" % tuple(s["accent_color"])
    for field in ("linkedin", "portfolio", "github"):
        url = profile.get(field, "")
        if not url:
            continue
        clean = re.sub(r"^https?://", "", url).rstrip("/")
        if not clean:
            continue
        full = url if url.startswith("http") else f"https://{clean}"
        if s["hyperlinks"]:
            contact_bits.append(
                f'<a href="{_pdf_esc(full)}" color="{link_hex}">{_pdf_esc(clean)}</a>'
            )
        else:
            contact_bits.append(_pdf_esc(clean))
    if contact_bits:
        story.append(para("  |  ".join(contact_bits), body, 9, c_gray,
                          align=TA_CENTER, sa=2))

    if s["name_rule"]:
        story.append(HRFlowable(
            width="100%", thickness=_border_pt(s["name_rule_size"]),
            color=_rl_hex(s["name_rule_color"]), spaceBefore=4, spaceAfter=4,
        ))

    sections = _parse_resume_sections(content)

    def section_header(title: str):
        story.append(para(_pdf_esc(title.upper()), body, s["header_size"],
                          _rl_color(s["header_color"]), bold=True, sb=10, sa=3))
        if s["header_underline"]:
            story.append(HRFlowable(
                width="100%", thickness=_border_pt(s["header_underline_size"]),
                color=_rl_hex(s["header_underline_color"]),
                spaceBefore=1, spaceAfter=4,
            ))

    # -- Summary --
    summary_text = sections.get("summary", "")
    if summary_text:
        section_header("Summary")
        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", summary_text).strip()
        story.append(para(_pdf_esc(clean), body, 10.5, c_dark, sb=2, sa=4))

    # -- Skills --
    skills_text = sections.get("skills", "")
    if skills_text:
        section_header("Technical Skills")
        skills_text = re.sub(r"\*\*(.+?)\*\*", r"\1", skills_text)
        skills_text = re.sub(r"^[-*•]\s*", "", skills_text, flags=re.MULTILINE)
        skill_lines = [ln.strip() for ln in skills_text.split("\n") if ln.strip()]
        if any(":" in ln for ln in skill_lines):
            for ln in skill_lines:
                if ":" in ln:
                    cat, sk = ln.split(":", 1)
                    txt = f"<b>{_pdf_esc(cat.strip())}:</b> {_pdf_esc(sk.strip())}"
                else:
                    txt = _pdf_esc(ln)
                story.append(para(txt, body, 10.5, c_dark, sb=1, sa=1))
        else:
            all_skills: list[str] = []
            for ln in skill_lines:
                all_skills.extend(p.strip() for p in ln.split(",") if p.strip())
            story.append(para(_pdf_esc(", ".join(all_skills)), body, 10.5,
                              c_dark, sb=2, sa=4))

    def _no_border_table(left_flow, right_flow, right_w):
        t = Table([[left_flow, right_flow]],
                  colWidths=[avail_w - right_w, right_w])
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    # -- Experience --
    exp_text = sections.get("experience", "")
    if exp_text:
        section_header("Professional Experience")
        right_w = 1.7 * inch
        for entry in _parse_experience_entries(exp_text):
            if s["entry_layout"] == "inline":
                left = f'<b>{_pdf_esc(entry["title"])}</b>'
                if entry["company"]:
                    sep_hex = "#%02X%02X%02X" % tuple(s["accent_color"])
                    left += (f'<font color="{sep_hex}">  ·  </font>'
                             f'{_pdf_esc(entry["company"])}')
                left_p = para(left, body, 11, c_black, sb=6, sa=2)
                if entry["dates"]:
                    right_p = para(_pdf_esc(entry["dates"]), body, 10, c_gray,
                                   align=TA_RIGHT, italic=True, sb=6, sa=2)
                    story.append(_no_border_table(left_p, right_p, right_w))
                else:
                    story.append(left_p)
            else:
                title_p = para(f'<b>{_pdf_esc(entry["title"])}</b>', body, 11,
                               c_black, sb=6, sa=0)
                if entry["dates"]:
                    right_p = para(_pdf_esc(entry["dates"]), body, 10, c_gray,
                                   align=TA_RIGHT, italic=True, sb=6, sa=0)
                    story.append(_no_border_table(title_p, right_p, right_w))
                else:
                    story.append(title_p)
                if entry["company"]:
                    story.append(para(_pdf_esc(entry["company"]), body, 10.5,
                                      c_gray, italic=True, sb=0, sa=2))
            marker_hex = "#%02X%02X%02X" % tuple(s["bullet_marker_color"])
            for bullet in entry["bullets"]:
                txt = (f'<font color="{marker_hex}">'
                       f'{_pdf_esc(bullet_marker)}</font>'
                       f'{_pdf_esc(bullet)}')
                story.append(para(txt, body, 10.5, c_dark, sb=1, sa=1,
                                  left_indent=18, first_indent=-12))

    # -- Education --
    edu_text = sections.get("education", "")
    if edu_text:
        section_header("Education")
        for entry in _parse_education_entries(edu_text):
            txt = f'<b>{_pdf_esc(entry["degree"])}</b>'
            if entry["school"]:
                tail = f'  —  {entry["school"]}'
                if entry["year"]:
                    tail += f'  ({entry["year"]})'
                txt += f'<font color="#666666">{_pdf_esc(tail)}</font>'
            elif entry["year"]:
                txt += f'<font color="#666666">  ({_pdf_esc(entry["year"])})</font>'
            story.append(para(txt, body, 10.5, c_black, sb=3, sa=1))

    # -- Certifications --
    cert_text = sections.get("certifications", "")
    if cert_text:
        section_header("Certifications")
        marker_hex = "#%02X%02X%02X" % tuple(s["bullet_marker_color"])
        for line in cert_text.split("\n"):
            line = re.sub(r"\*\*(.+?)\*\*", r"\1", line.strip())
            clean = line.lstrip("-*•– ").strip()
            if clean:
                txt = (f'<font color="{marker_hex}">'
                       f'{_pdf_esc(bullet_marker)}</font>{_pdf_esc(clean)}')
                story.append(para(txt, body, 10.5, c_dark, sb=1, sa=1,
                                  left_indent=18, first_indent=-12))

    # -- References (verbatim from profile, never AI-touched) --
    refs = [r for r in (profile.get("references", []) or [])
            if (r.get("name") or "").strip()]
    if refs:
        section_header("References")
        for ref in refs:
            line = f'<b>{_pdf_esc(ref["name"].strip())}</b>'
            position = (ref.get("position") or "").strip()
            if position:
                line += f'<i><font color="#666666">  —  {_pdf_esc(position)}</font></i>'
            story.append(para(line, body, 10.5, c_black, sb=4, sa=1))
            bits = [b for b in ((ref.get("email") or "").strip(),
                                (ref.get("phone") or "").strip()) if b]
            if bits:
                story.append(para(_pdf_esc("  |  ".join(bits)), body, 10,
                                  c_dark, sb=0, sa=2))

    doc.build(story)
    logger.info("Resume PDF saved to %s", file_path)
    return str(file_path)


def _render_cover_letter_pdf(content: str, profile: dict, job: dict) -> str:
    """Render the cover letter to PDF, mirroring generate_cover_letter_docx."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    )

    _ensure_dir()
    job_id = job.get("id", "unknown")
    file_path = RESUMES_DIR / f"{job_id}_cover_letter.pdf"

    doc = SimpleDocTemplate(
        str(file_path), pagesize=LETTER,
        topMargin=inch, bottomMargin=inch, leftMargin=inch, rightMargin=inch,
        title=f"{profile.get('full_name', 'Cover Letter')} — Cover Letter",
    )
    c_dark = _rl_color(COLOR_DARK)
    c_gray = _rl_color(COLOR_GRAY)
    c_black = _rl_color(COLOR_BLACK)
    story: list = []

    def para(text, size, color, *, align=TA_LEFT, bold=False, sb=0, sa=6):
        st = ParagraphStyle(
            "x", fontName=_pdf_font("Calibri", bold), fontSize=size,
            leading=size * 1.3, textColor=color, alignment=align,
            spaceBefore=sb, spaceAfter=sa,
        )
        return Paragraph(text, st)

    name = profile.get("full_name", "")
    if name:
        story.append(para(_pdf_esc(name.upper()), 16, c_black,
                          align=TA_CENTER, bold=True, sa=2))
    contact_parts = [profile.get(f, "") for f in ("email", "phone", "location")]
    contact_parts = [_pdf_esc(p) for p in contact_parts if p]
    if contact_parts:
        story.append(para("  |  ".join(contact_parts), 9, c_gray,
                          align=TA_CENTER, sa=2))
    story.append(HRFlowable(width="100%", thickness=0.75,
                            color=_rl_hex("2B5797"), spaceBefore=4, spaceAfter=8))

    story.append(para(_pdf_esc(datetime.now().strftime("%B %d, %Y")), 11,
                      c_dark, sb=6, sa=12))
    story.append(para(
        f'<b>Re: {_pdf_esc(job.get("title", ""))} at '
        f'{_pdf_esc(job.get("company", ""))}</b>', 11, c_black, sa=12))

    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
    clean = re.sub(r"^[#]+\s*", "", clean, flags=re.MULTILINE)
    for paragraph in clean.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        low = paragraph.lower()
        if low.startswith(("sincerely", "best regards", "regards", "thank you")):
            continue  # we add our own closing
        story.append(para(_pdf_esc(paragraph), 11, c_dark, sa=6))

    story.append(Spacer(1, 12))
    story.append(para("Sincerely,", 11, c_dark, sb=6, sa=2))
    story.append(para(f'<b>{_pdf_esc(profile.get("full_name", ""))}</b>', 11,
                      c_black, sb=2, sa=0))

    doc.build(story)
    logger.info("Cover letter PDF saved to %s", file_path)
    return str(file_path)


# ---------------------------------------------------------------------------
# Public dispatchers — pick format from config.application_honesty.document_format
# ---------------------------------------------------------------------------


def generate_resume(content: str, profile: dict, job: dict, config: dict | None = None) -> str:
    """Generate the resume in the user's configured format.

    Always writes the DOCX (the reliable internal artifact the autofill
    adapters and Applicant Assist drag/drop flow rely on). When the format is
    "pdf", also renders a PDF and returns its path; if PDF rendering fails,
    falls back to the DOCX so the pipeline never breaks.
    """
    docx_path = generate_resume_docx(content, profile, job, config)
    if _document_format(config) == "pdf":
        try:
            return _render_resume_pdf(content, profile, job, config)
        except Exception:
            logger.warning(
                "Resume PDF rendering failed — serving DOCX instead", exc_info=True
            )
    return docx_path


def generate_cover_letter(content: str, profile: dict, job: dict, config: dict | None = None) -> str:
    """Generate the cover letter in the user's configured format.

    Always writes the DOCX; additionally renders a PDF when configured,
    falling back to the DOCX if PDF rendering fails.
    """
    docx_path = generate_cover_letter_docx(content, profile, job)
    if _document_format(config) == "pdf":
        try:
            return _render_cover_letter_pdf(content, profile, job)
        except Exception:
            logger.warning(
                "Cover letter PDF rendering failed — serving DOCX instead",
                exc_info=True,
            )
    return docx_path
