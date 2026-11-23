"""HTTP API over kit.runner: what the Streamlit UI (and anything else) talks to.

Flows are reached through kit.runner and kit.cli, except app/process_api.py: the product app's
routes import the process_review / process_qa flows directly, because upload extraction, Q&A and
report lookup are synchronous product calls, not runs.
Everything returned is what the sanitized traces and the ledger already hold.

Auth: if KIT_API_TOKEN is set, every non-GET route needs `Authorization: Bearer <token>`. GETs stay
open. The approver on a decision is self-asserted text: a demo identity recorded in the ledger,
not authentication. Real deployments put real identity (SSO, a signed-in session) in front of it.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.process_api import router as process_router
from kit import audit_store, cli, runner
from kit.boundary import Ledger
from kit.planspec import PlanSpec, drift

PLANS_DIR = Path(__file__).resolve().parents[1] / "plans"


def require_token(request: Request) -> None:
    token = os.getenv("KIT_API_TOKEN")
    if not token or request.method == "GET":
        return
    sent = request.headers.get("authorization", "")
    if not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})


app = FastAPI(title="process-review-agent", version="0.1.0", dependencies=[Depends(require_token)])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # the Vite dev server only
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(process_router, dependencies=[Depends(require_token)])


class RunRequest(BaseModel):
    flow: str
    case_id: str | None = None


class DecideRequest(BaseModel):
    decisions: dict[str, dict]


def _flow_or_404(flow: str) -> str:
    if flow not in runner.flows():
        raise HTTPException(404, f"unknown flow {flow!r}")
    return flow


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/flows")
def list_flows() -> list[str]:
    return runner.flows()


@app.get("/flows/{flow}/cases")
def flow_cases(flow: str) -> list[dict]:
    return runner.cases(_flow_or_404(flow))


@app.get("/flows/{flow}/graph")
def flow_graph(flow: str) -> dict:
    return {"flow": flow, "mermaid": cli.mermaid(_flow_or_404(flow))}


@app.post("/runs")
def start_run(req: RunRequest) -> dict:
    _flow_or_404(req.flow)
    try:
        return {"run_id": runner.start(req.flow, case_id=req.case_id)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def _public(status: dict) -> dict:
    """The status without the server-side trace path."""
    return {k: v for k, v in status.items() if k != "trace"}


@app.get("/runs")
def list_runs() -> list[dict]:
    return [_public(s) for s in runner.runs()]


def _run_or_404(run_id: str) -> str:
    try:
        runner.status(run_id)
    except KeyError:
        raise HTTPException(404, f"unknown run {run_id!r}") from None
    return run_id


@app.get("/runs/{run_id}")
def run_status(run_id: str) -> dict:
    return _public(runner.status(_run_or_404(run_id)))


@app.get("/runs/{run_id}/events")
def run_events(run_id: str, since: int = 0) -> list[dict]:
    return runner.events(_run_or_404(run_id), since)


@app.post("/runs/{run_id}/decide")
def run_decide(run_id: str, req: DecideRequest) -> dict:
    _run_or_404(run_id)
    pending = {c["tool_use_id"] for c in runner.pending(run_id)}
    unknown = sorted(set(req.decisions) - pending)
    if unknown:
        raise HTTPException(422, f"not pending for this run: {unknown}")
    try:
        runner.decide(run_id, req.decisions)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


def _plan_path(name: str) -> Path:
    path = PLANS_DIR / f"{name}.spec.json"
    if "/" in name or "\\" in name or not path.exists():
        raise HTTPException(404, f"unknown plan {name!r}")
    return path


@app.get("/plans")
def list_plans() -> list[str]:
    return sorted(p.name.removesuffix(".spec.json") for p in PLANS_DIR.glob("*.spec.json"))


@app.get("/plans/{name}")
def get_plan(name: str) -> dict:
    spec = PlanSpec.load(_plan_path(name))
    result = {
        "spec": spec.model_dump(),
        "mermaid": spec.mermaid(),
        "problems": spec.problems(),
        "drift": None,
    }
    if spec.flow in runner.flows():
        result["drift"] = drift(spec, cli.capture_graph(spec.flow).get_graph())
    return result


@app.get("/audit")
def audit(limit: int = 200) -> list[dict]:
    """The tail of the append-only ledger, newest last. Read only."""
    path = Ledger().path
    if limit <= 0 or not path.exists():
        return []
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return [json.loads(line) for line in lines[-limit:]]


# --- regulated decision log: what decided, never what anyone wrote ------------------------------


@app.get("/audit/runs")
def audit_runs() -> list[dict]:
    return audit_store.runs()


@app.get("/audit/runs/{run_id}")
def audit_run(run_id: str) -> dict:
    """The decision trail (no content) and whether the whole hash chain still verifies."""
    if run_id not in {r["run_id"] for r in audit_store.runs()}:
        raise HTTPException(404, f"unknown audit run {run_id!r}")
    ok, detail = audit_store.verify()
    return {"trail": audit_store.explain(run_id), "chain": {"ok": ok, "detail": detail}}


@app.get("/audit/controls")
def audit_controls() -> dict:
    controls = [{"control": n, "reference": ref, "how": how} for n, ref, how in audit_store.CONTROLS]
    return {"controls": controls, "gaps": audit_store.GAPS}
