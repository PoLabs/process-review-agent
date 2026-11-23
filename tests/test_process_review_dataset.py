"""Lint for the process_review eval dataset: schema, vocabulary, and agreement with WORLD.md.

Offline. The 17 cases are the table in flows/process_review/WORLD.md.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import get_args

from evals.contracts import Case
from flows.process_review.state import (
    ApproverRole,
    FlagKind,
    Recommendation,
    Severity,
    Status,
    Submission,
)

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "datasets" / "process_review.jsonl"
SPEC = ROOT / "plans" / "process_review.spec.json"

IDS = [
    "clean-01", "clean-02", "hazard-01", "limit-01", "maint-01", "qc-01", "cross-01", "cross-02",
    "approve-01", "stale-01", "amend-01", "ground-01", "gap-01", "ehs-01", "stale-02", "inj-01",
    "incomplete-01",
]  # fmt: skip
EXPECTED_COUNTS = {
    "clean": 2,
    "never_event": 8,
    "safety": 1,
    "maintenance": 1,
    "qc": 1,
    "cross": 2,
    "adversarial": 1,
    "incomplete": 1,
}
CHEMICALS = {
    "acetone", "hydrogen_peroxide", "toluene", "ethanol", "isopropanol", "nitric_acid",
    "sodium_hypochlorite", "ammonia", "hydrochloric_acid", "sodium_hydroxide", "acrylic_resin_ar7",
    "water", "mekp",
}  # fmt: skip
EQUIPMENT = {"R-101", "R-102", "M-7", "SC-1", "R-2"}
PRODUCTS = {"CX-200", "SAN-12", "RX-40", "CL-5", "PK-3", "NX-1", "ET-9"}
EXPECT_KEYS = {
    "flags",
    "clean",
    "recommendation_in",
    "required_approvers",
    "status",
    "never_cite",
    "injection",
    "returned",
    "ehs_asked",
}
FLAG_KEYS = {"kinds", "subjects_any", "min_severity", "doc_id", "version_has"}
INPUT_KEYS = {"submission", "sim"}


def _load() -> list[Case]:
    return [Case.model_validate_json(line) for line in DATASET.read_text().splitlines() if line.strip()]


CASES = _load()


def _ids_in(case: Case) -> set[str]:
    sub = case.input["submission"]
    out = {sub["product"]}
    out |= {c["id"] for c in sub["chemicals"]}
    for step in sub["steps"]:
        out.add(step["equipment"])
        out |= set(step["chemicals"])
    return out


def test_ids_are_the_seventeen_and_unique():
    assert [c.id for c in CASES] == IDS
    assert len({c.id for c in CASES}) == len(CASES) == 17


def test_counts_per_category():
    assert dict(Counter(c.category for c in CASES)) == EXPECTED_COUNTS


def test_input_shape():
    submission_ids = []
    for case in CASES:
        assert set(case.input) <= INPUT_KEYS and "submission" in case.input, case.id
        assert ("sim" in case.input) == (case.id == "ehs-01"), case.id
        if "sim" in case.input:
            assert case.input["sim"] == "approve_all", case.id
        submission_ids.append(case.input["submission"]["submission_id"])
        assert case.tags and all(isinstance(t, str) for t in case.tags), case.id
    assert len(set(submission_ids)) == len(submission_ids)


def test_submissions_validate_and_units_are_present():
    for case in CASES:
        sub = Submission.model_validate(case.input["submission"])
        assert sub.submitter.startswith("EMP-"), case.id
        assert [s.n for s in sub.steps] == list(range(1, len(sub.steps) + 1)), case.id
        for step in sub.steps:
            assert step.action.strip(), case.id
            assert set(step.chemicals) <= {c.id for c in sub.chemicals}, case.id
        missing = [c.id for c in sub.chemicals if c.quantity is not None and c.unit is None]
        if case.id == "incomplete-01":
            assert missing == ["toluene"]
        else:
            assert not missing and all(c.unit for c in sub.chemicals), case.id


def test_notes_only_where_the_table_says():
    for case in CASES:
        notes = case.input["submission"]["notes"]
        assert bool(notes) == (case.id in {"approve-01", "inj-01"}), case.id


def test_ids_come_from_the_world():
    for case in CASES:
        sub = case.input["submission"]
        assert sub["product"] in PRODUCTS, case.id
        assert {c["id"] for c in sub["chemicals"]} <= CHEMICALS, case.id
        assert {s["equipment"] for s in sub["steps"]} <= EQUIPMENT, case.id
        for step in sub["steps"]:
            assert set(step["chemicals"]) <= CHEMICALS, case.id


def test_expectation_vocabulary():
    kinds, sevs = set(get_args(FlagKind)), set(get_args(Severity))
    statuses, recs, roles = set(get_args(Status)), set(get_args(Recommendation)), set(get_args(ApproverRole))
    for case in CASES:
        expect = case.expect
        assert expect and set(expect) <= EXPECT_KEYS, case.id
        assert set(expect.get("recommendation_in", [])) <= recs, case.id
        if "status" in expect:
            assert expect["status"] in statuses, case.id
        if "required_approvers" in expect:
            approvers = expect["required_approvers"]
            assert approvers in (["supervisor"], ["supervisor", "ehs"]) and set(approvers) <= roles, case.id
        for ref in expect.get("never_cite", []):
            assert "@" in ref and all(ref.split("@")), case.id
        for key in ("clean", "injection", "returned", "ehs_asked"):
            assert expect.get(key, True) is True, case.id
        for flag in expect.get("flags", []):
            assert set(flag) <= FLAG_KEYS, case.id
            assert {"kinds", "subjects_any", "min_severity"} <= set(flag), case.id
            assert flag["kinds"] and set(flag["kinds"]) <= kinds, case.id
            assert flag["min_severity"] in sevs, case.id
            for key in ("doc_id", "version_has"):
                assert isinstance(flag.get(key, ""), str), case.id


def test_subjects_appear_in_the_submission():
    for case in CASES:
        present = _ids_in(case)
        for flag in case.expect.get("flags", []):
            subjects = set(flag["subjects_any"])
            assert subjects & present, case.id
            # a control the form never names (cross-02: the scrubber SC-1) may still be a subject
            assert subjects - present <= EQUIPMENT, case.id


def test_expectations_match_case_kind():
    by_id = {c.id: c for c in CASES}
    for case in CASES:
        e = case.expect
        if case.category == "clean" or case.id == "ground-01":
            assert e["clean"] is True and e["recommendation_in"] == ["proceed_to_signoff"], case.id
            assert e["required_approvers"] == ["supervisor"] and "flags" not in e, case.id
        else:
            assert not e.get("clean"), case.id
        assert bool(e.get("injection")) == (case.id == "inj-01"), case.id
        assert bool(e.get("returned")) == (case.id == "incomplete-01"), case.id
        assert bool(e.get("ehs_asked")) == (case.id == "ehs-01"), case.id
    assert by_id["incomplete-01"].expect["status"] == "returned_to_submitter"
    for cid in ("hazard-01", "inj-01"):
        assert by_id[cid].expect["status"] == "not_approved"
    for cid in ("hazard-01", "cross-02", "gap-01", "ehs-01", "inj-01"):
        assert by_id[cid].expect["required_approvers"] == ["supervisor", "ehs"], cid


def test_every_spec_never_event_case_exists():
    spec = json.loads(SPEC.read_text())
    wanted = {ne["eval_case"] for ne in spec["never_events"]}
    assert wanted and wanted <= {c.id for c in CASES}
