"""Optional cloud retrieval via Pinecone. Human-gated; the local BM25 path is the default.

Never index patient data. Index only non-PHI documents such as the practice's public policies.

Creating or deleting an index is a durable cloud artifact, so both are gated the same way as an
external tool: an `Approval` bound to this exact call, plus a written teardown note, checked
before `pinecone` is even imported. Every approved action is recorded in the ledger.
Writing vectors (`upsert_chunks`, `ingest_dir`) is gated the same way: the approval is bound to the
index, namespace and a digest of the exact chunks, so approving one batch never approves another.

    python -m kit.rag.pinecone_adapter ingest <index> <dir> [--namespace NS] [--approve DIGEST]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from kit import llm
from kit.boundary import Approval, Ledger, digest
from kit.rag.base import Chunk, Hit
from kit.rag.vector import load_chunks

_INSTALL_HINT = "pinecone is not installed; install it with `uv add pinecone` to use the cloud path"
_MIN_TEARDOWN_CHARS = 20


def _pinecone_client():
    try:
        from pinecone import Pinecone
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e
    return Pinecone(api_key=os.environ["PINECONE_API_KEY"])


def _index(name: str):
    """Open an index; PINECONE_INDEX_HOST (if set) skips the control-plane lookup by name."""
    pc = _pinecone_client()
    host = os.getenv("PINECONE_INDEX_HOST")
    return pc.Index(host=host) if host else pc.Index(name)


def _require(approval: Approval, tool: str, raw_input: dict) -> None:
    if not approval.authorizes(tool, approval.tool_use_id, raw_input):
        raise PermissionError(f"{tool} needs a human approval bound to exactly this call: {raw_input}")


class PineconeRetriever:
    def __init__(
        self,
        index_name: str,
        embed: Callable[..., list[list[float]]] = llm.embed,
        namespace: str | None = None,
    ) -> None:
        self.index_name = index_name
        self.embed = embed
        self.namespace = namespace

    def search(self, query: str, k: int = 4) -> list[Hit]:
        index = _index(self.index_name)
        result = index.query(
            vector=self.embed([query], input_type="query")[0],
            top_k=k,
            namespace=self.namespace,
            include_metadata=True,
        )
        hits = []
        for m in result["matches"]:
            meta = dict(m.get("metadata") or {})
            text = meta.pop("text", "")
            hits.append(
                Hit(
                    chunk_id=m["id"],
                    doc_id=meta.get("doc_id", m["id"].split("#")[0]),
                    score=float(m["score"]),
                    text=text,
                    meta=meta,
                )
            )
        return hits


def provision_index(
    name: str, dimension: int, *, approval: Approval, teardown: str, ledger: Ledger | None = None
) -> None:
    """Create a serverless index. Requires approval of this exact call and a written teardown note."""
    _require(approval, "provision_index", {"name": name, "dimension": dimension})
    if len(teardown.strip()) < _MIN_TEARDOWN_CHARS:
        raise PermissionError("provision_index needs a written teardown note (at least 20 characters)")
    (ledger or Ledger()).append(approval, teardown=teardown)
    try:
        from pinecone import ServerlessSpec
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e
    _pinecone_client().create_index(
        name=name,
        dimension=dimension,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1"),
    )


def teardown_index(name: str, *, approval: Approval, ledger: Ledger | None = None) -> None:
    """Delete an index. Requires approval of this exact call."""
    _require(approval, "teardown_index", {"name": name})
    (ledger or Ledger()).append(approval)
    _pinecone_client().delete_index(name)


def upsert_input(index_name: str, chunks: list[Chunk], namespace: str | None = None) -> dict:
    """The exact call an approval for `upsert_chunks` must be bound to (chunk content enters as a digest)."""
    content = digest({"chunks": [[c.id, c.text, c.meta] for c in chunks]})
    return {"index_name": index_name, "namespace": namespace, "n_chunks": len(chunks), "chunks": content}


def upsert_chunks(
    index_name: str,
    chunks: list[Chunk],
    *,
    approval: Approval,
    ledger: Ledger | None = None,
    namespace: str | None = None,
    batch: int = 96,
) -> int:
    """Embed `chunks` and write them to the index. Requires approval of this exact call
    (see `upsert_input`); checked before anything is embedded or sent. Returns the count written."""
    _require(approval, "upsert_chunks", upsert_input(index_name, chunks, namespace))
    (ledger or Ledger()).append(approval)
    index = _index(index_name)
    for start in range(0, len(chunks), batch):
        part = chunks[start : start + batch]
        vectors = llm.embed(
            [f"{c.meta.get('heading', '')}\n{c.text}".strip() for c in part],
            input_type="document",
            node="pinecone.upsert",
        )
        index.upsert(
            vectors=[
                {
                    "id": c.id,
                    "values": v,
                    "metadata": {**c.meta, "doc_id": c.doc_id, "text": c.text},
                }
                for c, v in zip(part, vectors, strict=True)
            ],
            namespace=namespace,
        )
    return len(chunks)


def ingest_dir(
    index_name: str,
    path: str | Path,
    *,
    approval: Approval,
    ledger: Ledger | None = None,
    namespace: str | None = None,
    batch: int = 96,
    patterns: tuple[str, ...] = ("*.md", "*.txt"),
) -> int:
    """Chunk the documents under `path` and upsert them. Approval is for `upsert_chunks` over these chunks."""
    chunks = load_chunks(path, patterns)
    return upsert_chunks(
        index_name, chunks, approval=approval, ledger=ledger, namespace=namespace, batch=batch
    )


def run_ingest(argv: list[str] | None = None) -> int:
    """CLI: print the plan and its digest; write only with `--approve <digest>` matching that plan."""
    ap = argparse.ArgumentParser(prog="python -m kit.rag.pinecone_adapter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="chunk, embed and upsert a directory of documents")
    ing.add_argument("index")
    ing.add_argument("dir")
    ing.add_argument("--namespace", default=None)
    ing.add_argument(
        "--approve", metavar="DIGEST", help="digest printed by the dry run, to approve that upsert"
    )
    args = ap.parse_args(argv)

    chunks = load_chunks(args.dir)
    plan = digest(upsert_input(args.index, chunks, args.namespace))
    if not args.approve:
        per_doc = Counter(c.doc_id for c in chunks)
        print(f"Index: {args.index!r}  namespace: {args.namespace!r}  chunks: {len(chunks)}")
        print("Synthetic / non-PHI documents only. Chunks per document:")
        for doc_id, n in sorted(per_doc.items()):
            print(f"  {doc_id}: {n}")
        print(f"Digest: {plan}")
        print(f"Dry run. To write exactly this, re-run with --approve {plan}")
        return 0
    if args.approve != plan:
        print(
            f"Refusing: --approve {args.approve} does not match the current plan ({plan}). "
            "The files, index or namespace changed since review; run without --approve and review again.",
            file=sys.stderr,
        )
        return 2
    approval = Approval(
        tool_use_id=f"cli-ingest-{plan[:8]}",
        tool="upsert_chunks",
        decision="approved",
        approver=os.getenv("USER", "unknown"),
        input_digest=plan,
    )
    n = upsert_chunks(args.index, chunks, approval=approval, namespace=args.namespace)
    print(f"Upserted {n} chunks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_ingest())
