"""Scoped questions about a process review — state, schemas and the output contract.

Flow (graph.py; plan: plans/process_qa.spec.json):

    intake ─► harm_screen ─┬─► refuse ─────────────────────────────────────────────► finalize
                           └─► scope_gate ─┬─► refuse
                                           ├─► search_store ─┐
                                           └─► load_own ─────┴─► answer ⇄ answer_guard ─► finalize

Design rules this encodes:
- The user picks the target; the scope gate checks it (code rules first, then a classifier) and
  may suggest another. Nothing is answered from general knowledge.
- "my_process" and "report" read only the submission and report bound to the session's review
  run, passed in by the server. Nothing in the question can select another submission.
- Chat is advice about documents, never a decision: no approval or safe-to-run wording, no
  rewritten recipe. Changes go back through upload, review and sign-off.
- A question seeking harm is refused and an EHS notice is recorded (codes only, no text).
- Answers cite: document chunks with exact quotes (current versions only), form field paths, or
  report flag ids. The answer guard checks every one; numbers must be in a cited source.
- Each question stands alone (no chat memory). Runs are regulated: traces store no content.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

Target = Literal["my_process", "safety", "maintenance", "qc", "report"]
TARGETS: tuple[Target, ...] = ("my_process", "safety", "maintenance", "qc", "report")
STORE_TARGETS = ("safety", "maintenance", "qc")
Category = Literal[
    "limit_lookup",  # a documented temperature / pressure / concentration limit
    "compatibility",  # whether two chemicals may be combined, per the documents
    "handling_requirement",  # PPE, ventilation, controls a document requires
    "equipment_status",  # in service, locked out, retired
    "equipment_rating",  # pressure / temperature rating
    "spec_lookup",  # a product specification value or method
    "process_detail",  # what the bound submission says (quantities, steps, equipment)
    "explain_flag",  # what a flag in the bound report means and what it cites
    "explain_recommendation",  # why the report recommends what it does
    "out_of_scope",
]
RefusalReason = Literal[
    "harmful_intent",
    "harm_screen_failed",
    "injection",
    "asks_approval",  # approve it / is it safe / can I run it
    "asks_rewrite",  # write me a corrected recipe
    "other_submission",  # names a submission that isn't the session's
    "out_of_scope",  # off-topic, general knowledge, not this target's documents
    "wrong_target",  # in scope for a different target (suggested_target says which)
    "no_bound_run",  # my_process / report asked with no review run in the session
    "no_report_yet",  # report asked before the review produced one
    "too_long",
]
Status = Literal["answered", "refused", "withheld"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- model outputs (structured) ----------------------------------------------------------------


class HarmVerdict(Strict):
    harmful: bool
    signals: list[str]  # short snake_case codes, e.g. "maximize_hazard", "defeat_control"


class ScopeVerdict(Strict):
    in_scope: bool  # answerable from THIS target's material
    category: Category
    suggested_target: Target | None  # when another target is the right one
    reason_code: str  # short snake_case


class Cite(Strict):
    kind: Literal["chunk", "field", "flag"]
    ref: str  # chunk id "SOP-SAF-012@C+A1#2" | field path "steps.1.temp_c" | flag id "flag:2"
    quote: str  # chunk: exact text from it; field: the value as written; flag: the flag's title


class DraftAnswer(Strict):
    answer: str  # plain language, 1-5 sentences
    citations: list[Cite]
    cannot_answer: bool  # the loaded material doesn't answer it: say so instead of guessing


# --- graph state -------------------------------------------------------------------------------


class QAState(TypedDict, total=False):
    question: str
    target: str  # Target
    submission: dict | None  # bound by the server from the session's review run
    report: dict | None  # bound by the server: the review outcome / archived report
    injection_flagged: bool
    harm: dict  # {"harmful", "signals", "source": "lexicon"|"classifier"|"both"|"none"|"failed"}
    scope: dict  # ScopeVerdict + {"rule": code rule that decided, or "classifier"}
    refusal: dict  # {"reason": RefusalReason, "suggested_target": Target | None}
    material: list[dict]  # what the answer may use: passages, fields, flags (each with a ref)
    draft: dict  # DraftAnswer
    guard: dict  # {"passed", "violations": [codes], "revise", "withheld"}
    revisions: int
    outcome: dict


# --- the output contract: what the flow returns to callers, the API and evals ------------------


class Citation(Strict):
    kind: Literal["chunk", "field", "flag"]
    ref: str
    quote: str
    doc_id: str | None = None  # chunk citations: from the index, not the model
    version: str | None = None


class QAOutcome(Strict):
    status: Status
    target: Target
    answer: str = ""  # '' unless answered
    citations: list[Citation] = Field(default_factory=list)  # [] unless answered; withheld shows passages
    refusal_reason: RefusalReason | None = None
    suggested_target: Target | None = None
    examples: list[str] = Field(default_factory=list)  # example questions for the right target
    category: Category | None = None
    ehs_notice: bool = False  # a harm refusal recorded for EHS
    injection_flagged: bool = False
    guard_violations: list[str] = Field(default_factory=list)
    executed_actions: list[str] = Field(default_factory=list)  # always []: Q&A has no external tools
    tool_calls: list[dict] = Field(default_factory=list)  # always []


OUTCOME_KEYS = {name: f.description or str(f.annotation) for name, f in QAOutcome.model_fields.items()}
