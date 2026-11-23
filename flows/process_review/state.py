"""Formulation / process review for floor supervisors — state, schemas and the output contract.

Flow (graph.py; plan: plans/process_review.spec.json):

    intake ─┬─► hazard_rules ─► plan_queries ─┬─► search_safety ──────► safety_review ──────┐
            │                                 ├─► search_maintenance ─► maintenance_review ─┼─► verify
            │                                 └─► search_qc ──────────► qc_review ──────────┘     │
            │                                                                                     ▼
            │                              synthesis ◄──(revise once)── report_guard ◄── synthesis
            │                                                                │
            │                                                     supervisor_signoff ─(critical)─► ehs_signoff
            └─(incomplete form)──────────────────────────────────────────────┴───────────────────► finalize

Design rules this encodes:
- The agent never approves. A process is approved only when every required human sign-off
  (`sign_off_process`, an external tool) ran after an approval of that exact call. The model's
  recommendation is a closed vocabulary with no "approve" in it.
- Only current documents exist for the model: the index holds approved, in-effect documents at
  their highest version with approved amendments merged in (docs.DocIndex). verify re-checks every
  citation against the index when the report is built.
- Deterministic floors: hazard rules (incompatible pairs, chemical limits, equipment ratings and
  service status) come from rule rows inside the current documents. Their flags are `locked`: no
  model can remove or downgrade them. Coverage gaps (an item no current document covers) are found
  by code, never by a model.
- Flags carry the citations; the code fills doc_id/version from the chunk, so a model can't
  misreport which version it read. A number in a flag must appear in its quotes or on the form.
- Final report flags are assembled by code (locked + verified lane flags + verified cross flags +
  gaps + intake flags). Synthesis writes only the summary, cross-domain flags and recommendation.
- Submitter and approvers are carried as role + pseudonym. Traces store no content (regulated run).
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

Store = Literal["safety", "maintenance", "qc"]
STORES: tuple[Store, ...] = ("safety", "maintenance", "qc")
Lane = Literal["safety", "maintenance", "qc", "cross", "intake"]
Severity = Literal["info", "minor", "major", "critical"]
SEVERITY_RANK: dict[str, int] = {"info": 0, "minor": 1, "major": 2, "critical": 3}
FlagKind = Literal[
    "incompatible",  # two chemicals that must not be combined
    "over_limit",  # a chemical's temperature / concentration over its documented limit
    "over_rating",  # a step's temperature / pressure over the equipment's rating
    "out_of_service",  # equipment not in service (locked out, under repair)
    "spec_out_of_range",  # a product parameter outside its QC specification
    "missing_control",  # a required control (ventilation, scrubber, PPE, test) not in the process
    "cross_conflict",  # two areas' documents disagree for this process
    "coverage_gap",  # no current document covers this chemical / equipment / product
    "unverified",  # a model concern whose citation or number could not be verified
    "injection",  # the free-text notes tried to instruct the reviewer
    "other",
]
# What a model may call a flag. unverified, coverage_gap and injection are verdicts only code gives:
# a model labelling its own claim "unverified" would blur which flags verification rejected.
ModelFlagKind = Literal[
    "incompatible", "over_limit", "over_rating", "out_of_service", "spec_out_of_range",
    "missing_control", "cross_conflict", "other",
]  # fmt: skip
DocStatus = Literal["approved", "draft", "superseded", "withdrawn", "discontinued"]
Recommendation = Literal["proceed_to_signoff", "revise", "reject"]
ApproverRole = Literal["supervisor", "ehs"]
Status = Literal["approved", "not_approved", "returned_to_submitter"]
Unit = Literal["kg", "g", "L", "mL"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the submission form (input) ---------------------------------------------------------------


class ChemicalLine(Strict):
    id: str  # canonical slug, e.g. "acetone", "hydrogen_peroxide" (WORLD.md)
    quantity: float | None = None
    unit: Unit | None = None
    concentration_pct: float | None = None  # % w/w of the active ingredient as supplied


class Step(Strict):
    n: int
    action: str  # short imperative, e.g. "charge", "mix", "heat", "cure"
    equipment: str  # equipment id, e.g. "R-101"
    chemicals: list[str] = Field(default_factory=list)  # chemical ids present in this step
    temp_c: float | None = None
    pressure_bar: float | None = None  # gauge
    duration_min: float | None = None


class Submission(Strict):
    submission_id: str  # "SUB-0001"
    product: str  # product code, e.g. "CX-200"
    submitter: str  # synthetic employee id "EMP-0xx"; pseudonymized at intake
    chemicals: list[ChemicalLine]
    steps: list[Step]
    notes: str = ""  # free text: untrusted; screened, and kept out of prompts when flagged


# --- documents, citations, rules ---------------------------------------------------------------


class Citation(Strict):
    chunk_id: str  # "<doc_id>@<version>#<n>", from the current index only
    doc_id: str
    version: str  # e.g. "C", or "C+A1" when an amendment is merged in
    quote: str  # exact substring of the chunk text


class RuleRow(Strict):
    """One machine-readable rule from a current document (docs.DocIndex.rules()).

    kind=incompatible  : a, b (chemical ids)
    kind=chem_limit    : chemical, max_temp_c and/or max_concentration_pct
    kind=equipment     : equipment, max_temp_c, max_pressure_bar, in_service
    kind=requires      : chemical, control (an equipment id that must be in service, e.g. "SC-1")
    """

    kind: Literal["incompatible", "chem_limit", "equipment", "requires"]
    severity: Severity  # severity of the flag when this rule is broken
    a: str | None = None
    b: str | None = None
    chemical: str | None = None
    equipment: str | None = None
    control: str | None = None
    max_temp_c: float | None = None
    max_pressure_bar: float | None = None
    max_concentration_pct: float | None = None
    in_service: bool | None = None
    hazard: str = ""  # short, e.g. "forms explosive organic peroxides"
    citation: Citation  # the sentence in the document that states this rule


class Flag(Strict):
    """One issue in the report. Built by code (rules, verify, intake) or from a model flag."""

    lane: Lane
    kind: FlagKind
    severity: Severity
    subjects: list[str]  # form item ids: chemical ids, equipment ids, the product code, "step:<n>"
    title: str  # one line
    detail: str = ""
    citations: list[Citation] = Field(default_factory=list)
    source: Literal["rule", "model", "verify", "intake"]
    locked: bool = False  # rule flags: never removed or downgraded


# --- model outputs (structured) ----------------------------------------------------------------


class QueryPlan(Strict):
    safety: list[str]
    maintenance: list[str]
    qc: list[str]


class CitedQuote(Strict):
    chunk_id: str  # must be one of the passages this step was shown
    quote: str  # copied exactly from that passage


class ModelFlag(Strict):
    kind: ModelFlagKind
    severity: Severity
    subjects: list[str]
    title: str
    detail: str
    citations: list[CitedQuote]


class LaneReport(Strict):
    summary: str  # 2-4 sentences, plain language for a supervisor
    flags: list[ModelFlag]


class SynthesisDraft(Strict):
    summary: str  # what the supervisor needs to know first, plain language
    cross_flags: list[ModelFlag]  # conflicts between areas; cite a passage from each side
    recommendation: Recommendation
    rationale: str


# --- graph state -------------------------------------------------------------------------------


class ReviewState(TypedDict, total=False):
    submission: dict  # Submission, validated at intake
    submitter_pseudonym: str
    submission_digest: str
    incomplete: list[str]  # field paths missing / invalid; non-empty -> returned to submitter
    injection_flagged: bool
    intake_flags: list[dict]  # Flag
    locked_flags: list[dict]  # Flag, from hazard_rules
    queries: dict  # {"safety": [...], "maintenance": [...], "qc": [...]}
    passages: Annotated[dict, operator.or_]  # store -> [{chunk_id, doc_id, version, heading, text}]
    lane_reports: Annotated[dict, operator.or_]  # store -> {"summary", "flags": [ModelFlag dict]}
    flags: list[dict]  # Flag: verified lane flags + locked + gaps + intake (verify)
    dropped: list[dict]  # {"lane", "reason"} for model flags that failed verification
    synthesis: dict  # SynthesisDraft + {"cross_flags": [Flag dict]} after its own verification
    guard: dict  # {"passed", "violations": [codes], "fallback": bool}
    revisions: int
    report: dict  # the final report put in front of the approvers
    report_digest: str
    signoffs: Annotated[list[dict], operator.add]  # {"role", "decision", "approver"}
    executed: Annotated[list[str], operator.add]
    blocked: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[dict], operator.add]
    outcome: dict


# --- the output contract: what the flow returns to callers and to evals ------------------------

OUTCOME_KEYS = {
    "status": "Status — approved only when every required sign-off executed",
    "recommendation": "Recommendation | None — the agent's advice (None when returned)",
    "required_approvers": "list[ApproverRole] — supervisor always; ehs when any flag is critical",
    "signoffs": "list[{role, decision, approver}] in the order they were asked",
    "flags": "list[Flag] — the report's flags, highest severity first",
    "summary": "str — the plain-language summary for the supervisor ('' when returned)",
    "lanes": "dict[Store, str] — each lane's own summary",
    "incomplete": "list[str] — form fields that sent it back to the submitter",
    "injection_flagged": "bool",
    "dropped": "int — model flags removed by verification",
    "guard_violations": "list[str] — report-guard codes from the last check",
    "fallback_report": "bool — the code-built report replaced synthesis",
    "executed_actions": "list[str] — external tools that executed after approval",
    "blocked_actions": "list[str] — external tools refused",
    "tool_calls": "list[{name}] — external tool calls",
    "report_digest": "str — sha256 of the report the approvers were shown",
}
