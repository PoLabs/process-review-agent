"""process_review hazard floor: each rule fires, boundaries don't, output is stable and locked."""

import re

from flows.process_review.rules import check
from flows.process_review.state import ChemicalLine, Citation, RuleRow, Step, Submission


def cite(quote: str, doc: str = "DOC") -> Citation:
    return Citation(chunk_id=f"{doc}@1#0", doc_id=doc, version="1", quote=quote)


def sub(chems=(), steps=()) -> Submission:
    return Submission(
        submission_id="SUB-0001",
        product="CX-200",
        submitter="EMP-001",
        chemicals=[c if isinstance(c, ChemicalLine) else ChemicalLine(id=c) for c in chems],
        steps=list(steps),
    )


def step(n=1, eq="R-101", chems=(), temp=None, bar=None) -> Step:
    return Step(n=n, action="heat", equipment=eq, chemicals=list(chems), temp_c=temp, pressure_bar=bar)


INC = RuleRow(
    kind="incompatible",
    severity="critical",
    a="hydrogen_peroxide",
    b="acetone",
    hazard="forms explosive organic peroxides",
    citation=cite("Never combine acetone with hydrogen peroxide."),
)
LIM = RuleRow(
    kind="chem_limit",
    severity="major",
    chemical="acetone",
    max_temp_c=35,
    max_concentration_pct=99.5,
    citation=cite("Acetone must not exceed 35 C or 99.5 %."),
)
EQ = RuleRow(
    kind="equipment",
    severity="critical",
    equipment="R-101",
    max_temp_c=150,
    max_pressure_bar=6,
    in_service=True,
    citation=cite("R-101 is rated 6 bar and 150 C."),
)
SC1_DOWN = RuleRow(
    kind="equipment",
    severity="major",
    equipment="SC-1",
    in_service=False,
    citation=cite("SC-1 is locked out."),
)
SC1_UP = SC1_DOWN.model_copy(update={"in_service": True})
REQ = RuleRow(
    kind="requires",
    severity="critical",
    chemical="hydrochloric_acid",
    control="SC-1",
    citation=cite("Hydrochloric acid needs SC-1 in service."),
)


def kinds(flags):
    return [(f.lane, f.kind, f.severity, tuple(f.subjects)) for f in flags]


def test_incompatible_fires_with_sorted_subjects():
    flags = check(sub(["acetone", "hydrogen_peroxide"]), [INC])
    assert kinds(flags) == [("safety", "incompatible", "critical", ("acetone", "hydrogen_peroxide"))]
    assert "acetone and hydrogen_peroxide must not be combined" == flags[0].title
    assert INC.hazard in flags[0].detail
    assert flags[0].citations == [INC.citation]


def test_incompatible_needs_both():
    assert check(sub(["acetone"]), [INC]) == []


def test_chem_limit_step_temp_and_concentration():
    s = sub([ChemicalLine(id="acetone", concentration_pct=99.9)], [step(2, chems=["acetone"], temp=40)])
    flags = check(s, [LIM])
    assert sorted(kinds(flags)) == [
        ("safety", "over_limit", "major", ("acetone",)),
        ("safety", "over_limit", "major", ("acetone", "step:2")),
    ]


def test_chem_limit_boundary_and_none_do_not_fire():
    s = sub(
        [ChemicalLine(id="acetone", concentration_pct=99.5)],
        [step(1, chems=["acetone"], temp=35), step(2, chems=["acetone"], temp=None)],
    )
    assert check(s, [LIM]) == []


def test_chem_limit_ignores_steps_without_the_chemical():
    assert check(sub(["acetone"], [step(1, chems=["toluene"], temp=80)]), [LIM]) == []


def test_equipment_over_rating_pressure_critical_temp_major():
    flags = check(sub([], [step(3, temp=160, bar=7)]), [EQ])
    assert sorted(kinds(flags)) == [
        ("maintenance", "over_rating", "critical", ("R-101", "step:3")),
        ("maintenance", "over_rating", "major", ("R-101", "step:3")),
    ]
    assert flags[0].severity == "critical"


def test_equipment_boundary_none_and_unused_do_not_fire():
    assert check(sub([], [step(1, temp=150, bar=6), step(2, temp=None, bar=None)]), [EQ]) == []
    assert check(sub([], [step(1, eq="R-102", temp=500, bar=50)]), [EQ]) == []


def test_equipment_with_none_limits_does_not_fire():
    row = EQ.model_copy(update={"max_temp_c": None, "max_pressure_bar": None})
    assert check(sub([], [step(1, temp=500, bar=50)]), [row]) == []


def test_out_of_service_once_per_equipment():
    s = sub([], [step(1, eq="SC-1"), step(2, eq="SC-1"), step(3, eq="SC-1")])
    flags = check(s, [SC1_DOWN])
    assert kinds(flags) == [("maintenance", "out_of_service", "major", ("SC-1",))]
    assert check(sub([], [step(1, eq="R-101")]), [SC1_DOWN]) == []


def test_requires_fires_when_control_out_of_service():
    flags = check(sub(["hydrochloric_acid"]), [REQ, SC1_DOWN])
    assert kinds(flags) == [("cross", "missing_control", "critical", ("hydrochloric_acid", "SC-1"))]
    assert flags[0].citations == [REQ.citation, SC1_DOWN.citation]


def test_requires_fires_when_control_has_no_row():
    flags = check(sub(["hydrochloric_acid"]), [REQ])
    assert kinds(flags) == [("cross", "missing_control", "critical", ("hydrochloric_acid", "SC-1"))]
    assert flags[0].citations == [REQ.citation]


def test_requires_silent_when_in_service_or_chemical_absent():
    assert check(sub(["hydrochloric_acid"]), [REQ, SC1_UP]) == []
    assert check(sub(["acetone"]), [REQ, SC1_DOWN]) == []


def test_deterministic_sorted_and_deduped():
    s = sub(
        ["hydrogen_peroxide", "acetone", "hydrochloric_acid"],
        [step(1, chems=["acetone"], temp=40, bar=7), step(2, eq="SC-1")],
    )
    rows = [LIM, INC, REQ, SC1_DOWN, EQ]
    a = check(s, rows)
    b = check(s, list(reversed(rows)) + [INC, LIM])  # reordered, with duplicate rows
    assert a == b
    ranks = {"critical": 3, "major": 2}
    sev = [ranks[f.severity] for f in a]
    assert sev == sorted(sev, reverse=True)
    keys = [(f.lane, f.kind, tuple(f.subjects), f.title) for f in a]
    assert len(keys) == len(set(keys))


def test_all_flags_locked_rule_sourced_and_cited():
    s = sub(
        ["hydrogen_peroxide", "acetone", "hydrochloric_acid"], [step(1, chems=["acetone"], temp=40, bar=7)]
    )
    flags = check(s, [LIM, INC, REQ, EQ])
    assert flags
    for f in flags:
        assert f.locked and f.source == "rule"
        assert f.citations


def test_numbers_in_text_come_from_submission_or_quote():
    s = sub(
        [
            ChemicalLine(id="acetone", concentration_pct=99.9, quantity=12),
            "hydrogen_peroxide",
            "hydrochloric_acid",
        ],
        [step(2, chems=["acetone"], temp=40.5, bar=7), step(4, eq="SC-1")],
    )
    flags = check(s, [LIM, INC, REQ, SC1_DOWN, EQ])
    assert len(flags) >= 5
    allowed_sub = {"99.9", "12", "40.5", "7", "2", "4"}
    ids = ["hydrogen_peroxide", "hydrochloric_acid", "acetone", "R-101", "SC-1"]
    for f in flags:
        text = f.title + " " + f.detail
        for i in ids:
            text = text.replace(i, "")
        quotes = " ".join(c.quote for c in f.citations)
        allowed_quote = set(re.findall(r"\d+(?:\.\d+)?", quotes))
        for num in re.findall(r"\d+(?:\.\d+)?", text):
            assert num in allowed_quote or num in allowed_sub, (num, f.title)
        # step numbers never appear in prose, only in subjects
        assert "step:" not in text
