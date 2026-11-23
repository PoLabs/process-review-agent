"""Eval contracts. Flows, checks, gates and reports all speak these types.

A flow under test is any `FlowFn`: it takes one Case and returns a structured output dict
(route label, escalation flag, tool calls, final draft, ...). Checks read that dict. The
harness runs every case k times, because one run of a non-deterministic system is an
anecdote; the report gives pass *rates* and names the flaky cases.

The ship gate is conjunctive: a high overall score does not pass if a must-always-pass
check failed even once. A score can measure fluency while the task quietly didn't happen.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class Case(BaseModel):
    id: str
    category: str  # e.g. "scheduling", "refill", "safety", "adversarial"
    input: dict  # flow-specific payload
    expect: dict = Field(default_factory=dict)  # flow-specific expectations, read by checks
    tags: list[str] = Field(default_factory=list)
    twin_of: str | None = None  # adversarial twin: same facts as `twin_of`, hardened delivery
    pii: list[str] = Field(default_factory=list)  # synthetic identifiers in `input`; redacted exactly


class CheckResult(BaseModel):
    check: str
    passed: bool
    applicable: bool = True  # N/A results are excluded from rates (e.g. a safety check on a billing case)
    detail: str = ""  # structural only — labels, ids, counts. Sanitized before it reaches disk.


class Check(Protocol):
    name: str
    kind: Literal["deterministic", "judge"]

    def __call__(self, case: Case, output: dict) -> CheckResult: ...


FlowFn = Callable[[Case], dict]


class Gate(BaseModel):
    """Every condition must hold for the gate to pass."""

    name: str
    min_pass_rate: float = 0.98  # share of (case, repeat) runs where every applicable check passed
    must_always_pass: list[str] = Field(default_factory=list)  # 100% of applicable runs, no exceptions
    category_min: dict[str, float] = Field(default_factory=dict)  # e.g. {"safety": 1.0}


class RunResult(BaseModel):
    case_id: str
    category: str
    repeat: int
    passed: bool  # no error and every applicable check passed
    checks: list[CheckResult]
    error: str | None = None
    latency_s: float = 0.0


class GateVerdict(BaseModel):
    passed: bool
    reasons: list[str]  # one line per failed condition, with the numbers


class Report(BaseModel):
    flow: str
    gate: str
    n_cases: int
    k: int
    pass_rate: float
    by_category: dict[str, float]
    by_check: dict[str, float]  # pass rate over applicable runs only
    flaky_cases: list[str]  # passed on some repeats, failed on others
    verdict: GateVerdict
    runs: list[RunResult]
    meta: dict = Field(default_factory=dict)  # profile, models, started_at, duration_s
