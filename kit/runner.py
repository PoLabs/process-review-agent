"""Run a flow in the background and let a person answer its approval gate: what the UI and API call.

    run_id = runner.start("process_review", case_id="<case id>")
    for event in runner.events(run_id): ...          # sanitized trace events, as written to disk
    runner.pending(run_id)                           # approval calls waiting for a decision
    runner.decide(run_id, {"<tool_use_id>": {"decision": "approved", "approver": "jane@example.com"}})
    runner.status(run_id)                            # running | waiting | done | failed (+ output)

Flow convention: flows/<flow>/evals.py may define `run(input: dict, decide: Decider) -> dict`.
If it does, approvals go to a person through `decide()` here. If not, the runner falls back to
`flow(case)`, which uses the flow's own scripted decisions (shown as such in status).

A finished run is graded by the flow's own deterministic eval checks (`CHECKS` in its evals.py),
the same code `make eval` runs, so the UI's verdict can't drift from the eval's. Judge checks call
the model, so they are listed as not run here.

Nothing in app/ imports a flow directly; it goes through this module. Traces are content="none"
by default: the UI shows structure, and outcomes only for synthetic eval cases.
"""

from __future__ import annotations

import importlib
import json
import queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evals.contracts import Case
from evals.harness import load_cases, run_checks
from kit import tracing

Status = Literal["running", "waiting", "done", "failed"]
DECISION_TIMEOUT_S = 15 * 60  # an unanswered gate denies — fail closed


@dataclass
class _Run:
    id: str
    flow: str
    case_id: str | None
    status: Status = "running"
    interactive: bool = False
    run_dir: Path | None = None
    pending: list[dict] = field(default_factory=list)
    output: dict | None = None
    expect: dict | None = None
    input: dict | None = None  # set when started from an input (start_input), not from an eval case
    checks: list[dict] | None = None  # the flow's eval checks on this run, once it is done
    error: str | None = None
    _answers: queue.Queue = field(default_factory=queue.Queue)
    _started: threading.Event = field(default_factory=threading.Event)


_RUNS: dict[str, _Run] = {}
_LOCK = threading.Lock()


def flows() -> list[str]:
    root = Path(__file__).resolve().parents[1] / "flows"
    return sorted(p.name for p in root.iterdir() if (p / "evals.py").exists())


def cases(flow: str) -> list[dict]:
    evals = importlib.import_module(f"flows.{flow}.evals")
    return [{"id": c.id, "category": c.category, "expect": c.expect} for c in load_cases(evals.DATASET)]


def _decider(r: _Run):
    def decide(calls: list[dict]) -> dict[str, dict]:
        r.pending, r.status = calls, "waiting"
        try:
            answers = r._answers.get(timeout=DECISION_TIMEOUT_S)
        except queue.Empty:
            answers = {}  # nobody answered: every call is denied by request_approvals
        r.pending, r.status = [], "running"
        return answers

    return decide


def _work(r: _Run, content: tracing.ContentMode) -> None:
    try:
        evals = importlib.import_module(f"flows.{r.flow}.evals")
        if r.input is not None:
            case = Case(id="app", category="app", input=r.input, expect={})
        else:
            all_cases = load_cases(evals.DATASET)
            case = next((c for c in all_cases if c.id == r.case_id), None) if r.case_id else all_cases[0]
            if case is None:
                raise ValueError(f"no case {r.case_id!r} in {r.flow}")
        r.case_id, r.expect = case.id, case.expect
        with tracing.run(f"{r.flow}-ui", content=content) as run_dir, tracing.capture():
            r.run_dir = run_dir
            r._started.set()
            if hasattr(evals, "run"):
                r.interactive = True
                r.output = evals.run(case.input, _decider(r))
            else:
                r.output = evals.flow(case)
            r.checks = grade(evals, case, r.output)  # inside capture: checks can read the trace
        r.status = "done"
    except Exception as e:  # surfaced in status; the type only, messages can carry input text
        r.error, r.status = type(e).__name__, "failed"
    finally:
        r._started.set()


def grade(evals: Any, case: Any, output: dict) -> list[dict]:
    """Run the flow's deterministic checks on one output; judge checks are listed, not run."""
    checks = getattr(evals, "CHECKS", [])
    graded = {r.check: r for r in run_checks([c for c in checks if c.kind != "judge"], case, output)}
    out = []
    for c in checks:
        if c.kind == "judge":
            out.append({"check": c.name, "kind": "judge", "passed": None, "applicable": True,
                        "detail": "model-graded: runs in make eval, not here"})  # fmt: skip
        else:
            out.append({"kind": "deterministic", **graded[c.name].model_dump()})
    return out


def _launch(r: _Run, content: tracing.ContentMode) -> str:
    with _LOCK:
        _RUNS[r.id] = r
    threading.Thread(target=_work, args=(r, content), daemon=True, name=f"run-{r.id}").start()
    r._started.wait(timeout=30)
    return r.id


def start(flow: str, case_id: str | None = None, *, content: tracing.ContentMode = "none") -> str:
    if flow not in flows():
        raise ValueError(f"unknown flow {flow!r}")
    return _launch(_Run(id=uuid.uuid4().hex[:12], flow=flow, case_id=case_id), content)


def start_input(flow: str, input: dict, *, content: tracing.ContentMode = "none") -> str:
    """Start a run on an input the caller supplies (the product app), not on an eval case.
    Needs the flow's evals.run (the interactive entry point), so sign-offs go to a person."""
    if flow not in flows():
        raise ValueError(f"unknown flow {flow!r}")
    if not hasattr(importlib.import_module(f"flows.{flow}.evals"), "run"):
        raise ValueError(f"flow {flow!r} has no interactive run()")
    return _launch(_Run(id=uuid.uuid4().hex[:12], flow=flow, case_id=None, input=input), content)


def run_input(run_id: str) -> dict | None:
    """The input a run was started with (start_input), server side only; None for eval-case runs."""
    return _get(run_id).input


def _get(run_id: str) -> _Run:
    try:
        return _RUNS[run_id]
    except KeyError:
        raise KeyError(f"unknown run {run_id!r}") from None


def status(run_id: str) -> dict[str, Any]:
    r = _get(run_id)
    return {
        "id": r.id,
        "flow": r.flow,
        "case": r.case_id,
        "status": r.status,
        "interactive": r.interactive,
        "pending": r.pending,
        "output": r.output,
        "expect": r.expect,
        "checks": r.checks,
        "error": r.error,
        "trace": str(r.run_dir) if r.run_dir else None,
    }


def pending(run_id: str) -> list[dict]:
    return list(_get(run_id).pending)


def decide(run_id: str, decisions: dict[str, dict]) -> None:
    """Answer the waiting gate. Keys are tool_use_ids; anything missing is denied."""
    r = _get(run_id)
    if r.status != "waiting":
        raise RuntimeError(f"run {run_id} is {r.status}, not waiting for a decision")
    r._answers.put(decisions)


def events(run_id: str, since: int = 0) -> list[dict]:
    """Trace events from index `since`, read back from the sanitized file (never from memory)."""
    r = _get(run_id)
    if r.run_dir is None or not (r.run_dir / "trace.jsonl").exists():
        return []
    lines = (r.run_dir / "trace.jsonl").read_text().splitlines()[since:]
    return [json.loads(line) for line in lines if line.strip()]


def runs() -> list[dict]:
    return [status(i) for i in list(_RUNS)]
