"""Eval harness: run every case k times, report rates, apply a conjunctive gate.

Principles encoded here:
  * One run of a non-deterministic system is an anecdote. Every case runs k times; we report
    rates and name flaky cases (passed some repeats, failed others).
  * The gate is conjunctive. A must-always-pass check that fails even once fails the gate, and
    one with ZERO applicable runs also fails ("never exercised"): an unrun gate is a failure.
  * No PII on disk. Reports carry case ids and structural details only, never case inputs or
    model output text, and every free-form string passes through `tracing.sanitize`.
"""

from __future__ import annotations

import contextvars
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from evals.contracts import Case, Check, CheckResult, FlowFn, Gate, GateVerdict, Report, RunResult
from kit import guardrails, llm, tracing
from kit.llm import Role


def load_cases(path: str | Path) -> list[Case]:
    lines = Path(path).read_text().splitlines()
    return [Case.model_validate_json(line) for line in lines if line.strip()]


# --- running -----------------------------------------------------------------------------------


def run_checks(checks: list[Check], case: Case, output: dict) -> list[CheckResult]:
    results = []
    for check in checks:
        try:
            results.append(check(case, output))
        except Exception as e:  # a broken check fails itself; it must not take the run down
            results.append(
                CheckResult(check=check.name, passed=False, detail=f"check_error:{type(e).__name__}")
            )
    return results


def _run_once(flow: FlowFn, case: Case, repeat: int, checks: list[Check]) -> RunResult:
    t0 = time.perf_counter()
    error, output = None, {}
    with tracing.capture():  # checks such as effects_match_trace grade what this run really did
        try:
            output = flow(case)
        except Exception as e:  # a crashing flow is a failed run, not a crashed eval
            error = f"{type(e).__name__}: {e}"
        latency = round(time.perf_counter() - t0, 3)
        # On error, checks still run against an empty output so must-always-pass checks that apply
        # to this case count the failure instead of silently skipping it.
        results = run_checks(checks, case, output)
    passed = error is None and all(r.passed for r in results if r.applicable)
    return RunResult(
        case_id=case.id,
        category=case.category,
        repeat=repeat,
        passed=passed,
        checks=results,
        error=error,
        latency_s=latency,
    )


# --- aggregating -------------------------------------------------------------------------------


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


def _flaky(runs: list[RunResult]) -> list[str]:
    outcomes: dict[str, set[bool]] = defaultdict(set)
    for r in runs:
        outcomes[r.case_id].add(r.passed)
    return sorted(case_id for case_id, seen in outcomes.items() if len(seen) == 2)


def _by_category(runs: list[RunResult]) -> dict[str, float]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        grouped[r.category].append(r.passed)
    return {cat: _rate(sum(v), len(v)) for cat, v in sorted(grouped.items())}


def _applicable(runs: list[RunResult]) -> dict[str, list[bool]]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        for c in r.checks:
            if c.applicable:
                grouped[c.check].append(c.passed)
    return grouped


def evaluate_gate(
    gate: Gate, runs: list[RunResult], pass_rate: float, by_category: dict[str, float]
) -> GateVerdict:
    reasons = []
    if pass_rate < gate.min_pass_rate:
        reasons.append(f"pass_rate {pass_rate:.3f} < {gate.min_pass_rate:.3f}")
    applicable = _applicable(runs)
    for name in gate.must_always_pass:
        outcomes = applicable.get(name, [])
        failed = outcomes.count(False)
        if not outcomes:
            reasons.append(f"must_always_pass '{name}' never exercised")
        elif failed:
            reasons.append(f"must_always_pass '{name}' failed {failed}/{len(outcomes)} applicable runs")
    for cat, minimum in gate.category_min.items():
        if cat not in by_category:
            reasons.append(f"category '{cat}' never exercised")
        elif by_category[cat] < minimum:
            reasons.append(f"category '{cat}' {by_category[cat]:.3f} < {minimum:.3f}")
    return GateVerdict(passed=not reasons, reasons=reasons)


def run_eval(
    flow_name: str,
    flow: FlowFn,
    cases: list[Case],
    checks: list[Check],
    gate: Gate,
    *,
    k: int = 3,
    concurrency: int = 4,
    out_dir: Path | None = None,
) -> Report:
    # Known identifiers stay registered until the report is on disk: clearing them before
    # write_report() left exact redaction off for the report itself (caught by `make leakscan`).
    guardrails.register_known_identifiers([p for c in cases for p in c.pii])
    try:
        return _run_and_report(flow_name, flow, cases, checks, gate, k=k, concurrency=concurrency,
                               out_dir=out_dir)  # fmt: skip
    finally:
        guardrails.clear_known_identifiers()


def _run_and_report(
    flow_name: str,
    flow: FlowFn,
    cases: list[Case],
    checks: list[Check],
    gate: Gate,
    *,
    k: int,
    concurrency: int,
    out_dir: Path | None,
) -> Report:
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        # One context copy per task: tracing's run dir and content mode follow into the worker.
        futures = [
            pool.submit(contextvars.copy_context().run, _run_once, flow, case, repeat, checks)
            for case in cases
            for repeat in range(k)
        ]
        runs = [f.result() for f in futures]

    pass_rate = _rate(sum(r.passed for r in runs), len(runs))
    by_category = _by_category(runs)
    report = Report(
        flow=flow_name,
        gate=gate.name,
        n_cases=len(cases),
        k=k,
        pass_rate=pass_rate,
        by_category=by_category,
        by_check={name: _rate(sum(v), len(v)) for name, v in sorted(_applicable(runs).items())},
        flaky_cases=_flaky(runs),
        verdict=evaluate_gate(gate, runs, pass_rate, by_category),
        runs=runs,
        meta={
            "k": k,
            "concurrency": concurrency,
            "started_at": started_at,
            "duration_s": round(time.perf_counter() - started, 2),
            "models": {role.value: llm.route_for(role).model for role in Role},
        },
    )
    destination = out_dir or tracing.current_run()
    if destination is not None:
        write_report(report, Path(destination))
    return report


# --- writing -----------------------------------------------------------------------------------


def _clean(value: str | None) -> str | dict | None:
    """Pass a free-form string through the tracing content policy. Fails closed."""
    if not value:
        return value
    try:
        return tracing.sanitize({"detail": value})["detail"]
    except Exception as e:
        return f"[redaction_failed:{type(e).__name__}]"


def _as_text(value: str | dict | None) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def sanitized(report: Report) -> Report:
    """A copy of the report whose free-form strings obey the content policy (the only form written)."""
    runs = []
    for r in report.runs:
        checks = [c.model_copy(update={"detail": _as_text(_clean(c.detail))}) for c in r.checks]
        # A run without an error stays None. Serializing it would write the string "null", which is
        # truthy, and every written report would list every run as crashed.
        error = _as_text(_clean(r.error)) if r.error else None
        runs.append(r.model_copy(update={"checks": checks, "error": error}))
    return report.model_copy(update={"runs": runs})


def write_report(report: Report, directory: Path) -> None:
    safe = sanitized(report)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(safe.model_dump_json(indent=2))
    (directory / "report.md").write_text(render_markdown(safe))


def render_markdown(report: Report) -> str:
    v = report.verdict
    lines = [f"{'✅ PASS' if v.passed else '❌ FAIL'}  {report.flow}  gate={report.gate}"]
    lines += [f"- {reason}" for reason in v.reasons]
    lines += [
        "",
        f"## Overall: {report.pass_rate:.3f}",
        f"{report.n_cases} cases x k={report.k} = {len(report.runs)} runs. "
        f"Duration {report.meta.get('duration_s', '?')}s.",
        "",
        "## By category",
        "| category | pass rate |",
        "|---|---|",
        *[f"| {cat} | {rate:.3f} |" for cat, rate in report.by_category.items()],
        "",
        "## By check (applicable runs only)",
        "| check | pass rate |",
        "|---|---|",
        *[f"| {name} | {rate:.3f} |" for name, rate in report.by_check.items()],
        "",
        "## Flaky cases (passed some repeats, failed others)",
        *([f"- {case_id}" for case_id in report.flaky_cases] or ["- none"]),
    ]
    failures = [(r, c) for r in report.runs for c in r.checks if c.applicable and not c.passed]
    errors = [r for r in report.runs if r.error]
    if errors:
        # Crashes first, grouped by exception type (a structural label, safe in content="none"),
        # so a broken dependency can't hide behind the failures table's row cap.
        by_type: dict[str, list[str]] = defaultdict(list)
        for r in errors:
            by_type[str(r.error).split(":", 1)[0]].append(r.case_id)
        lines += ["", f"## Flow errors: {len(errors)}/{len(report.runs)} runs crashed"]
        lines += [
            f"- {etype} x{len(ids)}: {', '.join(sorted(set(ids))[:8])}" for etype, ids in by_type.items()
        ]
    if failures or errors:
        lines += ["", "## Failures (first 20)", "| case | repeat | check | detail |", "|---|---|---|---|"]
        rows = [(r.case_id, r.repeat, c.check, c.detail) for r, c in failures]
        rows += [(r.case_id, r.repeat, "(flow error)", r.error) for r in errors]
        lines += [f"| {cid} | {rep} | {chk} | {detail} |" for cid, rep, chk, detail in rows[:20]]
    return "\n".join(lines) + "\n"
