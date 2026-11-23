"""Audit evidence for the regulated build path: verify the chains, list decisions, apply retention.

    python -m kit.audit verify [PATH ...]      every *.jsonl under runs/ (or the given files/dirs)
    python -m kit.audit decisions RUN_DIR      the decision records of one run, with their evidence
    python -m kit.audit purge --days N [--yes] delete run directories older than N days

What this gives a HIPAA / HITRUST / SOC2 reviewer, and what it doesn't:
  - integrity: each JSONL line is hash-chained (kit.tracing); `verify` finds an edited, deleted or
    reordered line. Evidence of tampering, not prevention: ship runs/ to WORM storage for that.
  - attribution: in regulated mode every event names its actor; approvals name their approver.
  - decision evidence: `tracing.decision()` records outcome, rule and hashed evidence refs.
  - minimum necessary: content="none" is forced, so traces hold structure, never PHI.
  - retention: `purge` removes run traces past N days. The approval ledger is never purged here;
    its retention is a records-policy decision (HIPAA documentation: 6 years).
Not provided: authenticated identity (approver is whatever the caller passes), encryption at rest,
access control on runs/. Those belong to the deployment, and the plan should say who owns them.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from kit.config import RUNS_DIR
from kit.tracing import GENESIS, chain_hash


@dataclass
class ChainReport:
    path: Path
    lines: int
    ok: bool
    unchained: int = 0  # lines written before chaining existed
    problem: str = ""

    def __str__(self) -> str:
        state = "ok" if self.ok else f"BROKEN: {self.problem}"
        extra = f", {self.unchained} legacy unchained" if self.unchained else ""
        return f"{state:<8} {self.lines:>6} lines{extra}  {self.path}"


def verify_file(path: Path) -> ChainReport:
    prev, expected_seq, unchained, n = GENESIS, 0, 0, 0
    for n, line in enumerate((ln for ln in path.read_text().splitlines() if ln.strip()), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ChainReport(path, n, False, unchained, f"line {n} is not JSON")
        if "hash" not in event:
            if expected_seq:  # an unchained line after the chain started = inserted
                return ChainReport(path, n, False, unchained, f"line {n} has no hash inside the chain")
            unchained += 1
            continue
        if event.get("seq") != expected_seq:
            return ChainReport(
                path, n, False, unchained, f"line {n}: seq {event.get('seq')} != {expected_seq}"
            )
        if event.get("prev") != prev:
            return ChainReport(path, n, False, unchained, f"line {n}: prev hash does not match line {n - 1}")
        if chain_hash(event) != event["hash"]:
            return ChainReport(path, n, False, unchained, f"line {n}: content does not match its hash")
        prev, expected_seq = event["hash"], expected_seq + 1
    return ChainReport(path, n, True, unchained)


def _jsonl_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    return out


def verify(paths: list[Path] | None = None) -> list[ChainReport]:
    return [verify_file(f) for f in _jsonl_files(paths or [RUNS_DIR])]


def decisions(run_dir: Path) -> list[dict]:
    trace = run_dir / "trace.jsonl"
    events = [json.loads(ln) for ln in trace.read_text().splitlines() if ln.strip()]
    return [e for e in events if e.get("kind") == "decision"]


def purge(days: float, *, apply: bool = False, root: Path | None = None) -> list[Path]:
    """Run directories older than `days`. Deleted only when apply=True. The ledger is never touched."""
    root = root or RUNS_DIR
    cutoff = time.time() - days * 86400
    old = (
        [d for d in sorted(root.iterdir()) if d.is_dir() and d.stat().st_mtime < cutoff]
        if root.exists()
        else []
    )
    if apply:
        for d in old:
            shutil.rmtree(d)
    return old


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kit.audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify hash chains")
    v.add_argument("paths", nargs="*", type=Path)
    d = sub.add_parser("decisions", help="decision records of one run")
    d.add_argument("run_dir", type=Path)
    p = sub.add_parser("purge", help="retention: delete old run traces (never the ledger)")
    p.add_argument("--days", type=float, required=True)
    p.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        reports = verify(args.paths or None)
        for r in reports:
            print(r)
        broken = [r for r in reports if not r.ok]
        print(f"\n{len(reports)} files, {len(broken)} broken")
        return 1 if broken else 0
    if args.cmd == "decisions":
        for e in decisions(args.run_dir):
            refs = ", ".join(f"{r['kind']}:{r['id']}" for r in e.get("evidence", []))
            print(
                f"[{e.get('seq')}] {e.get('node')}: {e.get('outcome')}"
                f"  rule={e.get('rule')}  evidence=[{refs}]"
            )
        return 0
    old = purge(args.days, apply=args.yes)
    verb = "deleted" if args.yes else "would delete"
    print(
        f"{verb} {len(old)} run directories older than {args.days} days"
        + ("" if args.yes else " (--yes to apply)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
