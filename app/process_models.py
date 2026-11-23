"""The /process API contract: what the React app (web/) sends and receives.

Everything here is a pydantic model so FastAPI validates it and publishes it at /openapi.json.
The routes live in app/api.py. Reused unchanged: GET /runs, GET /runs/{id}, GET /runs/{id}/events
and POST /runs/{id}/decide (sign-off decisions).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from flows.process_qa.state import QAOutcome, Target

MAX_QUESTION_CHARS = 1000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_TYPES = (".pdf", ".docx", ".txt")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- POST /process/extract (multipart: file) ------------------------------------------------------


class ExtractResponse(Strict):
    """A draft form for the intern to confirm. Never submitted by the server."""

    form: dict  # Submission-shaped; any field the document doesn't state is null / missing
    missing: list[str]  # field paths the intern must fill or check, e.g. "chemicals.0.unit"
    quotes: dict[str, str]  # field path -> the text in the document it came from
    document_digest: str  # sha256 of the extracted text; the text itself is never stored
    warnings: list[str] = Field(default_factory=list)  # e.g. "injection_signals_in_notes"


# --- POST /process/review --------------------------------------------------------------------------


class ReviewRequest(Strict):
    submission: dict  # a confirmed Submission (flows/process_review/state.py)


class ReviewStarted(Strict):
    run_id: str
    # Names the plant doesn't recognise after normalising ("Toluene" -> toluene is recognised). The
    # review still runs and flags each as a coverage gap; the app shows them so a typo is caught early.
    unknown_ids: list[str] = Field(default_factory=list)


# --- GET /process/reports/{digest} -----------------------------------------------------------------


class ReportView(Strict):
    """The exact report an approver is asked to sign, looked up by the digest in the pending call."""

    report_digest: str
    product: str
    summary: str
    recommendation: Literal["proceed_to_signoff", "revise", "reject"]
    rationale: str
    flags: list[dict]  # Flag (flows/process_review/state.py)
    lanes: dict[str, str]
    required_approvers: list[Literal["supervisor", "ehs"]]


# --- POST /process/ask -----------------------------------------------------------------------------


class AskRequest(Strict):
    run_id: str | None = None  # the session's review run; required for my_process and report
    target: Target
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


AskResponse = QAOutcome


# --- GET /process/examples ---------------------------------------------------------------------------


class TargetInfo(Strict):
    id: Target
    label: str
    description: str
    questions: list[str]


class Workflow(Strict):
    title: str
    description: str
    steps: list[str]


class ExamplesResponse(Strict):
    targets: list[TargetInfo]
    workflows: list[Workflow]
    wont_answer: list[str]
