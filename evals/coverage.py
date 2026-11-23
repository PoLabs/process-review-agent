"""Every output field is checked, or named as unchecked with the reason.

A flow's evals.py declares two things next to CHECKS:

    FIELDS = {"<output field>": ("<check>", ...) or "unchecked: <why>", ...}
    ADDS = ("<field>", ...)   # fields the flow fills on its way out: attachments, actions, ...

FIELDS has one entry per key of the flow's state.OUTCOME_KEYS. A tuple names the checks that
would fail if that field carried something wrong. A string says why nothing does: a person reads
it as text, it only feeds a field that is checked, it is a fixed template. Each ADDS field must
name at least one default-deny check (`only_allowed` / `empty_unless` in evals/checks.py), so a
case that says nothing about it means "none expected". tests/test_closed_world.py runs this over
every flow, so a new output field can't ship without a decision about how it is checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from evals.contracts import Check

UNCHECKED = "unchecked: "


def field_map_problems(
    outcome_keys: Iterable[str],
    fields: Mapping[str, Sequence[str] | str],
    checks: Iterable[Check],
    adds: Iterable[str] = (),
) -> list[str]:
    """What is missing from a flow's FIELDS / ADDS, one line each; [] when every field is covered."""
    keys, by_name = set(outcome_keys), {c.name: c for c in checks}
    problems = [
        f"output field '{k}' has no FIELDS entry: name the checks that cover it, or 'unchecked: <why>'"
        for k in sorted(keys - set(fields))
    ]
    problems += [f"FIELDS names '{k}', which is not an output field" for k in sorted(set(fields) - keys)]
    for key, value in sorted(fields.items()):
        if isinstance(value, str):
            if not value.startswith(UNCHECKED) or len(value.strip()) <= len(UNCHECKED) + 5:
                problems.append(f"'{key}': a string entry must read 'unchecked: <why>'")
        elif not value:
            problems.append(f"'{key}': no checks named; write 'unchecked: <why>' if nothing covers it")
        else:
            problems += [f"'{key}': no check named '{n}' in CHECKS" for n in value if n not in by_name]
    for key in sorted(set(adds)):
        if key not in keys:
            problems.append(f"ADDS names '{key}', which is not an output field")
            continue
        named = fields.get(key)
        named = () if isinstance(named, str) or named is None else named
        if not any(getattr(by_name.get(n), "always_applies", False) for n in named):
            problems.append(
                f"'{key}' is in ADDS but no default-deny check covers it: add only_allowed or empty_unless"
            )
    return problems
