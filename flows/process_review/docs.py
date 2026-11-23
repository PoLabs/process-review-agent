"""The document world for process_review: load every document version, resolve what is current.

Files live in data/<store>/<doc_id>@<version>.md (format in WORLD.md): frontmatter, `## ` sections
(one section = one chunk), and optionally one ```rules block of JSON rule rows.

Why currency is enforced here, at index time, and not in a prompt: a model that is shown a
superseded limit ("acetone 45 °C") next to the current one ("35 °C") has to be trusted to pick the
right one, and a model that is told "ignore old versions" can still quote them. So the index never
contains them. `DocIndex` keeps, per document, only the highest approved version already in effect
on `today`; drafts, superseded, withdrawn and discontinued versions are loaded (so evals can name
them) but never chunked, retrieved, cited or turned into rules. An approved, in-effect amendment is
merged into its base document: its sections replace the base's sections of the same heading, its
rule rows replace the base's rows with the same key, and the merged version reads "C+A1" so a
citation says which text the reader actually saw.

`rules()` returns the machine-readable rows of the current documents only, each with a Citation
whose chunk_id is the chunk containing the quote. A quote that is in no chunk raises: a rule that
cannot be cited must not exist. `covers()` answers the coverage question (is there any current
document for this chemical / equipment / product) for code, never for a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from flows.process_review.state import STORES, Citation, RuleRow
from kit.patterns.retrieve import LazyRetriever, Tier, standard_tiers, vector_index
from kit.rag.base import Chunk

TODAY = date(2026, 11, 1)
DATA = Path(__file__).parent / "data"
EXEMPT = frozenset({"water"})  # needs no document: never a coverage gap

_STATUSES = {"approved", "draft", "superseded", "withdrawn", "discontinued"}
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
_RULES = re.compile(r"^```rules\n(.*?)\n```[ \t]*$", re.S | re.M)
_HEADING = re.compile(r"^## (.+?)\s*$", re.M)


@dataclass
class DocVersion:
    doc_id: str
    store: str
    title: str
    version: str
    status: str
    effective: date
    amends: str | None
    covers: list[str]
    sections: list[tuple[str, str]]
    rules: list[dict] = field(default_factory=list)


@dataclass
class ResolvedDoc:
    doc_id: str
    version: str  # "C", or "C+A1" with an amendment merged in
    title: str
    store: str
    covers: list[str]
    sections: list[tuple[str, str]]


def _parse(path: Path) -> DocVersion:
    def bad(why: str) -> ValueError:
        return ValueError(f"{path.name}: {why}")

    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if not m:
        raise bad("missing frontmatter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise bad(f"bad frontmatter line {line!r}")
        meta[key.strip()] = value.strip()
    for key in ("doc_id", "title", "store", "version", "status", "effective", "amends", "covers"):
        if key not in meta:
            raise bad(f"frontmatter lacks {key}")
    if f"{meta['doc_id']}@{meta['version']}.md" != path.name:
        raise bad("file name does not match doc_id@version")
    if meta["store"] not in STORES or path.parent.name != meta["store"]:
        raise bad(f"store {meta['store']!r} is not the folder it sits in")
    if meta["status"] not in _STATUSES:
        raise bad(f"unknown status {meta['status']!r}")
    try:
        effective = date.fromisoformat(meta["effective"])
    except ValueError as e:
        raise bad(f"effective is not a date: {e}") from e

    body = m.group(2)
    rules: list[dict] = []
    blocks = _RULES.findall(body)
    if len(blocks) > 1:
        raise bad("more than one rules block")
    if blocks:
        try:
            rules = json.loads(blocks[0])
        except json.JSONDecodeError as e:
            raise bad(f"rules block is not JSON: {e}") from e
        if not isinstance(rules, list):
            raise bad("rules block is not a list")
        body = _RULES.sub("", body)

    parts = _HEADING.split(body)
    if parts[0].strip():
        raise bad("text before the first '## ' heading")
    sections = [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    if not sections or any(not text for _, text in sections):
        raise bad("no sections, or an empty section")
    if len({h for h, _ in sections}) != len(sections):
        raise bad("duplicate section heading")

    dummy = Citation(chunk_id="x", doc_id="x", version="x", quote="x")
    for row in rules:
        quote = row.get("quote") if isinstance(row, dict) else None
        if not quote or not any(quote in text for _, text in sections):
            raise bad(f"rule quote is not in any section: {quote!r}")
        try:
            RuleRow(**{k: v for k, v in row.items() if k != "quote"}, citation=dummy)
        except Exception as e:
            raise bad(f"invalid rule row {row!r}: {e}") from e

    return DocVersion(
        doc_id=meta["doc_id"],
        store=meta["store"],
        title=meta["title"],
        version=meta["version"],
        status=meta["status"],
        effective=effective,
        amends=meta["amends"] or None,
        covers=[c.strip() for c in meta["covers"].split(",") if c.strip()],
        sections=sections,
        rules=rules,
    )


def load_all(root: Path = DATA) -> list[DocVersion]:
    """Parse every document version under root, any status. Raises on a malformed file."""
    docs = [_parse(p) for p in sorted(root.glob("*/*.md"))]
    if not docs:
        raise ValueError(f"no documents under {root}")
    return docs


def _vkey(version: str) -> tuple[int, int, str]:
    return (1, int(version), "") if version.isdigit() else (0, 0, version)


def _rule_key(row: dict) -> tuple:
    kind = row["kind"]
    if kind == "incompatible":
        return (kind, frozenset({row.get("a"), row.get("b")}))
    if kind == "chem_limit":
        return (kind, row.get("chemical"))
    if kind == "equipment":
        return (kind, row.get("equipment"))
    return (kind, row.get("chemical"), row.get("control"))


class DocIndex:
    """The current documents as of `today`, their chunks, rules and retrieval tiers."""

    def __init__(self, today: date = TODAY, root: Path = DATA) -> None:
        self.today, self.root = today, root
        self._all = load_all(root)
        self._current: dict[str, list[tuple[ResolvedDoc, list[dict]]]] = {s: [] for s in STORES}
        self._resolve()
        self._chunks: dict[str, list[Chunk]] = {s: self._build_chunks(s) for s in STORES}
        self._by_id = {c.id: c for s in STORES for c in self._chunks[s]}
        self._vector: dict[str, LazyRetriever] = {}
        self._rules: list[RuleRow] | None = None

    # --- resolution ---------------------------------------------------------------------------

    def _latest(self, docs: list[DocVersion]) -> dict[str, DocVersion]:
        best: dict[str, DocVersion] = {}
        for d in docs:
            if d.status != "approved" or d.effective > self.today:
                continue
            if d.doc_id not in best or _vkey(d.version) > _vkey(best[d.doc_id].version):
                best[d.doc_id] = d
        return best

    def _resolve(self) -> None:
        bases = self._latest([d for d in self._all if d.amends is None])
        amendments = self._latest([d for d in self._all if d.amends is not None])
        for doc_id in sorted(bases):
            base = bases[doc_id]
            sections = list(base.sections)
            rules = list(base.rules)
            version = base.version
            covers = list(base.covers)
            for amd in sorted((a for a in amendments.values() if a.amends == doc_id), key=lambda a: a.doc_id):
                index = {h: i for i, (h, _) in enumerate(sections)}
                for heading, text in amd.sections:
                    if heading in index:
                        sections[index[heading]] = (heading, text)
                    else:
                        sections.append((heading, text))
                replaced = {_rule_key(r) for r in amd.rules}
                rules = [r for r in rules if _rule_key(r) not in replaced] + list(amd.rules)
                covers += [c for c in amd.covers if c not in covers]
                version += f"+A{amd.version}"
            resolved = ResolvedDoc(base.doc_id, version, base.title, base.store, covers, sections)
            self._current[base.store].append((resolved, rules))

    def _build_chunks(self, store: str) -> list[Chunk]:
        out = []
        for doc, _ in self._current[store]:
            for n, (heading, text) in enumerate(doc.sections, start=1):
                out.append(
                    Chunk(
                        id=f"{doc.doc_id}@{doc.version}#{n}",
                        doc_id=doc.doc_id,
                        text=text,
                        meta={
                            "doc_id": doc.doc_id,
                            "version": doc.version,
                            "store": store,
                            "heading": heading,
                            "covers": list(doc.covers),
                            "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        },
                    )
                )
        return out

    # --- queries ------------------------------------------------------------------------------

    def current(self, store: str) -> list[ResolvedDoc]:
        return [d for d, _ in self._current[store]]

    def chunks(self, store: str) -> list[Chunk]:
        return list(self._chunks[store])

    def chunk(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def is_current(self, doc_id: str, version: str) -> bool:
        return any(d.doc_id == doc_id and d.version == version for s in STORES for d in self.current(s))

    def reread(self) -> DocIndex:
        """The store as it is now, read again from disk: a document withdrawn or superseded after
        this index was built is not current in the result. verify uses it at report time."""
        return DocIndex(self.today, self.root)

    def covers(self, store: str, item_id: str) -> bool:
        return item_id in EXEMPT or any(item_id in d.covers for d in self.current(store))

    def all_versions(self) -> list[tuple[str, str, str]]:
        return [(d.doc_id, d.version, d.status) for d in self._all]

    def rules(self) -> list[RuleRow]:
        if self._rules is None:
            out = []
            for store in STORES:
                for doc, rows in self._current[store]:
                    chunks = [c for c in self._chunks[store] if c.meta["doc_id"] == doc.doc_id]
                    for row in rows:
                        quote = row["quote"]
                        hit = next((c for c in chunks if quote in c.text), None)
                        if hit is None:
                            raise ValueError(f"{doc.doc_id}@{doc.version}: quote is in no chunk: {quote!r}")
                        cite = Citation(chunk_id=hit.id, doc_id=doc.doc_id, version=doc.version, quote=quote)
                        out.append(RuleRow(**{k: v for k, v in row.items() if k != "quote"}, citation=cite))
            self._rules = out
        return list(self._rules)

    def tiers(self, store: str) -> list[Tier]:
        chunks = self.chunks(store)
        if store not in self._vector:
            self._vector[store] = LazyRetriever(lambda: vector_index(chunks))
        return standard_tiers(chunks, vector=self._vector[store])
