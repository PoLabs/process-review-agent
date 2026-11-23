"""Retrieval types and document prep.

Retrieval quality is mostly a data-prep problem: mangled chunks sink RAG regardless of the model.
So chunking happens in one visible place: clean the text, split on headings and paragraphs, pack
paragraphs into windows, and carry a little overlap between consecutive windows so a sentence cut at
a boundary is still whole in one of them.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    id: str  # "{doc_id}#{n}"
    doc_id: str
    text: str
    meta: dict = Field(default_factory=dict)  # heading path, source filename


class Hit(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    text: str
    meta: dict = Field(default_factory=dict)


class Retriever(Protocol):
    def search(self, query: str, k: int = 4) -> list[Hit]: ...


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keeps \t and \n; both are normalized below
_DECORATION = re.compile(r"([=*_~\-#+])\1{3,}")  # ====, ****, ----: runs of 4+ of one symbol
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")


def clean_text(raw: str) -> str:
    """Normalize whitespace, drop control characters and decorative symbol runs."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _DECORATION.sub("", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break a paragraph longer than one window at whitespace."""
    pieces = []
    while len(paragraph) > limit:
        cut = paragraph.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        pieces.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()
    return [*pieces, paragraph] if paragraph else pieces


def _overlap_tail(text: str, overlap: int) -> str:
    """Last `overlap` characters of a window, trimmed so they start on a whole word."""
    if overlap <= 0:
        return ""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    if not text[-overlap - 1].isspace() and " " in tail:
        tail = tail.split(" ", 1)[1]
    return tail


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """Split cleaned text into (heading path, paragraphs) sections, one per heading."""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    paragraphs: list[str] = []
    path = ""

    def flush() -> None:
        if paragraphs:
            sections.append((path, paragraphs.copy()))
            paragraphs.clear()

    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        body: list[str] = []
        for line in lines:
            m = _HEADING.match(line.strip())
            if m:
                if body:
                    paragraphs.append(" ".join(body))
                    body = []
                flush()
                level = len(m.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, m.group(2)))
                path = " > ".join(title for _, title in stack)
            elif line.strip():
                body.append(line.strip())
        if body:
            paragraphs.append(" ".join(body))
    flush()
    return sections


def chunk_document(
    doc_id: str, raw: str, *, source: str = "", chunk_chars: int = 1200, overlap: int = 200
) -> list[Chunk]:
    """Clean `raw`, then pack paragraphs (within one heading section) into ~chunk_chars windows."""
    chunks: list[Chunk] = []
    for heading, paragraphs in _sections(clean_text(raw)):
        pieces = [p for para in paragraphs for p in _split_long(para, chunk_chars)]
        windows: list[str] = []
        current = ""
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > chunk_chars:
                windows.append(current)
                current = f"{_overlap_tail(current, overlap)}\n\n{piece}".strip()
            else:
                current = f"{current}\n\n{piece}" if current else piece
        if current:
            windows.append(current)
        for window in windows:
            chunks.append(
                Chunk(
                    id=f"{doc_id}#{len(chunks)}",
                    doc_id=doc_id,
                    text=window,
                    meta={"heading": heading, "source": source},
                )
            )
    return chunks
