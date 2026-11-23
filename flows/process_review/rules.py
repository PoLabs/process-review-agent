"""The deterministic hazard floor for process_review.

Why this exists: a model reading safety, maintenance and QC passages can miss a hazard or
soften it ("probably fine at 40 C"). The rules that matter most are machine-readable rows inside
the current documents (incompatible pairs, chemical limits, equipment ratings and service
status, required controls), so code checks the submission against them directly. No model is
involved, and the same inputs always give the same flags in the same order.

Every flag built here is `source="rule"` and `locked=True`. verify and report_guard must keep
locked flags as they are: no model output may remove one or lower its severity. Each flag cites
the row's own sentence, so a supervisor sees the document line behind it.

Text discipline: titles and details are fixed templates. A number in them is either a value
from the submission or the row's limit (which its quote states). Nothing is converted or
derived, and step numbers appear only in `subjects` ("step:2"), never in the prose.
"""

from __future__ import annotations

from flows.process_review.state import SEVERITY_RANK, Flag, RuleRow, Severity, Submission


def _n(x: float) -> str:
    """A number as written on a form or in a quote: 40.0 -> "40", 12.5 -> "12.5"."""
    return f"{x:g}"


def _flag(
    lane: str,
    kind: str,
    severity: Severity,
    subjects: list[str],
    title: str,
    detail: str,
    row: RuleRow,
    *extra_citations,
) -> Flag:
    return Flag(
        lane=lane,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        severity=severity,
        subjects=subjects,
        title=title,
        detail=detail,
        citations=[row.citation, *extra_citations],
        source="rule",
        locked=True,
    )


def _incompatible(sub: Submission, row: RuleRow) -> list[Flag]:
    ids = {c.id for c in sub.chemicals}
    if not row.a or not row.b or row.a not in ids or row.b not in ids:
        return []
    a, b = sorted([row.a, row.b])
    detail = f"{a} and {b} are both on this form."
    if row.hazard:
        detail += f" Hazard: {row.hazard}."
    return [
        _flag(
            "safety",
            "incompatible",
            row.severity,
            [a, b],
            f"{a} and {b} must not be combined",
            detail,
            row,
        )
    ]


def _chem_limit(sub: Submission, row: RuleRow) -> list[Flag]:
    chem = row.chemical
    if not chem:
        return []
    out: list[Flag] = []
    if row.max_temp_c is not None:
        for step in sub.steps:
            if chem in step.chemicals and step.temp_c is not None and step.temp_c > row.max_temp_c:
                out.append(
                    _flag(
                        "safety",
                        "over_limit",
                        row.severity,
                        [chem, f"step:{step.n}"],
                        f"{chem} is run at {_n(step.temp_c)} C, over its {_n(row.max_temp_c)} C limit",
                        f"The form has {chem} at {_n(step.temp_c)} C in a step; "
                        f"the current document limit is {_n(row.max_temp_c)} C.",
                        row,
                    )
                )
    if row.max_concentration_pct is not None:
        for line in sub.chemicals:
            if (
                line.id == chem
                and line.concentration_pct is not None
                and line.concentration_pct > row.max_concentration_pct
            ):
                out.append(
                    _flag(
                        "safety",
                        "over_limit",
                        row.severity,
                        [chem],
                        f"{chem} is {_n(line.concentration_pct)} %, over its "
                        f"{_n(row.max_concentration_pct)} % limit",
                        f"The form lists {chem} at {_n(line.concentration_pct)} %; "
                        f"the current document limit is {_n(row.max_concentration_pct)} %.",
                        row,
                    )
                )
    return out


def _equipment(sub: Submission, row: RuleRow) -> list[Flag]:
    eq = row.equipment
    if not eq:
        return []
    steps = [s for s in sub.steps if s.equipment == eq]
    if not steps:
        return []
    out: list[Flag] = []
    for step in steps:
        if (
            row.max_pressure_bar is not None
            and step.pressure_bar is not None
            and step.pressure_bar > row.max_pressure_bar
        ):
            out.append(
                _flag(
                    "maintenance",
                    "over_rating",
                    "critical",
                    [eq, f"step:{step.n}"],
                    f"{eq} is run at {_n(step.pressure_bar)} bar, over its "
                    f"{_n(row.max_pressure_bar)} bar rating",
                    f"The form has {eq} at {_n(step.pressure_bar)} bar in a step; "
                    f"the current maintenance rating is {_n(row.max_pressure_bar)} bar.",
                    row,
                )
            )
        if row.max_temp_c is not None and step.temp_c is not None and step.temp_c > row.max_temp_c:
            out.append(
                _flag(
                    "maintenance",
                    "over_rating",
                    "major",
                    [eq, f"step:{step.n}"],
                    f"{eq} is run at {_n(step.temp_c)} C, over its {_n(row.max_temp_c)} C rating",
                    f"The form has {eq} at {_n(step.temp_c)} C in a step; "
                    f"the current maintenance rating is {_n(row.max_temp_c)} C.",
                    row,
                )
            )
    if row.in_service is False:
        out.append(
            _flag(
                "maintenance",
                "out_of_service",
                "major",
                [eq],
                f"{eq} is out of service but the process uses it",
                f"The current maintenance document says {eq} is not in service, "
                "and the form has steps that use it.",
                row,
            )
        )
    return out


def _requires(sub: Submission, row: RuleRow, rows: list[RuleRow]) -> list[Flag]:
    chem, control = row.chemical, row.control
    if not chem or not control or chem not in {c.id for c in sub.chemicals}:
        return []
    control_rows = [r for r in rows if r.kind == "equipment" and r.equipment == control]
    down = next((r for r in control_rows if r.in_service is False), None)
    if control_rows and down is None:
        return []  # a row exists and nothing says it is out of service
    if down is not None:
        why = f"{control} is not in service."
        extra = [down.citation]
    else:
        why = f"no current document shows {control} in service."
        extra = []
    return [
        _flag(
            "cross",
            "missing_control",
            row.severity,
            [chem, control],
            f"{chem} needs {control} in service, and it is not",
            f"The current documents require {control} in service when {chem} is used; {why}",
            row,
            *extra,
        )
    ]


def check(sub: Submission, rows: list[RuleRow]) -> list[Flag]:
    """Every rule the submission breaks, as locked flags: severity desc, then lane, kind, subjects."""
    found: list[Flag] = []
    for row in rows:
        if row.kind == "incompatible":
            found += _incompatible(sub, row)
        elif row.kind == "chem_limit":
            found += _chem_limit(sub, row)
        elif row.kind == "equipment":
            found += _equipment(sub, row)
        elif row.kind == "requires":
            found += _requires(sub, row, rows)

    # one flag per (lane, kind, subjects, title); when rows overlap keep the most severe
    best: dict[tuple, Flag] = {}
    for f in found:
        key = (f.lane, f.kind, tuple(f.subjects), f.title)
        cur = best.get(key)
        if cur is None or SEVERITY_RANK[f.severity] > SEVERITY_RANK[cur.severity]:
            best[key] = f
    return sorted(
        best.values(),
        key=lambda f: (-SEVERITY_RANK[f.severity], f.lane, f.kind, f.subjects, f.title),
    )
