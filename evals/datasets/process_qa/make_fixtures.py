"""Build the Q&A eval fixtures: a submission and the review report it produced, offline.

Each fixture is {"submission", "report"} for one process_review case, so a my_process / report
question has a bound run to read. The review runs against a scripted model (no flags from the
model; the rule flags come from the documents) and a denied sign-off, with the index pinned to
its fixed date, so re-running gives identical files (the per-run report_digest is dropped).

    env -u VIRTUAL_ENV uv run python -m evals.datasets.process_qa.make_fixtures
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from flows.process_review.docs import DocIndex
from flows.process_review.graph import run_review
from flows.process_review.state import LaneReport, QueryPlan, SynthesisDraft
from kit import llm
from kit.patterns import approval

HERE = Path(__file__).parent
REVIEW_DATASET = HERE.parent / "process_review.jsonl"
CASES = ("clean-01", "hazard-01", "stale-01")


def _model(role, system, messages, tools, schema):
    if schema is QueryPlan:
        return {"safety": [], "maintenance": [], "qc": []}
    if schema is LaneReport:
        lane = next(n for n in ("safety", "maintenance", "qc") if f"You are the {n} reviewer" in system)
        return {"summary": f"{lane} review: see flags.", "flags": []}
    if schema is SynthesisDraft:
        return {
            "summary": "See the flags below.",
            "cross_flags": [],
            "recommendation": "revise",
            "rationale": "Set from the flags.",
        }
    raise AssertionError(f"unexpected schema {schema}")


def build(out_dir: Path) -> list[Path]:
    submissions = {}
    for line in REVIEW_DATASET.read_text().splitlines():
        if line.strip():
            case = json.loads(line)
            submissions[case["id"]] = case["input"]["submission"]
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for case_id in CASES:
        submission = submissions[case_id]
        with llm.fake(_model):
            report = run_review(
                json.loads(json.dumps(submission)),
                decide=approval.deny_all("supervisor:demo"),
                index=DocIndex(),
            )
        report.pop("report_digest", None)  # varies per run (the review hashes in a per-run salt)
        path = out_dir / f"{case_id}.json"
        path.write_text(
            json.dumps({"submission": submission, "report": report}, indent=2, sort_keys=True) + "\n"
        )
        written.append(path)
    return written


if __name__ == "__main__":
    for p in build(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "fixtures"):
        print(p)
