"""The process_qa compliance demo passes on the real graph, and goes red when a control breaks."""

from __future__ import annotations

from flows.process_qa import compliance
from kit import audit_store, tracing


def test_main_exits_zero_and_every_control_passes(tmp_path, capsys):
    out = tmp_path / "report.md"
    assert compliance.main(["--out", str(out)]) == 0
    rows = [ln for ln in out.read_text().splitlines() if ln.startswith("| ") and "---" not in ln][1:]
    assert len(rows) == 9
    assert all("| PASS |" in r for r in rows)
    assert "wrote" in capsys.readouterr().out


def test_demo_goes_red_when_the_question_is_logged(monkeypatch):
    """The regression the control exists for: someone logs the question text, and the filters are off."""
    original = tracing.record

    def leaky(kind, **fields):
        if kind == "intake":
            fields["note"] = compliance.NOTES + " " + compliance.QUESTIONS[0][2]
        original(kind, **fields)

    monkeypatch.setattr(tracing, "record", leaky)
    monkeypatch.setattr(tracing, "sanitize", lambda event, mode=None: dict(event))
    monkeypatch.setattr(audit_store, "_has_identifier", lambda fields: [])
    failed = {r["control"] for r in compliance.run_demo() if not r["passed"]}
    assert "No identifiers at rest" in failed
