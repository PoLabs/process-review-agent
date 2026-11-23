"""process_review document world: parsing, currency, amendments, rules, coverage (offline)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from flows.process_review.docs import DATA, DocIndex, load_all

WORLD = Path(__file__).parent.parent / "flows" / "process_review" / "WORLD.md"


@pytest.fixture(scope="module")
def idx() -> DocIndex:
    return DocIndex()


def _versions(idx: DocIndex, doc_id: str) -> list[str]:
    return [v for d, v, _ in idx.all_versions() if d == doc_id]


def test_every_file_parses():
    docs = load_all(DATA)
    assert len(docs) >= 30
    assert all(d.sections for d in docs)


def test_every_world_doc_version_exists(idx):
    have = {(d, v) for d, v, _ in idx.all_versions()}
    expected = {
        ("SDS-ACE", "3"),
        ("SDS-H2O2", "2"),
        ("SDS-TOL", "2"),
        ("SDS-ETH", "1"),
        ("SDS-IPA", "1"),
        ("SDS-HNO3", "1"),
        ("SDS-NAOCL", "3"),
        ("SDS-NAOCL", "4"),
        ("SDS-NH3", "1"),
        ("SDS-HCL", "2"),
        ("SDS-NAOH", "1"),
        ("SDS-AR7", "1"),
        ("SAF-INC-001", "C"),
        ("SAF-INC-001", "D"),
        ("SOP-SAF-012", "B"),
        ("SOP-SAF-012", "C"),
        ("SOP-SAF-012-A1", "1"),
        ("SOP-SAF-015", "A"),
        ("SOP-SAF-015", "B"),
        ("SOP-SAF-020", "1"),
        ("MM-R101", "5"),
        ("MM-R102", "2"),
        ("MM-R102", "3"),
        ("MM-M7", "2"),
        ("MM-SC1", "2"),
        ("MM-R2", "4"),
        ("QC-CX200", "4"),
        ("QC-SAN12", "2"),
        ("QC-RX40", "1"),
        ("QC-CL5", "1"),
        ("QC-PK3", "1"),
        ("QC-NX1", "1"),
        ("QC-ET9", "1"),
    }
    assert expected <= have
    # and WORLD.md names no document id the data lacks
    ids = set(re.findall(r"\b(?:SDS|SOP-SAF|SAF-INC|MM|QC)-[A-Za-z0-9]+\b", WORLD.read_text()))
    assert ids <= {d for d, _ in have} | {"SOP-SAF-012-A1"}


def test_current_versions(idx):
    cur = {d.doc_id: d.version for s in ("safety", "maintenance", "qc") for d in idx.current(s)}
    assert cur["SOP-SAF-012"] == "C+A1"
    assert cur["SAF-INC-001"] == "D"
    assert cur["MM-R102"] == "3"
    assert cur["SDS-NAOCL"] == "4"
    assert cur["SOP-SAF-015"] == "A"
    assert "MM-R2" not in cur
    assert "SOP-SAF-012-A1" not in cur


def test_no_chunk_from_a_non_current_version(idx):
    not_current = {(d, v) for d, v, st in idx.all_versions() if st != "approved"}
    for store in ("safety", "maintenance", "qc"):
        for c in idx.chunks(store):
            assert (c.meta["doc_id"], c.meta["version"]) not in not_current
            assert idx.chunk(c.id) is c
            assert c.id.startswith(f"{c.meta['doc_id']}@{c.meta['version']}#")
    assert not any("MM-R102@2" in c.id or "SOP-SAF-012@B" in c.id for c in idx.chunks("safety"))
    assert idx.chunk("MM-R2@4#1") is None


def test_is_current(idx):
    assert not idx.is_current("SOP-SAF-012", "B")
    assert not idx.is_current("SOP-SAF-012", "C")
    assert idx.is_current("SOP-SAF-012", "C+A1")
    assert not idx.is_current("MM-R2", "4")


def test_limits_use_current_numbers(idx):
    lim = {r.chemical: r for r in idx.rules() if r.kind == "chem_limit"}
    assert lim["toluene"].max_temp_c == 50
    assert lim["acetone"].max_temp_c == 35
    assert lim["toluene"].citation.version == "C+A1"
    assert lim["acetone"].citation.version == "C+A1"
    assert lim["hydrogen_peroxide"].max_concentration_pct == 35
    assert all(r.severity == "major" for r in lim.values())


def test_rules_quotes_are_in_their_chunks(idx):
    rules = idx.rules()
    assert rules
    for r in rules:
        chunk = idx.chunk(r.citation.chunk_id)
        assert chunk is not None and r.citation.quote in chunk.text
        assert idx.is_current(r.citation.doc_id, r.citation.version)
        for n in (r.max_temp_c, r.max_pressure_bar, r.max_concentration_pct):
            if n is not None:
                assert f"{n:g}" in r.citation.quote


def test_rule_rows(idx):
    rules = idx.rules()
    inc = {frozenset({r.a, r.b}) for r in rules if r.kind == "incompatible"}
    assert frozenset({"acetone", "hydrogen_peroxide"}) in inc and len(inc) == 6
    eq = {r.equipment: r for r in rules if r.kind == "equipment"}
    assert set(eq) == {"R-101", "R-102", "M-7", "SC-1"}
    assert eq["R-102"].max_pressure_bar == 3 and eq["M-7"].max_pressure_bar == 0
    assert eq["SC-1"].in_service is False
    req = [r for r in rules if r.kind == "requires"]
    assert [(r.chemical, r.control, r.severity) for r in req] == [("hydrochloric_acid", "SC-1", "critical")]


def test_coverage(idx):
    assert not idx.covers("safety", "mekp")
    assert not idx.covers("maintenance", "R-2")
    assert idx.covers("safety", "water")
    assert idx.covers("safety", "toluene")
    assert idx.covers("maintenance", "R-101")
    assert idx.covers("qc", "CX-200")


def test_naoh_sheet_states_no_temperature_limit(idx):
    text = " ".join(c.text for c in idx.chunks("safety") if c.meta["doc_id"] == "SDS-NAOH")
    assert text and "°C" not in text and "temperature" not in text.lower()


def test_before_the_amendment_takes_effect():
    early = DocIndex(today=date(2026, 8, 1))
    ver = {d.doc_id: d.version for d in early.current("safety")}
    assert ver["SOP-SAF-012"] == "C"
    tol = next(r for r in early.rules() if r.kind == "chem_limit" and r.chemical == "toluene")
    assert tol.max_temp_c == 60
    assert not early.covers("maintenance", "SC-1")  # MM-SC1 is not in effect until 2026-10-20
