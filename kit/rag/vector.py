"""In-memory vector retrieval: cosine similarity over embeddings from `kit.llm.embed`.

Pure Python, no index to provision. Document text leaves the machine to be embedded, so index
synthetic or de-identified documents only (rule 3).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

from kit import llm
from kit.rag.base import Chunk, Hit, chunk_document

Embedder = Callable[..., list[list[float]]]  # embed(texts, input_type="document"|"query")


def load_chunks(
    path: str | Path,
    patterns: tuple[str, ...] = ("*.md", "*.txt"),
    chunk_chars: int = 1200,
    overlap: int = 200,
) -> list[Chunk]:
    """Chunk every matching file directly under `path` (same rules as BM25Retriever.from_dir)."""
    files = sorted({f for pattern in patterns for f in Path(path).glob(pattern)})
    chunks: list[Chunk] = []
    for f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        chunks += chunk_document(f.stem, raw, source=f.name, chunk_chars=chunk_chars, overlap=overlap)
    return chunks


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm else list(v)


class VectorRetriever:
    def __init__(self, chunks: list[Chunk], embed: Embedder = llm.embed) -> None:
        self.chunks = chunks
        self.embed = embed
        texts = [f"{c.meta.get('heading', '')}\n{c.text}".strip() for c in chunks]
        self._vectors = [_unit(v) for v in embed(texts, input_type="document")] if chunks else []

    @classmethod
    def from_dir(
        cls,
        path: str | Path,
        patterns: tuple[str, ...] = ("*.md", "*.txt"),
        chunk_chars: int = 1200,
        overlap: int = 200,
        embed: Embedder = llm.embed,
    ) -> VectorRetriever:
        return cls(load_chunks(path, patterns, chunk_chars, overlap), embed=embed)

    def search(self, query: str, k: int = 4) -> list[Hit]:
        if not query.strip() or not self.chunks:
            return []
        q = _unit(self.embed([query], input_type="query")[0])
        scores = [sum(a * b for a, b in zip(q, v, strict=True)) for v in self._vectors]
        ranked = sorted(zip(self.chunks, scores, strict=True), key=lambda cs: (-cs[1], cs[0].id))
        return [
            Hit(chunk_id=c.id, doc_id=c.doc_id, score=float(s), text=c.text, meta=c.meta)
            for c, s in ranked[:k]
            if s > 0
        ]
