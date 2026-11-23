"""The regulated decision log: a SQLite add-on that records *how* the agent system decided.

It records what decided, never what anyone wrote. Each row is one step of a run: which route
was taken, which rule fired, which model and prompt version answered, who approved, and
references to the evidence (document ids, digests). There is no column that can hold patient
text: the schema has none, and every row is screened for identifier patterns before it is
written. A reviewer reconstructs the decision from the trail; the text itself stays in the source
system it came from (the portal, the EHR), which the evidence references point back to.

Separate from everything else on purpose: Redis is memory, `runs/*/trace.jsonl` is the
operational trace, and this file (`runs/audit.db`, or KIT_AUDIT_DB) is the audit record. It is
written automatically: in a regulated run (`KIT_REGULATED=1` or `tracing.run(..., regulated=True)`)
every `tracing.record()` event (model calls, tool calls, approvals, routing choices, retrieval,
memory, decisions) also lands here as a row.

The controls, and how each is enforced, not promised:

  audit controls      every event of a regulated run gets a row; if the row can't be written the
                      run stops (RegulatedViolation) rather than continuing unaudited
  integrity           rows are hash-chained (prev_hash/hash); `verify()` finds an altered row
  append-only         SQLite triggers refuse UPDATE, and DELETE before retention ends
  retention           a run's rows can be deleted only after its retain_until (default 6 years)
  no patient data     no content columns; a row containing an identifier pattern is replaced by
                      a marker, so it never reaches disk
  versioning          every model row names the model and the prompt's sha

A demonstration, not a certified system: approver identity is self-asserted, and the file has no
OS-level access control or WORM storage. `python -m kit.audit_store controls` prints the mapping.

    python -m kit.audit_store runs
    python -m kit.audit_store explain RUN            the decision trail of one run
    python -m kit.audit_store verify
    python -m kit.audit_store controls
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

GENESIS = "0" * 64
DEFAULT_RETAIN_DAYS = 6 * 365  # HIPAA documentation retention

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, name TEXT, agent TEXT, agent_version TEXT, session TEXT,
    started REAL NOT NULL, retain_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    ts REAL NOT NULL, kind TEXT NOT NULL, node TEXT, outcome TEXT, rule TEXT, model TEXT,
    prompt_sha TEXT, approver TEXT,
    evidence TEXT NOT NULL,            -- JSON list of {kind, id, sha256?}: references, never content
    structural TEXT NOT NULL,          -- the event as in trace.jsonl (content fields omitted)
    prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);

CREATE TRIGGER IF NOT EXISTS events_append_only BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_retention BEFORE DELETE ON events
WHEN (SELECT retain_until FROM runs WHERE run_id = OLD.run_id) > CAST(strftime('%s','now') AS REAL)
BEGIN SELECT RAISE(ABORT, 'audit events are under retention'); END;
CREATE TRIGGER IF NOT EXISTS runs_append_only BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'audit runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_retention BEFORE DELETE ON runs
WHEN OLD.retain_until > CAST(strftime('%s','now') AS REAL)
BEGIN SELECT RAISE(ABORT, 'audit runs are under retention'); END;
"""

_COLUMNS = ("run_id", "ts", "kind", "node", "outcome", "rule", "model", "prompt_sha", "approver", "evidence",
            "structural", "prev_hash")  # fmt: skip
_lock = threading.Lock()


def db_path() -> Path:
    if env := os.getenv("KIT_AUDIT_DB"):
        return Path(env)
    from kit import tracing  # RUNS_DIR is read at call time so tests can redirect it

    return tracing.RUNS_DIR / "audit.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_hash(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def _strings(value) -> list[str]:
    """Every string leaf (dict keys included). Numbers are skipped: a timestamp looks like a phone."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _has_identifier(fields: dict) -> list[str]:
    from kit import guardrails

    found: set[str] = set()
    for key, value in fields.items():
        if key in ("run_id", "ts"):
            continue
        parsed = json.loads(value) if key in ("evidence", "structural") else value
        for s in _strings(parsed):
            found.update(guardrails.redact_patterns(s)[1])
    return sorted(found)


# --- writing -------------------------------------------------------------------------------


def open_run(run_id: str, name: str, actor: dict | None, *, retain_days: float | None = None) -> None:
    days = (
        retain_days if retain_days is not None else float(os.getenv("KIT_RETAIN_DAYS", DEFAULT_RETAIN_DAYS))
    )
    now, a = time.time(), actor or {}
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?)",
            (run_id, name, a.get("agent"), a.get("agent_version"), a.get("session"), now, now + days * 86400),
        )
        conn.close()


def capture(run_id: str, event: dict) -> int:
    """One audit row for one trace event: what decided, with references. Never content."""
    from kit import tracing

    try:
        structural = tracing.sanitize(event, mode="none")  # content fields → {"omitted": True, "chars": n}
    except Exception as e:  # an identifier in a label: the event is replaced, not stored
        structural = {"kind": "redaction_failed", "event_kind": event.get("kind"), "error": type(e).__name__}
        event = {"ts": event.get("ts"), **structural}
    refs = event.get("evidence") if isinstance(event.get("evidence"), list) else []
    actor = event.get("actor") or {}
    fields = {
        "run_id": run_id, "ts": event.get("ts", time.time()), "kind": event.get("kind", ""),
        "node": event.get("node"), "outcome": event.get("outcome"), "rule": event.get("rule"),
        "model": event.get("model"), "prompt_sha": event.get("prompt_sha"),
        "approver": event.get("approver") or actor.get("approver"),
        "evidence": json.dumps(refs, sort_keys=True, default=str),
        "structural": json.dumps(structural, sort_keys=True, default=str),
    }  # fmt: skip
    if kinds := _has_identifier(fields):  # last line of defence: nothing identifying reaches disk
        marker = {"kind": "redaction_failed", "event_kind": fields["kind"], "found": kinds}
        blank = {k: None for k in fields}
        fields = {**blank, "run_id": run_id, "ts": fields["ts"], "kind": "redaction_failed", "evidence": "[]",
                  "structural": json.dumps(marker, sort_keys=True)}  # fmt: skip
    with _lock:
        conn = connect()
        try:
            last = conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            fields["prev_hash"] = last["hash"] if last else GENESIS
            row = {c: fields[c] for c in _COLUMNS}
            row["hash"] = _row_hash(row)
            cur = conn.execute(
                f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            return int(cur.lastrowid)
        finally:
            conn.close()


# --- reading -------------------------------------------------------------------------------


def explain(run_id: str) -> list[dict]:
    """The decision trail of one run, in order."""
    conn = connect()
    rows = conn.execute(
        "SELECT id, ts, kind, node, outcome, rule, model, prompt_sha, approver, evidence "
        "FROM events WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    conn.close()
    return [{**dict(r), "evidence": json.loads(r["evidence"])} for r in rows]


def runs() -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT r.*, COUNT(e.id) AS events FROM runs r LEFT JOIN events e USING(run_id) "
        "GROUP BY r.run_id ORDER BY r.started DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def verify() -> tuple[bool, str]:
    """Recompute the chain over every event row."""
    conn = connect()
    prev, n = GENESIS, 0
    try:
        for row in conn.execute("SELECT * FROM events ORDER BY id"):
            n += 1
            r = dict(row)
            if r["prev_hash"] != prev:
                return False, f"event {r['id']}: prev_hash does not match the event before it"
            if _row_hash({c: r[c] for c in _COLUMNS}) != r["hash"]:
                return False, f"event {r['id']}: row does not match its hash"
            prev = r["hash"]
    finally:
        conn.close()
    return True, f"{n} events, chain intact"


def purge_expired() -> int:
    """Retention: delete runs (and their events) past retain_until. Triggers refuse anything younger."""
    now = time.time()
    with _lock:
        conn = connect()
        try:
            expired = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE retain_until <= ?", (now,))]
            for run_id in expired:
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            return len(expired)
        finally:
            conn.close()


# --- the control map -----------------------------------------------------------------------

CONTROLS = [
    (
        "Audit controls",
        "HIPAA 164.312(b); SOC 2 CC7.2",
        "every regulated event → a row; write failure stops the run",
    ),
    (
        "Decision trail",
        "HIPAA 164.312(b); SOC 2 PI1.4",
        "every route, gate and final decision, with rule and evidence refs",
    ),
    ("Integrity", "HIPAA 164.312(c)(1); SOC 2 CC7.1", "hash-chained rows; verify() finds an altered row"),
    ("Append-only", "SOC 2 CC7.2", "SQLite triggers refuse UPDATE, and DELETE under retention"),
    ("Retention", "HIPAA 164.316(b)(2)", "rows deletable only after retain_until (default 6 years)"),
    (
        "No patient data at rest",
        "HIPAA 164.502(b) minimum necessary",
        "no content columns; identifier screen on every row",
    ),
    (
        "Memory holds no patient data",
        "HIPAA 164.502(b), 164.514(c)",
        "closed codes under a keyed pseudonym, TTL; a write carrying identifiers is refused",
    ),
    ("Model / prompt versioning", "SOC 2 CC8.1", "every model row names the model and the prompt's sha"),
    ("Human approval", "SOC 2 CC5.2", "external actions only with an approval bound to the exact call"),
]
GAPS = [
    "Authentication: approver identity is self-asserted, not verified (deployment: SSO).",
    "OS access control and WORM storage for runs/audit.db (deployment).",
    "In transit: the model vendor still sees the message while answering; that needs a BAA (contract).",
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m kit.audit_store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify")
    sub.add_parser("runs")
    sub.add_parser("controls")
    e = sub.add_parser("explain")
    e.add_argument("run_id")
    args = ap.parse_args(argv)
    if args.cmd == "verify":
        ok, msg = verify()
        print(("ok  " if ok else "BROKEN  ") + msg)
        return 0 if ok else 1
    if args.cmd == "runs":
        for r in runs():
            print(f"{r['run_id']}  {r['agent']}  events={r['events']}")
    elif args.cmd == "controls":
        for name, ref, how in CONTROLS:
            print(f"{name:<26} {ref:<36} {how}")
        print("\nNot provided (named owner required):")
        print("\n".join(f"  - {g}" for g in GAPS))
    else:
        for r in explain(args.run_id):
            bits = [r["kind"], r["node"] or "", r["outcome"] or "", r["rule"] or "", r["model"] or ""]
            refs = ", ".join(f"{x['kind']}:{x['id']}" for x in r["evidence"])
            print(f"{r['id']:>5}  " + "  ".join(b for b in bits if b) + (f"  [{refs}]" if refs else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
