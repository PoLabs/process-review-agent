"""Lint for the process_qa eval dataset: schema, vocabulary, fixtures, and agreement with the documents.

Offline. Every number a chunk-cited case expects must be in the current text of the target store.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import get_args

from evals.contracts import Case
from evals.datasets.process_qa import make_fixtures
from flows.process_qa.state import RefusalReason, Status, Target
from flows.process_review.docs import DocIndex

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "datasets" / "process_qa.jsonl"
FIXTURES = ROOT / "evals" / "datasets" / "process_qa" / "fixtures"
SPEC = ROOT / "plans" / "process_qa.spec.json"

IDS = [
    "own-01", "own-02", "safety-qa-01", "maint-qa-01", "qc-qa-01", "report-qa-01", "report-qa-02",
    "redirect-01", "harm-01", "harm-02", "safe-01", "rewrite-01", "other-01", "offtopic-01",
    "offtopic-02", "stale-qa-01", "inj-qa-01", "fp-01",
]  # fmt: skip
EXPECTED_COUNTS = {
    "in_scope": 7,
    "wrong_target": 1,
    "never_event": 8,
    "adversarial": 1,
    "false_positive_guard": 1,
}
EXPECT_KEYS = {
    "status", "refusal_reason", "suggested_target", "contains_any", "cites_kind", "ehs_notice",
    "never_cite", "no_recipe", "no_safe_language",
}  # fmt: skip
FIXTURE_NAMES = {"clean-01", "hazard-01", "stale-01"}
STORES = {"safety", "maintenance", "qc"}


def _load() -> list[Case]:
    return [Case.model_validate_json(line) for line in DATASET.read_text().splitlines() if line.strip()]


CASES = _load()
INDEX = DocIndex()


def test_ids_are_the_list_and_unique():
    assert [c.id for c in CASES] == IDS
    assert len({c.id for c in CASES}) == len(CASES)


def test_counts_per_category():
    assert dict(Counter(c.category for c in CASES)) == EXPECTED_COUNTS


def test_every_spec_never_event_case_exists():
    spec = json.loads(SPEC.read_text())
    wanted = {ne["eval_case"] for ne in spec["never_events"]}
    assert wanted and wanted <= {c.id for c in CASES}
    for case in CASES:
        if case.id in wanted:
            assert case.category == "never_event", case.id


def test_input_shape_and_fixtures():
    for case in CASES:
        assert set(case.input) == {"question", "target", "fixture"}, case.id
        assert case.input["question"].strip() and case.tags, case.id
        assert case.input["target"] in get_args(Target), case.id
        fixture = case.input["fixture"]
        if case.input["target"] in ("my_process", "report"):
            assert fixture in FIXTURE_NAMES, case.id
        else:
            assert fixture is None, case.id
        if fixture:
            data = json.loads((FIXTURES / f"{fixture}.json").read_text())
            assert {"submission", "report"} <= set(data), fixture
            assert isinstance(data["report"]["flags"], list), fixture


def test_every_fixture_file_is_used_and_stable():
    used = {c.input["fixture"] for c in CASES if c.input["fixture"]}
    assert used == FIXTURE_NAMES == {p.stem for p in FIXTURES.glob("*.json")}
    with tempfile.TemporaryDirectory() as tmp:
        for path in make_fixtures.build(Path(tmp)):
            assert path.read_text() == (FIXTURES / path.name).read_text(), path.name


def test_expectation_vocabulary():
    statuses, reasons, targets = set(get_args(Status)), set(get_args(RefusalReason)), set(get_args(Target))
    for case in CASES:
        e = case.expect
        assert set(e) <= EXPECT_KEYS and e["status"] in statuses, case.id
        assert isinstance(e["ehs_notice"], bool), case.id  # explicit on every case
        if "refusal_reason" in e:
            assert e["refusal_reason"] in reasons, case.id
        if "suggested_target" in e:
            assert e["suggested_target"] in targets, case.id
            assert e["suggested_target"] != case.input["target"], case.id
        if "cites_kind" in e:
            assert e["cites_kind"] in ("chunk", "field", "flag"), case.id
        for key in ("no_recipe", "no_safe_language"):
            assert e.get(key, True) is True, case.id
        for key in ("contains_any", "never_cite"):
            assert isinstance(e.get(key, []), list) and all(isinstance(x, str) and x for x in e.get(key, []))
        for ref in e.get("never_cite", []):
            assert "@" in ref and all(ref.split("@")), case.id


def test_expectations_match_case_kind():
    for case in CASES:
        e = case.expect
        if e["status"] == "answered":
            assert e.get("contains_any") or e.get("cites_kind"), case.id
            assert "refusal_reason" not in e and not e["ehs_notice"], case.id
        else:
            assert e["status"] == "refused" and "refusal_reason" in e, case.id
            assert "contains_any" not in e and "cites_kind" not in e, case.id
        assert e["ehs_notice"] == (e.get("refusal_reason") == "harmful_intent"), case.id
        assert ("suggested_target" in e) == (e.get("refusal_reason") == "wrong_target"), case.id
        if case.input["target"] in STORES and e["status"] == "answered":
            assert e.get("cites_kind") == "chunk", case.id


def test_never_cited_versions_are_not_current():
    for case in CASES:
        for ref in case.expect.get("never_cite", []):
            doc_id, version = ref.split("@")
            assert not INDEX.is_current(doc_id, version), (case.id, ref)


def test_expected_numbers_are_in_the_current_documents():
    for case in CASES:
        e = case.expect
        if e.get("cites_kind") != "chunk" or not e.get("contains_any"):
            continue
        text = " ".join(c.text for c in INDEX.chunks(case.input["target"]))
        assert any(token in text for token in e["contains_any"]), case.id


def test_stale_case_expects_the_amended_limit_not_the_old_one():
    case = next(c for c in CASES if c.id == "stale-qa-01")
    text = " ".join(c.text for c in INDEX.chunks("safety"))
    assert "toluene is 50" in text and "toluene is 60" not in text
    assert case.expect["contains_any"] == ["50"]
