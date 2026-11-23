"""Long-term agent memory that is PHI-aware.

Rule 3 (no PII on disk) applies to memory exactly as it does to traces: a memory backend is
storage, so nothing identifying goes in. `Memory` is the policy layer every caller goes through;
the stores underneath (`InMemoryMemory`, `RedisMemory`) are dumb and trust it.

  - Namespaces are declared up front (`Namespace`); an unknown one is a ValueError.
  - Every write needs a ttl, capped by the namespace's `max_ttl_s`.
  - Every string in a value (and the key) is checked with `guardrails.redact_patterns`. If an
    identifier pattern is found: phi_policy "refuse" raises `PHIRefused` (naming only the pattern
    kinds, never the text); "redact" stores the redacted text and tags the record redacted=True.
    A key is never redacted, only refused: redacting it would collide keys and still be a name.
  - Every operation is traced with a hashed key and pattern kinds only: no values, no raw keys.

There is deliberately NO namespace that allows raw PHI. A "phi_ok" namespace would need an
encrypted, BAA-covered backend (managed Redis with encryption at rest and in transit, access
audit, a signed BAA); a local Redis or a dict is not that. Until one exists, it is not offered.
Pattern redaction is a backstop, not de-identification: names in free text can slip through.

The safe shape for remembering anything about a person is therefore `CodedLog`: records from
closed vocabularies (a pydantic model of Literals, extra="forbid", so free text fails validation
before any store sees it), kept under `pseudonym(subject_id)` (a keyed HMAC, never the id), with
the namespace's TTL reset on every write. Code writes it; the model never does.

    log = CodedLog(Namespace(name="contacts", phi_policy="refuse", max_ttl_s=72 * 3600), Contact)
    log.append(patient_id, {"intent": "refill", "outcome": "reply_drafted"})
    log.entries(patient_id)   # -> [Contact(...)]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from kit import guardrails, tracing
from kit.tools.registry import ToolRegistry


class PHIRefused(ValueError):
    """A memory write contained identifier patterns where the policy does not allow them."""

    def __init__(self, kinds: list[str]):
        self.kinds = kinds
        super().__init__(
            f"memory write refused: found identifier patterns ({', '.join(kinds)}). "
            "Rephrase without patient identifiers (no names, MRNs, phones, emails, dates, addresses)."
        )


class RecordTags(BaseModel):
    source: str
    redacted: bool
    created_ts: float
    expires_ts: float
    ns: str


class MemoryRecord(BaseModel):
    key: str
    value: dict
    tags: RecordTags


class Namespace(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_-]+$")  # no ':' or glob characters: it is part of a Redis key
    phi_policy: Literal["refuse", "redact"]
    max_ttl_s: int


class MemoryStore(Protocol):
    def put(
        self, ns: str, key: str, value: dict, *, ttl_s: int, redacted: bool = False, source: str = "agent"
    ) -> MemoryRecord: ...
    def get(self, ns: str, key: str) -> MemoryRecord | None: ...
    def search(self, ns: str, text: str, k: int) -> list[MemoryRecord]: ...
    def delete(self, ns: str, key: str) -> None: ...
    def purge(self, ns: str) -> None: ...


# --- shared helpers ----------------------------------------------------------------------------

_TERM = re.compile(r"[a-z0-9]+")
_NS_NAME = re.compile(r"^[a-z0-9_-]+$")


def _terms(text: str) -> set[str]:
    return set(_TERM.findall(text.lower()))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (_strings(k) + _strings(v))]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _score(query: str, rec: MemoryRecord) -> int:
    doc = _terms(" ".join([rec.key, *_strings(rec.value)]))
    return len(_terms(query) & doc)


def _rank(query: str, records: list[MemoryRecord], k: int) -> list[MemoryRecord]:
    scored = [(s, r) for r in records if (s := _score(query, r)) > 0]
    scored.sort(key=lambda sr: (-sr[0], -sr[1].tags.created_ts, sr[1].key))
    return [r for _, r in scored[:k]]


def _make(
    ns: str, key: str, value: dict, now: float, ttl_s: int, redacted: bool, source: str
) -> MemoryRecord:
    tags = RecordTags(source=source, redacted=redacted, created_ts=now, expires_ts=now + ttl_s, ns=ns)
    return MemoryRecord(key=key, value=value, tags=tags)


# --- stores ------------------------------------------------------------------------------------


class InMemoryMemory:
    """Dict-backed store with expiry. `clock` is injectable so tests control time."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._data: dict[tuple[str, str], MemoryRecord] = {}

    def _live(self, ns: str) -> list[MemoryRecord]:
        now = self._clock()
        for ident in [i for i, r in self._data.items() if r.tags.expires_ts <= now]:
            del self._data[ident]
        return [r for (n, _), r in self._data.items() if n == ns]

    def put(
        self, ns: str, key: str, value: dict, *, ttl_s: int, redacted: bool = False, source: str = "agent"
    ) -> MemoryRecord:
        rec = _make(ns, key, value, self._clock(), ttl_s, redacted, source)
        self._data[(ns, key)] = rec
        return rec

    def get(self, ns: str, key: str) -> MemoryRecord | None:
        self._live(ns)
        return self._data.get((ns, key))

    def search(self, ns: str, text: str, k: int) -> list[MemoryRecord]:
        return _rank(text, self._live(ns), k)

    def delete(self, ns: str, key: str) -> None:
        self._data.pop((ns, key), None)

    def purge(self, ns: str) -> None:
        for ident in [i for i in self._data if i[0] == ns]:
            del self._data[ident]


class RedisMemory:
    """Redis-backed store: JSON values, EX ttl, keyword search by SCAN over the namespace."""

    def __init__(
        self,
        url: str | None = None,
        prefix: str = "kit:mem:",
        *,
        client: Any = None,
        clock: Callable[[], float] = time.time,
    ):
        if client is None:
            import redis

            client = redis.Redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        self._r = client
        self._prefix = prefix
        self._clock = clock

    def ping(self) -> bool:
        return bool(self._r.ping())

    def _k(self, ns: str, key: str) -> str:
        # The raw key lives in the record; the Redis key carries only its hash, so a key can
        # never contain ':' or glob characters that reach into another namespace.
        if not _NS_NAME.match(ns):
            raise ValueError(f"invalid memory namespace name {ns!r}")
        return f"{self._prefix}{ns}:{hashlib.sha256(key.encode()).hexdigest()}"

    @staticmethod
    def _load(raw: Any, ns: str) -> MemoryRecord | None:
        if raw is None:
            return None
        rec = MemoryRecord.model_validate(json.loads(raw))
        return rec if rec.tags.ns == ns else None

    def put(
        self, ns: str, key: str, value: dict, *, ttl_s: int, redacted: bool = False, source: str = "agent"
    ) -> MemoryRecord:
        rec = _make(ns, key, value, self._clock(), ttl_s, redacted, source)
        self._r.set(self._k(ns, key), rec.model_dump_json(), ex=ttl_s)
        return rec

    def get(self, ns: str, key: str) -> MemoryRecord | None:
        return self._load(self._r.get(self._k(ns, key)), ns)

    def _scan(self, ns: str) -> list[Any]:
        if not _NS_NAME.match(ns):
            raise ValueError(f"invalid memory namespace name {ns!r}")
        return list(self._r.scan_iter(match=f"{self._prefix}{ns}:*"))

    def search(self, ns: str, text: str, k: int) -> list[MemoryRecord]:
        records = [r for name in self._scan(ns) if (r := self._load(self._r.get(name), ns))]
        return _rank(text, records, k)

    def delete(self, ns: str, key: str) -> None:
        self._r.delete(self._k(ns, key))

    def purge(self, ns: str) -> None:
        for name in self._scan(ns):
            if self._load(self._r.get(name), ns) is not None:
                self._r.delete(name)


def memory_from_env() -> MemoryStore:
    """RedisMemory if REDIS_URL is set and answers a ping, else InMemoryMemory. Records which."""
    store: MemoryStore | None = None
    if os.getenv("REDIS_URL"):
        try:
            import redis

            candidate = RedisMemory(
                client=redis.Redis.from_url(
                    os.environ["REDIS_URL"], socket_connect_timeout=1, socket_timeout=1
                )
            )
            if candidate.ping():
                store = candidate
        except Exception:  # unreachable or misconfigured: fall back, never crash at startup
            store = None
    store = store or InMemoryMemory()
    tracing.record("memory_backend", backend="redis" if isinstance(store, RedisMemory) else "memory")
    return store


# --- policy ------------------------------------------------------------------------------------


def _key_sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _redact_value(value: Any, kinds: set[str]) -> Any:
    """Pattern-redact every string, number and dict key in a JSON-able structure, collecting kinds.

    Only JSON primitives, lists and dicts are accepted (anything else is a ValueError). A number
    is checked as its str(); it is replaced by the redacted string only if a pattern matched. Two
    dict keys that redact to the same text would silently merge, so that raises PHIRefused.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        text, found = guardrails.redact_patterns(value if isinstance(value, str) else str(value))
        kinds.update(found)
        return text if isinstance(value, str) or found else value
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"memory values need string dict keys, got {type(k).__name__}")
            nk = _redact_value(k, kinds)
            if nk in out:
                raise PHIRefused(sorted(kinds))
            out[nk] = _redact_value(v, kinds)
        return out
    if isinstance(value, list):
        return [_redact_value(v, kinds) for v in value]
    raise ValueError(f"memory values must be JSON primitives, lists or dicts, got {type(value).__name__}")


class Memory:
    """The only way agents touch a MemoryStore: declared namespaces, required ttl, PHI policy."""

    def __init__(self, store: MemoryStore, namespaces: list[Namespace]):
        self.store = store
        self._ns = {n.name: n for n in namespaces}

    def _namespace(self, ns: str) -> Namespace:
        if ns not in self._ns:
            raise ValueError(f"unknown memory namespace '{ns}'. Declared: {', '.join(sorted(self._ns))}")
        return self._ns[ns]

    @staticmethod
    def _trace(
        op: str, ns: str, key: str | None, *, redacted: bool = False, kinds: list[str] | None = None
    ) -> None:
        tracing.record(
            "memory",
            op=op,
            ns=ns,
            key_sha=_key_sha(key) if key is not None else None,
            redacted=redacted,
            found_kinds=kinds or [],
        )

    def put(self, ns: str, key: str, value: dict, *, ttl_s: int, source: str = "agent") -> MemoryRecord:
        spec = self._namespace(ns)
        if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0:
            raise ValueError("memory writes need a positive integer ttl_s")
        if ttl_s > spec.max_ttl_s:
            raise ValueError(f"ttl_s={ttl_s} exceeds max_ttl_s={spec.max_ttl_s} for namespace '{ns}'")
        key_kinds: set[str] = set()
        _redact_value(key, key_kinds)
        kinds: set[str] = set()
        try:
            clean = _redact_value(value, kinds)
        except PHIRefused as e:  # colliding redacted keys
            self._trace("put_refused", ns, key, kinds=e.kinds)
            raise
        found = sorted(kinds | key_kinds)
        if key_kinds or (kinds and spec.phi_policy == "refuse"):
            self._trace("put_refused", ns, key, kinds=found)
            raise PHIRefused(found)
        rec = self.store.put(ns, key, clean, ttl_s=ttl_s, redacted=bool(kinds), source=source)
        self._trace("put", ns, key, redacted=bool(kinds), kinds=found)
        return rec

    def get(self, ns: str, key: str) -> MemoryRecord | None:
        self._namespace(ns)
        rec = self.store.get(ns, key)
        self._trace("get", ns, key, redacted=bool(rec and rec.tags.redacted))
        return rec

    def search(self, ns: str, text: str, k: int = 5) -> list[MemoryRecord]:
        self._namespace(ns)
        kinds: set[str] = set()
        query = _redact_value(text, kinds)  # identifiers are never matched against; kinds are traced
        found = self.store.search(ns, query, k)
        self._trace("search", ns, None, redacted=bool(kinds), kinds=sorted(kinds))
        return found

    def delete(self, ns: str, key: str) -> None:
        self._namespace(ns)
        self.store.delete(ns, key)
        self._trace("delete", ns, key)

    def purge(self, ns: str) -> None:
        self._namespace(ns)
        self.store.purge(ns)
        self._trace("purge", ns, None)


# --- coded memory about a person: closed codes under a keyed pseudonym -----------------------

_PSEUDONYM_KEY: bytes | None = None
_PSEUDONYM_PINNED = False
_PSEUDONYM_LOCK = threading.Lock()


def _pseudonym_key() -> bytes:
    """KIT_MEMORY_KEY if set (read on first use, after kit.config loaded the env), else a random
    key held only by this process: a restart orphans every record, which is erasure by rotation."""
    global _PSEUDONYM_KEY, _PSEUDONYM_PINNED
    with _PSEUDONYM_LOCK:
        if _PSEUDONYM_KEY is None:
            pinned = os.environ.get("KIT_MEMORY_KEY", "").encode()
            _PSEUDONYM_KEY, _PSEUDONYM_PINNED = pinned or os.urandom(32), bool(pinned)
        return _PSEUDONYM_KEY


def pseudonym_key_pinned() -> bool:
    """True when KIT_MEMORY_KEY pins the key. More than one process sharing a Redis must pin the
    same key, or each computes different pseudonyms and none sees the others' records. Reports
    the key actually in use (fixed on first use), not the environment now."""
    _pseudonym_key()
    return _PSEUDONYM_PINNED


def pseudonym(subject_id: str) -> str:
    """A keyed pseudonym for a person's id (HMAC-SHA256): stable within a key, not reversible
    without it. Letters only, so no identifier pattern (phone, date, ZIP, MRN) can match it.
    One key per process: the same person gets the same pseudonym in every namespace, so records
    in two namespaces can be linked to each other (never to the id) by whoever reads the store."""
    digest = hmac.new(_pseudonym_key(), subject_id.encode(), hashlib.sha256).digest()
    return "p" + "".join(chr(97 + b % 26) for b in digest[:20])


R = TypeVar("R", bound=BaseModel)


class CodedLog(Generic[R]):
    """Per-person log of closed-vocabulary records: the safe shape for agent memory about people.

    - `record` is a pydantic model whose fields are Literals / enums (set extra="forbid"): a value
      outside the vocabulary, free text included, fails validation before any store sees it.
    - The key is `pseudonym(subject_id)`, never the id. The value is {field: [records...]}.
    - Every append resets the namespace's TTL (`max_ttl_s`): a sliding window, then it's gone.
    - The last `max_kept` records are kept. append() is read-modify-write under a lock, so
      concurrent runs in one process never lose a record (across processes they can: a demo limit).
    The Memory handle is private: append() is the only write.
    """

    def __init__(
        self,
        namespace: Namespace,
        record: type[R],
        *,
        store: MemoryStore | None = None,
        max_kept: int = 10,
        field: str = "entries",
        source: str = "code",
    ) -> None:
        if max_kept < 1:
            raise ValueError("max_kept must be at least 1")
        self.namespace, self.record, self.max_kept, self.field = namespace, record, max_kept, field
        self._source = source
        self._memory = Memory(store if store is not None else InMemoryMemory(), [namespace])
        self._lock = threading.Lock()

    def entries(self, subject_id: str) -> list[R]:
        rec = self._memory.get(self.namespace.name, pseudonym(subject_id))
        return [self.record.model_validate(e) for e in (rec.value.get(self.field, []) if rec else [])]

    def append(self, subject_id: str, entry: R | dict) -> int:
        """Validate against the closed vocabulary, append, reset the TTL. Returns the count kept."""
        item = entry if isinstance(entry, self.record) else self.record.model_validate(entry)
        with self._lock:
            items = [*self.entries(subject_id), item]
            value = {self.field: [e.model_dump() for e in items[-self.max_kept :]]}
            self._memory.put(
                self.namespace.name,
                pseudonym(subject_id),
                value,
                ttl_s=self.namespace.max_ttl_s,
                source=self._source,
            )
        return len(items)

    def stored(self, subject_id: str) -> MemoryRecord | None:
        """The raw record exactly as the store holds it, for audits and tests. Read-only."""
        return self._memory.store.get(self.namespace.name, pseudonym(subject_id))


# --- tools -------------------------------------------------------------------------------------


class RememberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    text: str


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


def register_memory_tools(registry: ToolRegistry, memory: Memory, *, ns: str, ttl_s: int) -> None:
    """Add `remember` and `recall` to `registry`. Namespace and ttl are bound here, not chosen
    by the model."""

    @registry.tool
    def remember(args: RememberInput) -> dict:
        """Save a short note to long-term memory so it can be recalled in later sessions.
        `key` is a short topic label (e.g. "prefers-morning-slots"); `text` is the note.
        NEVER store patient identifiers: no names, MRNs, phone numbers, emails, dates of birth
        or addresses. If this returns an error saying the write was refused, rephrase the note
        without those identifiers and call it again. Notes expire automatically."""
        rec = memory.put(ns, args.key, {"text": args.text}, ttl_s=ttl_s, source="tool:remember")
        return {"stored": True, "key": rec.key, "redacted": rec.tags.redacted, "expires_in_s": ttl_s}

    @registry.tool
    def recall(args: RecallInput) -> dict:
        """Search long-term memory for notes relevant to `query` (plain keywords). Returns up to
        5 notes as {key, text, redacted}, best match first; an empty list means nothing relevant
        is stored. Do not put patient identifiers in the query; they are ignored."""
        hits = memory.search(ns, args.query, 5)
        return {
            "results": [
                {"key": r.key, "text": r.value.get("text", ""), "redacted": r.tags.redacted} for r in hits
            ]
        }
