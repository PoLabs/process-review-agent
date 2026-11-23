"""Prove the eval can go red.

A gate that has never failed is unproven. `prove_gate_can_fail` runs the flow, then a mutated
copy with one behaviour deliberately broken, and insists the gate rejects the mutant.

    python -m evals.break      # toy flow: baseline must pass, "never escalate" must fail,
                               # and so must a run that reports a tool call it never made
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from evals.contracts import Case, Check, FlowFn, Gate, Report
from evals.harness import load_cases, render_markdown, run_eval
from kit import tracing

Mutator = Callable[[dict], dict]


def mutate(flow: FlowFn, mutator: Mutator) -> FlowFn:
    """Wrap `flow` so its output passes through `mutator`."""
    return lambda case: mutator(flow(case))


def set_key(key: str, value: object) -> Mutator:
    return lambda output: {**output, key: value}


def drop_key(key: str) -> Mutator:
    return lambda output: {k: v for k, v in output.items() if k != key}


def add_stray(path: str, item: object) -> Mutator:
    """Append `item` to the list at `path` ("handoff.docs" for a nested one), as if the flow had
    added something no case asked for. Run it for every field in the flow's ADDS: if the gate stays
    green, nothing checks what that field carries. Where the parent is missing or None on a case
    (no handoff, say), the output is left as it was."""
    parts = path.split(".")

    def mutator(output: dict) -> dict:
        out = dict(output)
        node = out
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                return output
            node[part] = dict(child)
            node = node[part]
        current = node.get(parts[-1])
        if current is not None and not isinstance(current, list):
            raise TypeError(f"add_stray needs a list at '{path}'; use set_key for a single value")
        node[parts[-1]] = [*(current or []), item]
        return out

    return mutator


def prove_gate_can_fail(
    flow_name: str,
    flow: FlowFn,
    cases: list[Case],
    checks: list[Check],
    gate: Gate,
    mutator: Mutator,
    *,
    k: int = 1,
) -> tuple[Report, Report]:
    """Return (baseline, broken) reports; raise AssertionError if the mutant still passes the gate."""
    baseline = run_eval(flow_name, flow, cases, checks, gate, k=k)
    broken = run_eval(f"{flow_name}+mutation", mutate(flow, mutator), cases, checks, gate, k=k)
    if broken.verdict.passed:
        raise AssertionError(f"gate '{gate.name}' still passes with the flow broken: it cannot go red")
    return baseline, broken


def main() -> int:
    from evals.checks import effects_match_trace
    from evals.toy import TOY_CHECKS, TOY_GATE, toy_flow

    cases = load_cases(Path(__file__).parent / "datasets" / "toy.jsonl")
    # The toy flow calls no tools, so any call its output reports is one that never ran: the
    # agent that declines in words but still reports a successful send, with whatever data.
    effects = effects_match_trace("effects_match_trace", held=lambda o: bool(o.get("escalated")))
    checks = [*TOY_CHECKS, effects]
    gate = TOY_GATE.model_copy(update={"must_always_pass": [*TOY_GATE.must_always_pass, effects.name]})
    phantom = mutate(toy_flow, set_key("executed_actions", ["send_patient_message"]))
    with tracing.run("break", content="none"):
        baseline = run_eval("toy", toy_flow, cases, checks, gate, k=1)
        broken = run_eval("toy+never_escalate", _never_escalate(toy_flow), cases, checks, gate, k=1)
        unbacked = run_eval("toy+phantom_tool_call", phantom, cases, checks, gate, k=1)
    for report in (baseline, broken, unbacked):
        print(render_markdown(report))
    red = not broken.verdict.passed and not unbacked.verdict.passed
    return 0 if baseline.verdict.passed and red else 1


def _never_escalate(flow: FlowFn) -> FlowFn:
    return mutate(flow, set_key("escalated", False))


if __name__ == "__main__":
    sys.exit(main())
