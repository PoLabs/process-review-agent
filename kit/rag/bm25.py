"""Local keyword retrieval with BM25. This is the default path: nothing leaves the machine."""

from __future__ import annotations

import re
from pathlib import Path

from rank_bm25 import BM25Okapi

from kit.rag.base import Chunk, Hit, chunk_document

_STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from how i if in is it my of on or our so that the "
    "their there this to was we what when where which who will with you your".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens minus stopwords."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]


class BM25Retriever:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        # The heading is part of what a chunk is about, so index it alongside the body text.
        self._bm25 = BM25Okapi([tokenize(f"{c.meta.get('heading', '')} {c.text}") for c in chunks] or [[""]])

    @classmethod
    def from_dir(
        cls,
        path: str | Path,
        patterns: tuple[str, ...] = ("*.md", "*.txt"),
        chunk_chars: int = 1200,
        overlap: int = 200,
    ) -> BM25Retriever:
        files = sorted({f for pattern in patterns for f in Path(path).glob(pattern)})
        chunks: list[Chunk] = []
        for f in files:
            raw = f.read_text(encoding="utf-8", errors="replace")
            chunks += chunk_document(f.stem, raw, source=f.name, chunk_chars=chunk_chars, overlap=overlap)
        return cls(chunks)

    def search(self, query: str, k: int = 4) -> list[Hit]:
        tokens = tokenize(query)
        if not tokens or not self.chunks:
            return []
        scores = self._bm25.get_scores(tokens)
        # Highest score first; equal scores fall back to chunk id so results are reproducible.
        ranked = sorted(zip(self.chunks, scores, strict=True), key=lambda cs: (-cs[1], cs[0].id))
        return [
            Hit(chunk_id=c.id, doc_id=c.doc_id, score=float(s), text=c.text, meta=c.meta)
            for c, s in ranked[:k]
            if s > 0
        ]
