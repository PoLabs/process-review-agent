"""JSONL tracing under a hard rule: no PII on disk.

Every event passes `sanitize()` before it is written. Structural fields (ids, labels,
latencies, token counts) are written as-is; content fields are either redacted or omitted
depending on the run's content mode. Sanitizing **fails closed**: if redaction raises, the
event is dropped and only a content-free marker is written. There is no code path that
writes an unsanitized event.

Every JSONL file the kit writes is **hash-chained**: each line carries `seq`, `prev` (the previous
line's hash) and `hash` = SHA-256 over the sanitized event. Editing, deleting or reordering a line
breaks the chain, and `python -m kit.audit verify` says where (tamper *evidence*, not prevention —
ship the files to a WORM store for that).

Regulated mode (`KIT_REGULATED=1`, or `run(..., regulated=True)`) is the HIPAA / HITRUST / SOC2
build path: content is forced to "none", `record()` outside a run raises instead of silently
dropping the event, structural string fields are screened for identifier patterns, and every
event names its actor (agent, version, session).

Content modes, set per run:
  "redacted" — content fields pass through kit.guardrails.redact (default)
  "none"     — content fields are replaced by their length; nothing textual reaches disk.
               Use for any flow handling patient data: regex redaction is weakest on names.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from kit.config import RUNS_DIR

ContentMode = Literal["redacted", "none"]

# Fields that can carry user/patient content. Everything else is treated as structural.
CONTENT_FIELDS = frozenset(
    {"text", "input", "output", "parsed", "tool_calls", "messages", "detail", "payload"}
)

_run_dir: ContextVar[Path | None] = ContextVar("run_dir", default=None)
_mode: ContextVar[ContentMode] = ContextVar("content_mode", default="redacted")
_redaction_disabled: ContextVar[bool] = ContextVar("redaction_disabled", default=False)
_regulated: ContextVar[bool | None] = ContextVar("regulated", default=None)
_actor: ContextVar[dict | None] = ContextVar("actor", default=None)
_captured: ContextVar[list[dict] | None] = ContextVar("captured", default=None)

# What capture() keeps of each event: labels that say what ran and who allowed it, never content.
CAPTURE_KEYS = ("node", "name", "tool", "tool_use_id", "input_digest", "boundary", "blocked", "is_error",
                "decision", "outcome")  # fmt: skip


class RegulatedViolation(RuntimeError):
    """A regulated-mode control refused an action (unaudited event, identifier in a structural field)."""


def regulated() -> bool:
    flag = _regulated.get()
    return os.getenv("KIT_REGULATED") == "1" if flag is None else flag


def actor(agent: str, *, version: str = "", session: str | None = None, approver: str | None = None) -> dict:
    """Who is acting. Attached to every event of the run; ids only, never names of patients."""
    out = {"agent": agent, "agent_version": version, "session": session or uuid.uuid4().hex[:12]}
    if approver:
        out["approver"] = approver
    return out


def _omit(value: Any) -> dict:
    return {"omitted": True, "chars": len(json.dumps(value, default=str))}


def sanitize(event: dict, mode: ContentMode | None = None) -> dict:
    """Apply the content policy to one event. Raises if redaction fails — callers must not
    catch and write the original. Public so eval reports and ledgers use the same policy."""
    from kit import guardrails  # local import: guardrails must never depend on tracing

    mode = mode or _mode.get()
    screen = regulated() and not _redaction_disabled.get()
    out = {}
    for key, value in event.items():
        if key not in CONTENT_FIELDS or value is None:
            if screen and isinstance(value, str) and guardrails.redact_patterns(value)[1]:
                kinds = guardrails.redact_patterns(value)[1]
                raise RegulatedViolation(f"identifier pattern {kinds} in structural field {key!r}")
            out[key] = value
        elif _redaction_disabled.get():
            out[key] = value  # only reachable inside unsafe_disable_redaction() — the leak-scan canary
        elif mode == "none":
            out[key] = _omit(value)
        else:
            out[key] = guardrails.redact(value)
    return out


@contextmanager
def run(
    name: str,
    content: ContentMode = "redacted",
    *,
    actor: dict | None = None,
    regulated: bool | None = None,
) -> Iterator[Path]:
    """Open a trace directory for one run; everything recorded inside lands in it.

    regulated=None follows KIT_REGULATED. In regulated mode content is forced to "none" and the
    run must name its actor (a default one is minted from the run name if none is given).
    """
    reg = os.getenv("KIT_REGULATED") == "1" if regulated is None else regulated
    if reg:
        content = "none"
        actor = actor or {"agent": name, "agent_version": "", "session": uuid.uuid4().hex[:12]}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{stamp}-{name}-{uuid.uuid4().hex[:6]}"
    path.mkdir(parents=True, exist_ok=True)
    tokens = (_run_dir.set(path), _mode.set(content), _regulated.set(reg), _actor.set(actor))
    try:
        if reg:
            from kit import audit_store  # the regulated decision log (SQLite add-on)

            audit_store.open_run(path.name, name, actor)
            record("run_start", run=path.name, regulated=True, content=content)
        yield path
    finally:
        if reg:
            record("run_end", run=path.name)
        _run_dir.reset(tokens[0])
        _mode.reset(tokens[1])
        _regulated.reset(tokens[2])
        _actor.reset(tokens[3])


def current_run() -> Path | None:
    return _run_dir.get()


@contextmanager
def capture() -> Iterator[list[dict]]:
    """Also keep this run's events in memory, labels only (CAPTURE_KEYS, never content), so an
    eval can grade what the tools actually did instead of what the output says they did. The
    eval harness and kit.runner open one around each run; contexts copied into worker threads
    share the same list."""
    events: list[dict] = []
    token = _captured.set(events)
    try:
        yield events
    finally:
        _captured.reset(token)


def captured() -> list[dict] | None:
    """The events capture() is collecting in this context, or None outside capture()."""
    return _captured.get()


GENESIS = "0" * 64
_CHAIN_FIELDS = ("seq", "prev", "hash")
_chain_lock = threading.Lock()
_chain_tail: dict[Path, tuple[int, str]] = {}


def chain_hash(event: dict) -> str:
    """SHA-256 over the event as written, minus its own hash. `kit.audit` recomputes this."""
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _tail(path: Path) -> tuple[int, str]:
    """(last seq, last hash) of a chained file; recovered from disk the first time it's seen."""
    if path in _chain_tail:
        return _chain_tail[path]
    seq, prev = -1, GENESIS
    if path.exists():
        for line in reversed(path.read_text().splitlines()):
            if line.strip():
                last = json.loads(line)
                if "hash" in last:
                    seq, prev = last["seq"], last["hash"]
                break
    return seq, prev


def write_jsonl(path: Path, event: dict) -> bool:
    """Sanitize, chain and append one event to any JSONL file. Returns False if the event was dropped."""
    try:
        safe, ok = sanitize(event), True
    except Exception as e:  # fail closed: never fall back to the raw event
        safe = {
            "ts": time.time(),
            "kind": "redaction_failed",
            "event_kind": event.get("kind"),
            "error": type(e).__name__,
        }
        ok = False
    for k in _CHAIN_FIELDS:
        safe.pop(k, None)  # a caller can't forge chain fields
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.resolve()
    with _chain_lock:
        seq, prev = _tail(key)
        safe["seq"], safe["prev"] = seq + 1, prev
        safe["hash"] = chain_hash(safe)
        with path.open("a") as f:
            f.write(json.dumps(safe, default=str) + "\n")
            f.flush()
            if regulated():
                os.fsync(f.fileno())
        _chain_tail[key] = (safe["seq"], safe["hash"])
    return ok


def record(kind: str, **fields: Any) -> None:
    """Append one event to the current run. A no-op outside `run()`, so library code can always
    call it — except in regulated mode, where an event with nowhere to go is a control failure."""
    if (sink := _captured.get()) is not None:
        sink.append({"kind": kind, **{k: fields[k] for k in CAPTURE_KEYS if k in fields}})
    path = _run_dir.get()
    if path is None:
        if regulated():
            raise RegulatedViolation(f"{kind!r} recorded outside tracing.run(): it would go unaudited")
        return
    event = {"ts": time.time(), "kind": kind, **fields}
    if (who := _actor.get()) is not None:
        event = {**event, "actor": who}
    write_jsonl(path / "trace.jsonl", event)
    if regulated():
        from kit import audit_store

        try:
            audit_store.capture(path.name, event)
        except Exception as e:  # an event that can't be audited must not happen silently
            raise RegulatedViolation(f"audit store write failed for {kind!r}: {type(e).__name__}") from e


def audited_route(name: str):
    """Decorate a LangGraph routing function so every choice it makes is a recorded decision.

    The chosen branch (or the Send targets of a fan-out) is the outcome. The state it read is not
    recorded: the rule and the outcome say what decided, and the state may hold patient text.
    """

    def wrap(fn):
        def route(state, *args, **kwargs):
            choice = fn(state, *args, **kwargs)
            if isinstance(choice, list):  # fan-out: [Send(node, payload), ...]
                outcome = ",".join(sorted(str(getattr(c, "node", c)) for c in choice))
            else:
                outcome = str(choice)
            record("route", node=name, outcome=outcome, rule=f"route:{fn.__name__}")
            return choice

        route.__name__ = fn.__name__
        route.__doc__ = fn.__doc__
        return route

    return wrap


EvidenceKind = Literal[
    "doc", "chunk", "tool_result", "rule", "case", "approval", "model_output", "input", "retrieval", "memory"
]


def evidence(kind: EvidenceKind, id: str, content: Any = None) -> dict:
    """A reference to what a decision relied on: kind, id and a SHA-256 of the content. The
    content itself is never stored, so a decision record survives content="none"."""
    ref: dict[str, Any] = {"kind": kind, "id": id}
    if content is not None:
        raw = content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)
        ref["sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return ref


def decision(node: str, outcome: str, *, rule: str, evidence: list[dict], basis: str = "") -> None:
    """Record a decision and the evidence behind it: the audit trail a reviewer reconstructs.

    `outcome` and `rule` are labels (e.g. "escalate", "rule:crisis_terms_v2"); `basis` is a short
    structural reason code, not free text about the person. Evidence is a list of `evidence()` refs.
    """
    for ref in evidence:
        if set(ref) - {"kind", "id", "sha256"}:
            raise ValueError(f"evidence refs carry kind/id/sha256 only, got {sorted(ref)}")
    record("decision", node=node, outcome=outcome, rule=rule, basis=basis, evidence=evidence)


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()]


@contextmanager
def unsafe_disable_redaction() -> Iterator[None]:
    """Leak-scan canary ONLY: proves the scanner can see a leak. Never use in a flow."""
    token = _redaction_disabled.set(True)
    try:
        yield
    finally:
        _redaction_disabled.reset(token)
