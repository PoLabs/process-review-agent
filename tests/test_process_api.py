"""Offline tests for app/process_api.py: shapes, upload limits, server-side Q&A binding, CORS, auth."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.process_models import MAX_UPLOAD_BYTES
from flows.process_review.tools import ARCHIVE
from kit import runner, tracing

client = TestClient(app)

SUB = {
    "submission_id": "SUB-0001",
    "product": "CX-200",
    "submitter": "EMP-001",
    "chemicals": [{"id": "acetone", "quantity": 2.0, "unit": "kg"}],
    "steps": [{"n": 1, "action": "mix", "equipment": "M-7", "chemicals": ["acetone"], "temp_c": 20.0}],
}
REPORT = {
    "submission_digest": "x",
    "product": "CX-200",
    "summary": "s",
    "recommendation": "revise",
    "rationale": "r",
    "flags": [{"id": "flag:1"}],
    "lanes": {"safety": "done"},
    "required_approvers": ["supervisor"],
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("KIT_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("KIT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("KIT_API_TOKEN", raising=False)


def _stub(monkeypatch, module: str, **attrs):
    mod = types.ModuleType(module)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, module, mod)


def test_examples_shape():
    body = client.get("/process/examples").json()
    assert [t["id"] for t in body["targets"]] == ["my_process", "safety", "maintenance", "qc", "report"]
    assert all(t["label"] and t["questions"] for t in body["targets"])
    assert body["workflows"] and all(w["steps"] for w in body["workflows"])
    assert body["wont_answer"]


def test_extract_happy_path(monkeypatch):
    seen = {}

    def extract_form(text):
        seen["text"] = text
        return {"form": {"product": "CX-200"}, "missing": ["steps"], "quotes": {"product": "CX-200"},
                "document_digest": "d" * 64, "warnings": []}  # fmt: skip

    _stub(
        monkeypatch,
        "flows.process_review.extract",
        document_text=lambda n, d: d.decode(),
        extract_form=extract_form,
    )
    resp = client.post("/process/extract", files={"file": ("recipe.txt", b"product CX-200")})
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing"] == ["steps"] and seen["text"] == "product CX-200"


def test_extract_rejects_type_and_size(monkeypatch):
    _stub(
        monkeypatch, "flows.process_review.extract", document_text=lambda n, d: "", extract_form=lambda t: {}
    )
    assert client.post("/process/extract", files={"file": ("a.exe", b"x")}).status_code == 415
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    assert client.post("/process/extract", files={"file": ("a.txt", big)}).status_code == 413


def test_extract_value_error_is_a_code_only(monkeypatch):
    def boom(name, data):
        raise ValueError("bad_pdf secret document words")

    _stub(monkeypatch, "flows.process_review.extract", document_text=boom, extract_form=lambda t: {})
    resp = client.post("/process/extract", files={"file": ("a.pdf", b"x")})
    assert resp.status_code == 422 and "secret" not in resp.text


def test_review_validates_then_starts(monkeypatch):
    started = {}

    def start_input(flow, input, **kw):
        started.update(flow=flow, input=input)
        return "run123"

    monkeypatch.setattr(runner, "start_input", start_input)
    bad = client.post("/process/review", json={"submission": {"product": "CX-200", "secret_note": "hunter2"}})
    assert bad.status_code == 422 and "hunter2" not in bad.text and "submission_id" in bad.text
    ok = client.post("/process/review", json={"submission": SUB})
    assert ok.status_code == 200 and ok.json() == {"run_id": "run123", "unknown_ids": []}
    assert (
        started["flow"] == "process_review" and started["input"]["submission"]["submission_id"] == "SUB-0001"
    )


def test_reports_only_serve_reports():
    assert client.get("/process/reports/nope").status_code == 404
    sub_digest = ARCHIVE.put({**SUB, "submitter": "pseudo"})
    assert client.get(f"/process/reports/{sub_digest}").status_code == 404
    digest = ARCHIVE.put(REPORT)
    body = client.get(f"/process/reports/{digest}").json()
    assert body["report_digest"] == digest and body["required_approvers"] == ["supervisor"]
    assert "submission_digest" not in body


def _fake_run(monkeypatch, *, status="done", output=None, pending=None, flow="process_review", given=True):
    st = {"id": "r1", "flow": flow, "status": status, "output": output, "pending": pending or []}

    def get_status(run_id):
        if run_id != "r1":
            raise KeyError(run_id)
        return st

    monkeypatch.setattr(runner, "status", get_status)
    monkeypatch.setattr(runner, "run_input", lambda run_id: {"submission": SUB} if given else None)


def _qa(monkeypatch):
    calls = []

    def run_qa(question, target, *, submission, report):
        calls.append({"question": question, "target": target, "submission": submission, "report": report})
        return {"status": "answered", "target": target, "answer": "ok"}

    _stub(monkeypatch, "flows.process_qa.graph", run_qa=run_qa)
    return calls


def test_ask_binds_context_from_the_run_not_the_request(monkeypatch):
    calls = _qa(monkeypatch)
    digest = ARCHIVE.put(REPORT)
    _fake_run(monkeypatch, status="done", output={"report_digest": digest, "flags": []})
    body = {"run_id": "r1", "target": "my_process", "question": "q?", "submission": {"product": "EVIL"}}
    assert client.post("/process/ask", json=body).status_code == 422  # extra fields are forbidden
    del body["submission"]
    resp = client.post("/process/ask", json=body)
    assert resp.status_code == 200 and resp.json()["answer"] == "ok"
    assert calls[0]["submission"] == SUB
    # the archived report the approvers saw, plus how the sign-offs went
    assert calls[0]["report"] == {**REPORT, "status": None, "signoffs": []}


def test_ask_uses_the_pending_report_while_waiting(monkeypatch):
    calls = _qa(monkeypatch)
    digest = ARCHIVE.put({**REPORT, "summary": "pending one"})
    pending = [{"tool_use_id": "t", "tool": "sign_off_process", "input": {"report_digest": digest}}]
    _fake_run(monkeypatch, status="waiting", pending=pending)
    assert (
        client.post("/process/ask", json={"run_id": "r1", "target": "report", "question": "q"}).status_code
        == 200
    )
    assert calls[0]["report"]["summary"] == "pending one"


def test_ask_unknown_run_and_no_run(monkeypatch):
    calls = _qa(monkeypatch)
    _fake_run(monkeypatch)
    assert (
        client.post("/process/ask", json={"run_id": "zzz", "target": "safety", "question": "q"}).status_code
        == 404
    )
    assert client.post("/process/ask", json={"target": "safety", "question": "q"}).status_code == 200
    assert calls == [{"question": "q", "target": "safety", "submission": None, "report": None}]


def test_ask_ignores_runs_of_other_flows(monkeypatch):
    calls = _qa(monkeypatch)
    _fake_run(monkeypatch, flow="other_flow", given=False)
    client.post("/process/ask", json={"run_id": "r1", "target": "report", "question": "q"})
    assert calls[0]["submission"] is None and calls[0]["report"] is None


def test_cors_allows_only_the_dev_server():
    ok = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.get("/healthz", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in other.headers


def test_token_required_on_process_posts(monkeypatch):
    monkeypatch.setenv("KIT_API_TOKEN", "s3cret")
    assert client.get("/process/examples").status_code == 200
    assert client.post("/process/review", json={"submission": SUB}).status_code == 401
    assert client.post("/process/ask", json={"target": "safety", "question": "q"}).status_code == 401
    assert client.post("/process/extract", files={"file": ("a.txt", b"x")}).status_code == 401


def test_a_returned_form_has_no_report_to_ask_about(monkeypatch):
    calls = _qa(monkeypatch)
    _fake_run(
        monkeypatch,
        status="done",
        output={"status": "returned_to_submitter", "report_digest": "", "flags": []},
    )
    client.post("/process/ask", json={"run_id": "r1", "target": "report", "question": "q"})
    assert calls[0]["report"] is None and calls[0]["submission"] == SUB


def test_review_maps_typed_names_to_plant_ids(monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start_input", lambda flow, input: seen.setdefault("input", input) and "r9")
    typed = {
        "submission_id": "SUB-000001", "product": "CX-200", "submitter": "EMP-012",
        "chemicals": [{"id": "Toluene", "quantity": 120, "unit": "kg"},
                      {"id": "Acrylic resin AR7", "quantity": 300, "unit": "kg"},
                      {"id": "Unobtainium", "quantity": 1, "unit": "kg"}],
        "steps": [{"n": 1, "action": "mix", "equipment": "reactor R-101",
                   "chemicals": ["Toluene", "Acrylic resin AR7"], "temp_c": 45, "pressure_bar": 1}],
        "notes": "",
    }  # fmt: skip
    resp = client.post("/process/review", json={"submission": typed})
    assert resp.status_code == 200 and resp.json()["unknown_ids"] == ["unobtainium"]
    sub = seen["input"]["submission"]
    assert [c["id"] for c in sub["chemicals"]] == ["toluene", "acrylic_resin_ar7", "unobtainium"]
    assert sub["steps"][0]["equipment"] == "R-101"
    assert sub["steps"][0]["chemicals"] == ["toluene", "acrylic_resin_ar7"]
