"""Expose a retriever to the model as a local (read-only) tool."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kit.rag.base import Retriever
from kit.tools.registry import ToolRegistry

DEFAULT_DESCRIPTION = (
    "Search the practice's policy documents (refills, scheduling, billing, after-hours). "
    "Cite the chunk_id of every passage you rely on. "
    "If nothing relevant comes back, say so rather than guessing."
)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    k: int = Field(4, ge=1, le=8)


def register_search_tool(
    registry: ToolRegistry, retriever: Retriever, *, name: str = "search_docs", description: str | None = None
) -> None:
    """Register `retriever` as a local tool. Search reads only; it needs no human approval."""

    def search(args: SearchArgs) -> dict:
        hits = retriever.search(args.query, k=args.k)
        return {
            "hits": [
                {
                    "chunk_id": h.chunk_id,
                    "doc_id": h.doc_id,
                    "heading": h.meta.get("heading", ""),
                    "text": h.text,
                }
                for h in hits
            ]
        }

    search.__doc__ = description or DEFAULT_DESCRIPTION
    # The registry resolves type hints with get_type_hints; give it the module-level SearchArgs.
    registry.tool(search, name=name)
