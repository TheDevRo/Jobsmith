"""
section_edit.py — split a tailored document into sections and splice one back.

A resume's sections are the ALL-CAPS header lines the document parser accepts
(SUMMARY, SKILLS, EXPERIENCE…); a cover letter's are its paragraphs. Every
split is lossless: "".join(chunks) == text, so replacing one chunk leaves every
other byte of the document untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .fact_guard import RESUME_HEADERS

WHOLE = "whole"


@dataclass
class Chunk:
    id: str          # what the API accepts as `section`
    label: str       # what the UI shows
    text: str        # raw text, including its header line / trailing blank lines
    editable: bool   # False for text before the first header and for blank separators


def _header_of(line: str) -> str | None:
    cleaned = re.sub(r"^[#*_`]+\s*", "", line.strip()).strip()
    return cleaned.rstrip(": ") if cleaned.upper().rstrip(": ") in RESUME_HEADERS else None


def split_resume(text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[str] = []
    header: str | None = None
    for line in text.splitlines(keepends=True):
        found = _header_of(line)
        if found is not None:
            if current:
                chunks.append(_resume_chunk(header, "".join(current)))
            header, current = found, [line]
        else:
            current.append(line)
    if current:
        chunks.append(_resume_chunk(header, "".join(current)))
    return chunks


def _resume_chunk(header: str | None, text: str) -> Chunk:
    if header is None:
        return Chunk("preamble", "", text, False)
    return Chunk(header.upper(), header, text, True)


def split_cover_letter(text: str) -> list[Chunk]:
    """Paragraphs and the blank-line gaps between them, in order."""
    chunks: list[Chunk] = []
    n = 0
    for part in re.split(r"(\n[ \t]*\n\s*)", text):
        if not part:
            continue
        if re.fullmatch(r"\n[ \t]*\n\s*", part):
            chunks.append(Chunk("gap", "", part, False))
        else:
            n += 1
            first = part.strip().splitlines()[0] if part.strip() else ""
            chunks.append(Chunk(f"paragraph:{n}", f"Paragraph {n}: {first[:40]}", part, bool(part.strip())))
    return chunks


def split(target: str, text: str) -> list[Chunk]:
    return split_resume(text) if target == "resume" else split_cover_letter(text)


def options(target: str, text: str) -> list[dict]:
    """The sections the UI can offer, in document order."""
    return [{"id": c.id, "label": c.label} for c in split(target, text) if c.editable]


def find(target: str, text: str, section: str) -> tuple[list[Chunk], int] | None:
    """The chunks and the index of the requested section, or None if there is no such section."""
    chunks = split(target, text)
    wanted = (section or "").strip().upper() if target == "resume" else (section or "").strip().lower()
    for i, c in enumerate(chunks):
        if c.editable and (c.id == wanted):
            return chunks, i
    return None


def read_only_context(chunks: list[Chunk], index: int) -> str:
    """The rest of the document with the edited chunk replaced by a marker."""
    return "".join(
        "[... the section being edited ...]\n" if i == index else c.text for i, c in enumerate(chunks)
    )


def splice(target: str, chunks: list[Chunk], index: int, revised: str) -> str:
    """Put `revised` in place of chunk `index`; every other chunk is kept byte-for-byte."""
    old = chunks[index]
    new = fit(target, old, revised)
    return "".join(new if i == index else c.text for i, c in enumerate(chunks))


def fit(target: str, old: Chunk, revised: str) -> str:
    """Make the model's answer fit the slot: keep our trailing whitespace, and for a
    resume, make sure it starts with this section's header and carries no other header."""
    revised = revised.strip("\n").rstrip()
    trailing = old.text[len(old.text.rstrip()):]
    if target == "resume":
        lines = revised.splitlines()
        kept: list[str] = []
        for j, line in enumerate(lines):
            found = _header_of(line)
            if found is not None and (j > 0 or found.upper() != old.id):
                if j == 0:
                    continue          # a different header up front: drop the line, keep the body
                break                 # the model started on the next section: cut it off
            kept.append(line)
        body = kept
        if not body or _header_of(body[0]) is None:
            body = [old.text.splitlines()[0].rstrip("\r\n"), *body]
        revised = "\n".join(body).rstrip()
    return revised + trailing
