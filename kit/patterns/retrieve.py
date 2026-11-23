"""Retrieval node: one LangGraph node that answers `state["query"]` from tiered retrievers.

    cascade   try tiers in order (cheap and exact first); stop at the first whose top hit clears
              that tier's min_score. Deterministic: no model call.
    agentic   each tier becomes a `search_<tier>` tool and a small react loop lets the model pick.

Both write `{"retrieval": {"mode", "tier", "hits", "tried", "errors"}}`. A tier that raises (a rate
limit, an outage) is skipped and named in "errors"; the cascade moves on rather than failing the
run. The `retrieve` trace event holds
counts and scores only, never the query or the passages. In agentic mode the registry also traces
each search tool's input and output (query and passages); tracing sanitizes them: redacted by
default, omitted under content="none". That is a backstop, not permission: the corpus must be
non-PHI by policy.

The usual stack, ready made:

    tiers = standard_tiers(chunks)     # keyword -> bm25 -> vector (only if VOYAGE_API_KEY is set)
    node  = build_retrieve_node(tiers, mode="cascade")

Build the tiers once and reuse them: the vector index is embedded on the first search that falls
through to it and kept, so a run the local tiers answer makes no embedding call at all.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from kit import llm, tracing
from kit.patterns import react
from kit.rag.base import Chunk, Hit, Retriever
from kit.rag.bm25 import BM25Retriever, tokenize
from kit.rag.tool import register_search_tool
from kit.tools.registry import ToolRegistry

EXACT_PHRASE_SCORE = 1.0
ALL_TERMS_SCORE = 0.8

_AGENTIC_SYSTEM = (
    "Answer by retrieving passages. Call the search tools to find passages relevant to the user's "
    "query; prefer the cheapest tool that is likely to work and try another only if it returns "
    "nothing useful. When you have what you need, reply with one short sentence."
)


@dataclass
class Tier:
    name: str  # lowercase letters, digits, underscores: it becomes the tool name search_<name>
    retriever: Retriever
    min_score: float  # cascade stops here when the top hit scores at least this
    description: str = ""  # shown to the model in agentic mode


class KeywordRetriever:
    """Deterministic first tier: the query as an exact phrase (1.0), else every query term present (0.8)."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def search(self, query: str, k: int = 4) -> list[Hit]:
        phrase = " ".join(query.lower().split())
        terms = set(tokenize(query))
        if not phrase or not terms:
            return []
        hits = []
        for c in self.chunks:
            haystack = f"{c.meta.get('heading', '')} {c.text}".lower()
            if phrase in " ".join(haystack.split()):
                score = EXACT_PHRASE_SCORE
            elif terms <= set(tokenize(haystack)):
                score = ALL_TERMS_SCORE
            else:
                continue
            hits.append(Hit(chunk_id=c.id, doc_id=c.doc_id, score=score, text=c.text, meta=c.meta))
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:k]


class LazyRetriever:
    """A retriever built on its first search and kept: wrap anything that costs a call to build
    (an embedded index, a Pinecone client). `reset()` drops it so the next search rebuilds."""

    def __init__(self, build: Callable[[], Retriever]) -> None:
        self._build, self._built, self._lock = build, None, threading.Lock()

    def search(self, query: str, k: int = 4) -> list[Hit]:
        with self._lock:
            if self._built is None:
                self._built = self._build()
            built = self._built
        return built.search(query, k=k)

    def reset(self) -> None:
        with self._lock:
            self._built = None


def vector_index(chunks: list[Chunk]) -> Retriever:
    """Pinecone when PINECONE_INDEX is set (an index someone already provisioned and loaded),
    else in-memory cosine over `chunks` embedded with kit.llm.embed (one call for the corpus)."""
    if index := os.getenv("PINECONE_INDEX"):
        from kit.rag.pinecone_adapter import PineconeRetriever

        return PineconeRetriever(index, namespace=os.getenv("PINECONE_NAMESPACE"))
    from kit.rag.vector import VectorRetriever

    return VectorRetriever(chunks)


def standard_tiers(
    chunks: list[Chunk],
    *,
    bm25: Retriever | None = None,
    vector: Retriever | None = None,
    keyword_min: float = ALL_TERMS_SCORE,
    bm25_min: float = 3.0,
    vector_min: float = 0.4,
) -> list[Tier]:
    """keyword -> bm25 -> vector, cheapest and most exact first.

    keyword  exact phrase (1.0) or every term present (0.8)    local, deterministic
    bm25     keyword relevance (scores are corpus-relative)     local, deterministic
    vector   cosine over Voyage embeddings, or Pinecone         only when VOYAGE_API_KEY is set;
                                                                built lazily (LazyRetriever)
    Pass `bm25` to reuse an index you already built, `vector` to share one lazy vector index
    across calls. The minimum scores are starting points: tune them against the flow's eval.
    """
    out = [
        Tier("keyword", KeywordRetriever(chunks), min_score=keyword_min),
        Tier("bm25", bm25 if bm25 is not None else BM25Retriever(chunks), min_score=bm25_min),
    ]
    if os.getenv("VOYAGE_API_KEY"):
        lazy = vector if vector is not None else LazyRetriever(lambda: vector_index(chunks))
        out.append(Tier("vector", lazy, min_score=vector_min))
    return out


def _cascade(tiers: list[Tier], query: str, floor: float | None, k: int) -> dict:
    tried: list[str] = []
    errors: list[str] = []
    for tier in tiers:
        tried.append(tier.name)
        try:
            hits = tier.retriever.search(query, k=k)
        except Exception as e:  # a tier that fails is skipped, never fatal: the next one may answer
            errors.append(tier.name)
            tracing.record("retrieve_tier_error", tier=tier.name, error=type(e).__name__)
            continue
        bar = tier.min_score if floor is None else max(tier.min_score, floor)
        if hits and hits[0].score >= bar:
            return {"tier": tier.name, "hits": hits, "tried": tried, "errors": errors}
    return {"tier": None, "hits": [], "tried": tried, "errors": errors}


def _agentic(
    tiers: list[Tier], query: str, floor: float | None, k: int, role: llm.Role, registry: ToolRegistry | None
) -> dict:
    registry = registry if registry is not None else ToolRegistry()
    collected: dict[str, Hit] = {}  # chunk_id -> hit, in the order the tools returned them
    used: list[str] = []
    first_tier: list[str] = []  # the first tier to return a hit that cleared its bar

    def wrap(tier: Tier) -> Retriever:
        bar = tier.min_score if floor is None else max(tier.min_score, floor)

        class Recorder:
            def search(self, q: str, k: int = 4) -> list[Hit]:
                hits = [h for h in tier.retriever.search(q, k=k) if h.score >= bar]
                if tier.name not in used:
                    used.append(tier.name)
                if hits and not first_tier:
                    first_tier.append(tier.name)
                for h in hits:
                    collected.setdefault(h.chunk_id, h)
                return hits

        return Recorder()

    for tier in tiers:
        desc = tier.description or f"Search the '{tier.name}' index. Cite the chunk_id of each passage."
        register_search_tool(registry, wrap(tier), name=f"search_{tier.name}", description=desc)
    graph = react.build(
        role=role,
        system=_AGENTIC_SYSTEM,
        registry=registry,
        tool_names=[f"search_{t.name}" for t in tiers],
        max_steps=4,
        name="retrieve",
    )
    graph.invoke({"messages": [{"role": "user", "content": query}]})
    hits = list(collected.values())[:k]
    # A search tool that raises comes back to the model as is_error (kit.tools.registry), so the
    # loop carries on; the registry's trace names it.
    return {"tier": first_tier[0] if first_tier else None, "hits": hits, "tried": used, "errors": []}


def build_retrieve_node(
    tiers: list[Tier],
    *,
    mode: Literal["cascade", "agentic"] = "cascade",
    min_score: float | None = None,
    role: llm.Role = llm.Role.WORKER,
    registry: ToolRegistry | None = None,
    k: int = 4,
):
    """Return a node `(state) -> {"retrieval": {...}}` that reads `state["query"]`.

    `min_score` is an extra floor on top of each tier's own (hits below it are never returned).
    `registry` (agentic only) receives the `search_<tier>` tools; omit it to use a private one.
    When no tier clears its bar the result is {"tier": None, "hits": [], "tried": [all tiers]}.
    """
    if mode not in ("cascade", "agentic"):
        raise ValueError(f"mode must be 'cascade' or 'agentic', got {mode!r}")
    if not tiers:
        raise ValueError("build_retrieve_node needs at least one Tier")
    for t in tiers:
        if not re.fullmatch(r"[a-z0-9_]+", t.name):
            raise ValueError(f"Tier name {t.name!r} must be lowercase letters, digits and underscores")
    if len({t.name for t in tiers}) != len(tiers):
        raise ValueError("Tier names must be unique")

    def retrieve(state: dict) -> dict:
        query = state["query"]
        if mode == "cascade":
            out = _cascade(tiers, query, min_score, k)
        else:
            out = _agentic(tiers, query, min_score, k, role, registry)
        top = out["hits"][0].score if out["hits"] else None
        tracing.record(
            "retrieve",
            mode=mode,
            tier=out["tier"],
            tried=out["tried"],
            n_hits=len(out["hits"]),
            top_score=top,
            errors=out["errors"],
        )
        return {
            "retrieval": {
                "mode": mode,
                "tier": out["tier"],
                "hits": [h.model_dump() for h in out["hits"]],
                "tried": out["tried"],
                "errors": out["errors"],
            }
        }

    return retrieve
