# ruff: noqa: E501  (the evidence strings are table cells, not code)
"""Show, don't claim: run one hazardous process submission through the regulated path and prove each control.

    python -m flows.process_review.compliance [--out reports/process_review_compliance.md]
    (offline: scripted model, temp dirs)

A synthetic submission (acetone + 35 % hydrogen peroxide in M-7) carrying identifiers (a submitter id and
a name, phone and email in the notes) goes through the real process_review graph in a regulated run, with
both sign-offs given by named roles. Then every control is *tested* against what actually landed on disk
and in the archive, including attempts to break it (UPDATE, DELETE, a tampered line), and the result is a
control → evidence table. Any failing control exits 1.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from flows.process_review.docs import DocIndex
from flows.process_review.graph import FLOW_VERSION, RETENTION_DAYS, run_review
from flows.process_review.state import LaneReport, QueryPlan, SynthesisDraft
from flows.process_review.tools import Archive
from kit import audit, audit_store, boundary, llm, tracing
from kit.boundary import Ledger

SUBMITTER = "EMP-042"
IDENTIFIERS = (SUBMITTER, "Jane Roe", "555-0142", "jane.roe@example.com")
NOTES = "Questions to Jane Roe, 555-0142, jane.roe@example.com"
SUBMISSION = {
    "submission_id": "SUB-9042",
    "product": "CL-5",
    "submitter": SUBMITTER,
    "chemicals": [
        {"id": "acetone", "quantity": 50, "unit": "L"},
        {"id": "hydrogen_peroxide", "quantity": 10, "unit": "L", "concentration_pct": 35},
    ],
    "steps": [
        {
            "n": 1,
            "action": "mix",
            "equipment": "M-7",
            "chemicals": ["acetone", "hydrogen_peroxide"],
            "temp_c": 25,
            "pressure_bar": 0,
        }  # fmt: skip
    ],
    "notes": NOTES,
}
APPROVERS = {"supervisor": "supervisor:shift-b-demo", "ehs": "ehs:process-safety-demo"}
CONTENT_COLUMNS = {"content", "text", "message", "input", "output", "messages", "body", "notes"}


def _scripted_model(role, system, messages, tools, schema):
    """The model sees nothing wrong; the hazard flags come from the locked rules, not from it."""
    if schema is QueryPlan:
        return {"safety": [], "maintenance": [], "qc": []}
    if schema is LaneReport:
        return {"summary": "Nothing further found.", "flags": []}
    if schema is SynthesisDraft:
        return {"summary": "The rule checks raised issues; see the flags.", "cross_flags": [],
                "recommendation": "revise", "rationale": "A critical flag is open."}  # fmt: skip
    raise AssertionError(f"unexpected schema {schema}")


def _decide(calls: list[dict]) -> dict[str, dict]:
    """Each sign-off is answered by the person in that role."""
    return {
        c["tool_use_id"]: {"decision": "approved", "approver": APPROVERS[c["input"]["role"]]} for c in calls
    }


@contextmanager
def _sandbox():
    """Temp runs/, ledger and audit DB: the demo touches nothing real."""
    root = Path(tempfile.mkdtemp(prefix="kit-compliance-"))
    saved = {k: os.environ.get(k) for k in ("KIT_AUDIT_DB", "KIT_REGULATED")}
    prev_runs = tracing.RUNS_DIR
    os.environ["KIT_AUDIT_DB"] = str(root / "audit.db")
    os.environ.pop("KIT_REGULATED", None)
    tracing.RUNS_DIR = root / "runs"
    try:
        yield root
    finally:
        tracing.RUNS_DIR = prev_runs
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(root, ignore_errors=True)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def run_demo() -> list[dict]:
    results: list[dict] = []

    def check(control: str, ok, evidence: str) -> None:
        results.append({"control": control, "passed": bool(ok), "evidence": evidence})

    index = DocIndex()
    archive = Archive()
    with _sandbox() as root:
        ledger = Ledger(root / "ledger.jsonl")
        who = tracing.actor("process_review", version=FLOW_VERSION, session="demo-1")
        with llm.fake(_scripted_model):
            with tracing.run("process_review", regulated=True, actor=who) as run_dir:
                out = run_review(
                    copy.deepcopy(SUBMISSION), decide=_decide, index=index, ledger=ledger, archive=archive
                )
        run_id = run_dir.name
        trace = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text().splitlines()]
        trail = audit_store.explain(run_id)
        db = Path(os.environ["KIT_AUDIT_DB"])

        kinds = {e["kind"] for e in trace}
        check(
            "Audit controls",
            len(trail) == len(trace)
            and {"llm", "route", "retrieve", "gate", "decision", "approval"} <= kinds,
            f"{len(trace)} trace events → {len(trail)} audit rows; kinds: {', '.join(sorted(kinds))}",
        )

        # --- no identifier anywhere on disk ----------------------------------------------------
        files = [p for p in root.rglob("*") if p.is_file()]
        leaks = sorted({i for f in files for i in IDENTIFIERS if i.encode() in f.read_bytes()})
        conn = sqlite3.connect(db)
        columns = {r[1] for t in ("runs", "events") for r in conn.execute(f"PRAGMA table_info({t})")}
        conn.close()
        content_cols = sorted(columns & CONTENT_COLUMNS)
        check(
            "No identifiers at rest",
            files and not leaks and not content_cols,
            f"submitter id, name, phone, email searched in {len(files)} files (run dir, audit.db, ledger): "
            f"{leaks or 'none found'}; content columns in the schema: {content_cols or 'none'}",
        )

        # --- integrity: hash chains, and a tampered line ---------------------------------------
        reports = audit.verify([run_dir, ledger.path])
        ok_db, msg = audit_store.verify()
        tampered = root / "tampered"
        tampered.mkdir()
        lines = (run_dir / "trace.jsonl").read_text().splitlines()
        mid = len(lines) // 2
        forged = json.loads(lines[mid])
        forged["kind"] = "forged"
        lines[mid] = json.dumps(forged)
        (tampered / "trace.jsonl").write_text("\n".join(lines) + "\n")
        bad = audit.verify([tampered])
        copy_db = root / "tampered.db"
        shutil.copy(db, copy_db)
        c = sqlite3.connect(copy_db)
        c.execute("DROP TRIGGER events_append_only")
        c.execute(
            "UPDATE events SET outcome = 'forged' WHERE id = (SELECT MAX(id) FROM events WHERE kind='route')"
        )
        c.commit()
        c.close()
        os.environ["KIT_AUDIT_DB"] = str(copy_db)
        db_tampered_ok, _ = audit_store.verify()
        os.environ["KIT_AUDIT_DB"] = str(db)
        check(
            "Integrity",
            reports and all(r.ok for r in reports) and ok_db and bad and not bad[0].ok and not db_tampered_ok,
            f"JSONL chains: {', '.join(f'{r.path.name} {r.lines} lines ok' if r.ok else str(r) for r in reports)}; "
            f"audit.db: {msg}; tampered line → {bad[0].problem if bad else 'not run'}; tampered db row → "
            f"{'detected' if not db_tampered_ok else 'MISSED'}",
        )

        conn = sqlite3.connect(db)
        upd = _raises(lambda: conn.execute("UPDATE events SET outcome='x' WHERE id=1"))
        dele = _raises(lambda: conn.execute("DELETE FROM events WHERE id=1"))
        conn.close()
        check("Append-only", upd and dele, f"UPDATE refused={upd}, DELETE refused={dele}")

        # --- the decision record: what decided, and what it relied on --------------------------
        routes = {r["node"]: r["outcome"] for r in trail if r["kind"] == "route"}
        final = next(
            (r for r in reversed(trail) if r["kind"] == "decision" and r["node"] == "finalize"), None
        )
        evidence = (final or {}).get("evidence", [])
        by_kind: dict[str, list[dict]] = {}
        for e in evidence:
            by_kind.setdefault(e["kind"], []).append(e)
        cited = {c["chunk_id"] for f in out["flags"] for c in f["citations"]}
        roles_ref = {e["id"].split(":")[0] for e in by_kind.get("approval", [])}
        ids_only = all(set(e) <= {"kind", "id", "sha256"} for e in evidence)
        check(
            "Decision record",
            {"after_intake", "after_guard", "after_supervisor"} <= set(routes)
            and final
            and final["outcome"] == "approved" == out["status"]
            and final["rule"] == f"process_review:v{FLOW_VERSION}"
            and {"input", "model_output", "chunk", "rule", "approval"} <= set(by_kind)
            and cited
            and cited <= {e["id"] for e in by_kind["chunk"]}
            and roles_ref == {"supervisor", "ehs"}
            and ids_only,
            f"routes {', '.join(f'{k}→{v}' for k, v in routes.items())}; finalize → "
            f"{final['outcome'] if final else 'none'} by {final['rule'] if final else '-'}; evidence refs: "
            + ", ".join(f"{k} ×{len(v)}" for k, v in sorted(by_kind.items()))
            + f"; {len(cited)} cited chunks all referenced; ids + sha256 only={ids_only}",
        )

        # --- the 6-year record can be rebuilt from versioned documents -------------------------
        refs = []
        for cid in sorted(e["id"] for e in by_kind.get("chunk", [])):
            doc_id, _, rest = cid.partition("@")
            version = rest.rpartition("#")[0]
            chunk = index.chunk(cid)
            sha = next((e.get("sha256") for e in by_kind["chunk"] if e["id"] == cid), None)
            refs.append((cid, index.is_current(doc_id, version), chunk is not None
                         and sha == hashlib.sha256(chunk.text.encode()).hexdigest()))  # fmt: skip
        check(
            "Evidence resolves to documents",
            refs and all(cur and same for _, cur, same in refs),
            f"{len(refs)} chunk refs, each doc@version current in DocIndex and its sha256 equal to the stored "
            f"chunk's: {', '.join(c for c, _, _ in refs)}",
        )

        # --- human approval: the ledger, bound to the exact call ------------------------------
        approvals = ledger.read()
        by_role = {a["tool_use_id"].removeprefix("signoff-"): a for a in approvals}
        ran = archive.signoffs
        bound = bool(ran) and all(
            any(
                a["tool"] == "sign_off_process" and a["decision"] == "approved"
                and a["tool_use_id"] == f"signoff-{s['role']}"
                and a["input_digest"] == boundary.digest(s)
                for a in approvals
            )
            for s in ran
        )  # fmt: skip
        check(
            "Human approval",
            {r: by_role.get(r, {}).get("approver") for r in APPROVERS} == APPROVERS
            and all(a["decision"] == "approved" for a in approvals)
            and len(ran) == len(approvals) == 2
            and bound
            and out["executed_actions"] == ["sign_off_process"] * 2,
            "ledger: "
            + ", ".join(f"{r} signed by {a['approver']}" for r, a in by_role.items())
            + f"; sign_off_process ran {len(ran)}× and each run matches an approval of that exact call "
            f"(tool, id, input digest): {bound}",
        )

        # --- archive: what the approvers saw can be found again --------------------------------
        digest = out["report_digest"]
        archived = archive.get(digest) if digest in archive else {}
        report_ref = next((e for e in by_kind.get("model_output", [])), None)
        check(
            "Archive",
            digest in archive
            and "ehs" in archived.get("required_approvers", [])
            and report_ref
            and report_ref.get("sha256") == digest
            and any(s["report_digest"] == digest for s in ran),
            f"report digest {digest[:12]}… is in the archive (matches the decision's model_output ref and both "
            f"sign-offs); archived required_approvers = {archived.get('required_approvers')}",
        )

        retention = [e for e in trace if e["kind"] == "retention"]
        purged_now = audit_store.purge_expired()
        audit_store.open_run("expired-demo", "expired", None, retain_days=-1)
        purged_old = audit_store.purge_expired()
        check(
            "Retention",
            len(retention) == 1
            and retention[0]["days"] == RETENTION_DAYS
            and purged_now == 0
            and purged_old == 1,
            f"retention event: {[e['days'] for e in retention]} days (policy {RETENTION_DAYS}); audit rows under "
            f"retention: {purged_now} purged; past retain_until: {purged_old} purged",
        )

        llm_rows = [r for r in trail if r["kind"] == "llm"]
        check(
            "Model / prompt versioning",
            llm_rows and all(r["model"] and r["prompt_sha"] for r in llm_rows),
            f"{len(llm_rows)} model calls, each with model + prompt sha "
            f"({len({r['prompt_sha'] for r in llm_rows})} distinct prompts)",
        )
    return results


def render(results: list[dict]) -> str:
    lines = ["# Regulated-path compliance demo: process_review", "",
             "One synthetic hazardous submission (acetone + hydrogen peroxide) with a submitter id and a name,",
             "phone and email in its notes, through the real graph in a regulated run, signed off by a",
             "supervisor and an EHS engineer. Each control below was *tested* against what landed on disk and",
             "in the archive. The decision log records what decided (route, rule, model, prompt version,",
             "approver, evidence references), never what anyone wrote.", "",
             "| control | result | evidence |", "|---|---|---|"]  # fmt: skip
    for r in results:
        verdict = "PASS" if r["passed"] else "**FAIL**"
        lines.append(f"| {r['control']} | {verdict} | {r['evidence']} |")
    lines += ["", "**Not provided by the kit** (each needs a named owner in the deployment):", ""]
    lines += [f"- {g}" for g in audit_store.GAPS[:2]]
    lines += ["- The archive here is in-process; a deployment needs a WORM store with 6-year retention."]
    lines += ["", "_A demonstration of controls, not a certification. Synthetic data only._"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flows.process_review.compliance")
    parser.add_argument("--out", type=Path, default=Path("reports/process_review_compliance.md"))
    args = parser.parse_args(argv)
    results = run_demo()
    md = render(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)
    print(md)
    print(f"wrote {args.out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
