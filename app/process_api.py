"""The product app's routes (/process/*): upload extraction, review start, report lookup, scoped Q&A.

Unlike the rest of app/, this imports the process_review / process_qa flows directly: extraction,
Q&A and report lookup are synchronous product calls, not runs. The review itself is still a run
(kit.runner), and its sign-offs go through the reused /runs routes. Contract: app/process_models.py.

Nothing here logs or stores a document, a question or an answer. Q&A binds its context on the
server, from the run id alone: no submission or report ever comes from the request.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import ValidationError

from app.process_models import (
    MAX_UPLOAD_BYTES,
    UPLOAD_TYPES,
    AskRequest,
    AskResponse,
    ExamplesResponse,
    ExtractResponse,
    ReportView,
    ReviewRequest,
    ReviewStarted,
)
from flows.process_qa.examples import QUESTIONS, TARGET_INFO, WONT_ANSWER, WORKFLOWS
from flows.process_qa.state import TARGETS, QAOutcome
from kit import runner, tracing

router = APIRouter(prefix="/process")


@router.get("/examples")
def examples() -> ExamplesResponse:
    return ExamplesResponse(
        targets=[{"id": t, **TARGET_INFO[t], "questions": QUESTIONS[t]} for t in TARGETS],
        workflows=WORKFLOWS,
        wont_answer=WONT_ANSWER,
    )


@router.post("/extract")
async def extract(file: UploadFile) -> ExtractResponse:
    from flows.process_review.extract import document_text, extract_form

    name = (file.filename or "").lower()
    if not name.endswith(UPLOAD_TYPES):
        raise HTTPException(415, f"unsupported file type; use one of {list(UPLOAD_TYPES)}")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file larger than {MAX_UPLOAD_BYTES} bytes")
    actor = tracing.actor("process_extract", version="1")
    try:
        with tracing.run("process-extract", regulated=True, actor=actor):
            text = document_text(file.filename or "", data)
            result = extract_form(text)
    except ValueError as e:
        raise HTTPException(422, f"unreadable document: {_code(e)}") from None
    return ExtractResponse(**result)


def _code(e: Exception) -> str:
    """The error's short code only: messages could carry document text."""
    first = (str(e).split() or ["invalid"])[0]
    return first if first.replace("_", "").isalnum() and len(first) <= 40 else "invalid"


@router.post("/review")
def review(req: ReviewRequest) -> ReviewStarted:
    from flows.process_review.extract import normalize_ids
    from flows.process_review.state import Submission

    try:
        sub = Submission.model_validate(req.submission)
    except ValidationError as e:
        paths = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
        raise HTTPException(422, f"invalid submission at: {paths}") from None
    # A typed form and an uploaded one must mean the same thing: map names to plant ids first.
    form, unknown = normalize_ids(sub.model_dump())
    run_id = runner.start_input(
        "process_review", {"submission": Submission.model_validate(form).model_dump()}
    )
    return ReviewStarted(run_id=run_id, unknown_ids=unknown)


def _archived_report(digest: str) -> dict | None:
    """The archived object for a digest, only if it is a report (never a stored submission)."""
    from flows.process_review.tools import ARCHIVE

    if not isinstance(digest, str) or digest not in ARCHIVE:
        return None
    obj = ARCHIVE.get(digest)
    ok = isinstance(obj, dict) and "flags" in obj and "required_approvers" in obj
    return obj if ok else None


@router.get("/reports/{digest}")
def report(digest: str) -> ReportView:
    obj = _archived_report(digest)
    if obj is None:
        raise HTTPException(404, "unknown report")
    return ReportView(
        report_digest=digest, **{k: obj.get(k) for k in ReportView.model_fields if k != "report_digest"}
    )


def _bound_context(run_id: str) -> tuple[dict | None, dict | None]:
    """(submission, report) of a process_review run, resolved from server state only."""
    try:
        st = runner.status(run_id)
    except KeyError:
        raise HTTPException(404, "unknown run") from None
    given = runner.run_input(run_id) if st["flow"] == "process_review" else None
    if given is None:
        return None, None
    sub = given.get("submission")
    rep = None
    if st["status"] == "done" and st["output"]:
        out = st["output"]
        archived = _archived_report(out.get("report_digest", ""))
        # The archived copy is what the approvers saw; the outcome adds how the sign-offs went.
        # A form returned as incomplete has no report at all, so "the report" can't be asked about.
        if archived:
            rep = {**archived, "status": out.get("status"), "signoffs": out.get("signoffs", [])}
        elif out.get("report_digest"):
            rep = out
    elif st["status"] == "waiting":
        for call in st["pending"]:
            rep = _archived_report((call.get("input") or {}).get("report_digest", ""))
            if rep:
                break
    return sub, rep


@router.post("/ask")
def ask(req: AskRequest) -> AskResponse:
    from flows.process_qa.graph import run_qa

    sub, rep = _bound_context(req.run_id) if req.run_id is not None else (None, None)
    actor = tracing.actor("process_qa", version="1")
    with tracing.run("process-qa", regulated=True, actor=actor):
        out = run_qa(req.question, req.target, submission=sub, report=rep)
    return QAOutcome.model_validate(out)
