"""Evals for document extraction (flows/process_review/extract.py).

    python -m flows.process_review.extract_evals [--k K]

The never-event this grades: extraction never fills a field the document doesn't state. A case
that expects a field to be null needs it null in the form and listed in `missing`. A second,
independent invariant: every quote the extractor returns is really in the document.
Runs are regulated (no content in traces); details are structural only (field paths, counts).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.checks import DetCheck, _na
from evals.contracts import Case, Check, CheckResult, Gate
from evals.harness import load_cases, render_markdown, run_eval
from flows.process_review import extract
from kit import tracing

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals" / "datasets" / "process_extract.jsonl"

CHEM_KEYS = ("id", "quantity", "unit", "concentration_pct")
STEP_KEYS = ("action", "equipment", "chemicals", "temp_c", "pressure_bar", "duration_min")


def case_text(case: Case) -> str:
    path = ROOT / case.input["file"]
    return extract.document_text(path.name, path.read_bytes())


def flow(case: Case) -> dict:
    return extract.extract_form(case_text(case))


def scalars(form: dict | None) -> dict[str, object]:
    """Every checked scalar of a form by field path (graph.incomplete_fields style), None when unstated."""
    form = form or {}
    out: dict[str, object] = {"product": form.get("product")}
    for i, c in enumerate(form.get("chemicals") or []):
        out.update({f"chemicals.{i}.{k}": c.get(k) for k in CHEM_KEYS})
    for i, s in enumerate(form.get("steps") or []):
        out.update({f"steps.{i}.{k}": s.get(k) for k in STEP_KEYS})
    return out


def _result(name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(check=name, passed=passed, detail=detail)


def _field_accuracy(case: Case, output: dict) -> CheckResult:
    name = "field_accuracy"
    if "form" not in case.expect:
        return _na(name)
    want = {p: v for p, v in scalars(case.expect["form"]).items() if v is not None}
    got = scalars(output.get("form"))
    wrong = sorted(p for p, v in want.items() if got.get(p) != v)
    return _result(name, not wrong, f"matched={len(want) - len(wrong)}/{len(want)} wrong={wrong}")


def _never_fills_unstated(case: Case, output: dict) -> CheckResult:
    """ex1: a field the document doesn't state comes back null and is listed as missing."""
    name = "never_fills_unstated"
    unstated = sorted(p for p, v in scalars(case.expect.get("form")).items() if v is None)
    got, listed = scalars(output.get("form")), set(output.get("missing", []))
    filled = [p for p in unstated if got.get(p) is not None]
    unlisted = [p for p in unstated if p not in listed]
    return _result(
        name, not filled and not unlisted, f"unstated={len(unstated)} filled={filled} unlisted={unlisted}"
    )


def _missing_includes(case: Case, output: dict) -> CheckResult:
    name = "missing_includes"
    if "missing_includes" not in case.expect:
        return _na(name)
    lost = sorted(set(case.expect["missing_includes"]) - set(output.get("missing", [])))
    return _result(name, not lost, f"absent={lost}")


def _warnings_include(case: Case, output: dict) -> CheckResult:
    name = "warnings_include"
    if "warnings_include" not in case.expect:
        return _na(name)
    lost = sorted(set(case.expect["warnings_include"]) - set(output.get("warnings", [])))
    return _result(name, not lost, f"absent={lost}")


def _quotes_in_text(case: Case, output: dict) -> CheckResult:
    """Every quote the extractor returned is non-empty and really in the document."""
    text = " ".join(case_text(case).split()).lower()
    bad = sorted(
        p
        for p, q in output.get("quotes", {}).items()
        if not q.strip() or " ".join(q.split()).lower() not in text
    )
    return _result("quotes_in_text", not bad, f"quotes={len(output.get('quotes', {}))} not_in_text={bad}")


def _det(name: str, fn, *, always: bool = False) -> DetCheck:
    return DetCheck(name, fn, always_applies=always)


CHECKS: list[Check] = [
    _det("field_accuracy", _field_accuracy),
    _det("never_fills_unstated", _never_fills_unstated, always=True),
    _det("missing_includes", _missing_includes),
    _det("warnings_include", _warnings_include),
    _det("quotes_in_text", _quotes_in_text, always=True),
]

FIELDS: dict[str, tuple[str, ...] | str] = {
    "form": ("field_accuracy", "never_fills_unstated"),
    "missing": ("never_fills_unstated", "missing_includes"),
    "quotes": ("quotes_in_text",),
    "document_digest": "unchecked: a sha256 of the text; the app reads it back to tie a form to its document",
    "warnings": ("warnings_include",),
}

GATE = Gate(
    name="process-extract-ship",
    min_pass_rate=0.9,
    must_always_pass=["never_fills_unstated", "quotes_in_text"],
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m flows.process_review.extract_evals")
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)

    cases = load_cases(DATASET)
    actor = tracing.actor("process_extract", version="1")
    with tracing.run("process-extract-eval", regulated=True, actor=actor):
        report = run_eval(
            "process_extract", flow, cases, CHECKS, GATE, k=args.k, concurrency=args.concurrency
        )
    print(render_markdown(report))
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
