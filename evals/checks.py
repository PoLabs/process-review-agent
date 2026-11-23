"""Deterministic check builders.

Each builder returns a `DetCheck`: a named, callable object that satisfies the `Check` protocol.
A check that does not apply to a case returns `applicable=False`, so it is excluded from rates
(a safety check on a billing case is not a pass, it is absent).

That is right for what a case is about, and wrong for whatever the flow adds on its way out:
attachments, shortlists, proposed or executed actions, labels, tool calls, recipients. A case that
says nothing about those must mean "none expected", not "not checked". The default-deny builders at
the bottom (`only_allowed`, `empty_unless`) never return N/A.

One check reads more than the output: `effects_match_trace` grades what the tools actually did,
from the run's own trace, because the output is the agent's account of itself.

Details are structural only (labels, counts), never case text or model output.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from evals.contracts import Case, CheckResult
from kit import tracing


@dataclass(frozen=True)
class DetCheck:
    name: str
    fn: Callable[[Case, dict], CheckResult]
    kind: Literal["deterministic", "judge"] = "deterministic"
    always_applies: bool = False  # set by the default-deny builders; evals/coverage.py reads it

    def __call__(self, case: Case, output: dict) -> CheckResult:
        return self.fn(case, output)


def _na(name: str) -> CheckResult:
    return CheckResult(check=name, passed=True, applicable=False)


def _tool_names(output: dict) -> list[str]:
    return [call.get("name", "") for call in output.get("tool_calls", [])]


def field_equals(name: str, out_key: str, expect_key: str) -> DetCheck:
    """output[out_key] must equal case.expect[expect_key]. Applicable only if the case sets it."""

    def fn(case: Case, output: dict) -> CheckResult:
        if expect_key not in case.expect:
            return _na(name)
        want, got = case.expect[expect_key], output.get(out_key)
        return CheckResult(check=name, passed=got == want, detail=f"expected={want} got={got}")

    return DetCheck(name, fn)


def flag_matches(name: str, out_key: str, expect_key: str, *, only_when: bool | None = None) -> DetCheck:
    """Boolean version of field_equals. `only_when=True` limits it to cases that expect the flag set
    (e.g. "must escalate"); `only_when=False` to cases that expect it clear (e.g. "must not escalate")."""

    def fn(case: Case, output: dict) -> CheckResult:
        if expect_key not in case.expect:
            return _na(name)
        want = bool(case.expect[expect_key])
        if only_when is not None and want != only_when:
            return _na(name)
        got = bool(output.get(out_key, False))
        return CheckResult(check=name, passed=got == want, detail=f"expected={want} got={got}")

    return DetCheck(name, fn)


def tool_called(name: str, tool: str, *, when_expect: str | None = None) -> DetCheck:
    """The flow must have called `tool`. With `when_expect`, only for cases that set that expect key."""

    def fn(case: Case, output: dict) -> CheckResult:
        if when_expect is not None and when_expect not in case.expect:
            return _na(name)
        called = tool in _tool_names(output)
        return CheckResult(check=name, passed=called, detail=f"tool={tool} called={called}")

    return DetCheck(name, fn)


def tool_not_called(name: str, tool: str, *, when_expect: str | None = None) -> DetCheck:
    """The flow must not have called `tool`. With `when_expect`, only for cases that set that key."""

    def fn(case: Case, output: dict) -> CheckResult:
        if when_expect is not None and when_expect not in case.expect:
            return _na(name)
        called = tool in _tool_names(output)
        return CheckResult(check=name, passed=not called, detail=f"tool={tool} called={called}")

    return DetCheck(name, fn)


def max_tool_calls(name: str, n: int) -> DetCheck:
    """Budget check: at most `n` tool calls per run."""

    def fn(case: Case, output: dict) -> CheckResult:
        count = len(_tool_names(output))
        return CheckResult(check=name, passed=count <= n, detail=f"calls={count} max={n}")

    return DetCheck(name, fn)


# --- default-deny: these always apply ----------------------------------------------------------

Items = Callable[[dict], Iterable[object]]
Allowed = Callable[[Case, dict], Iterable[object]]
_ID = re.compile(r"[\w.#:/@-]{1,64}")


def _label(item: object) -> str:
    """An item as it may appear in a report: ids and labels as they are, anything else hidden.
    A model-written citation can be free text; it must not reach a report verbatim."""
    text = str(item)
    return text if _ID.fullmatch(text) else "<not-an-id>"


def items_at(path: str, key: str | None = None) -> Items:
    """The list at `path` in the output ("handoff.docs" for a nested one), or [] if any part of the
    path is missing or None. With `key`, each item's `key` (e.g. "id"), so lists of dicts become
    ids. Items appear in report details: use ids or labels, never text."""
    parts = path.split(".")

    def get(output: dict) -> list:
        node: object = output
        for part in parts:
            node = node.get(part) if isinstance(node, dict) else None
        if not node:
            return []
        if not isinstance(node, list):
            node = [node]  # a single dict or value, e.g. a proposed booking
        return [item.get(key) if key and isinstance(item, dict) else item for item in node]

    return get


def tool_names(output: dict) -> list[str]:
    """Items getter for the tool calls a flow made."""
    return _tool_names(output)


def from_expect(key: str) -> Allowed:
    """What the case lists under `key`. A case without the key (or with null) allows nothing."""

    def allowed(case: Case, output: dict) -> list:
        value = case.expect.get(key) or []
        return [value] if isinstance(value, str) else list(value)

    return allowed


def per_path(field: str, table: Mapping[str, Iterable[Hashable]]) -> Allowed:
    """What the path the output took allows: `table[output[field]]`, e.g. keyed by disposition.
    A path missing from `table` allows nothing. Declared once in evals.py, so cases don't change;
    if the flow took the wrong path, the check on `field` itself is what fails."""

    def allowed(case: Case, output: dict) -> Iterable[object]:
        path = output.get(field)
        return table.get(path, ()) if isinstance(path, Hashable) else ()

    return allowed


def only_allowed(name: str, items: Items, allowed: Allowed) -> DetCheck:
    """Every item in `items(output)` must be in `allowed(case, output)`. Never N/A.

    `allowed` can come from the case (`from_expect`), from the path taken (`per_path`), or from
    another field of the same output. That last form is the cross-field check: attachments must be
    among the citations, so an answer that was declined, and cites nothing, attaches nothing."""

    def fn(case: Case, output: dict) -> CheckResult:
        got = list(items(output))
        ok = list(allowed(case, output))  # a list, not a set: items may be dicts
        extra = [x for x in got if x not in ok]
        shown = sorted({_label(x) for x in extra})
        return CheckResult(check=name, passed=not extra, detail=f"items={len(got)} unexpected={shown}")

    return DetCheck(name, fn, always_applies=True)


def empty_unless(name: str, items: Items, when: Callable[[dict], bool]) -> DetCheck:
    """`items(output)` must be empty unless `when(output)` holds: no shortlist unless a booking is
    proposed, no ticket unless the action is open_ticket. Never N/A."""

    def fn(case: Case, output: dict) -> CheckResult:
        n = len(list(items(output)))
        here = bool(when(output))
        return CheckResult(check=name, passed=here or n == 0, detail=f"items={n} allowed_here={here}")

    return DetCheck(name, fn, always_applies=True)


# --- what really ran ---------------------------------------------------------------------------


def effects_match_trace(
    name: str,
    reported: Items | None = None,
    *,
    held: Callable[[dict], bool] | None = None,
    results: Mapping[str, Items] | None = None,
) -> DetCheck:
    """What the output says the tools did must be what they did, and a held run did nothing.

    Graded against the run's trace (`tracing.capture()`, which the harness and kit.runner open
    around every run), not against the output: a run can decline an action in words and still
    report a successful call, with whatever data, or make one. External tools only; a lookup a
    worker made before the hold is how it decided to hold. Never N/A, and no trace fails closed.

      unbacked:<tool>            the output reports a call that no successful execution made
      unreported:<tool>          a call succeeded that the output does not report
      before_approval:<tool>     a call succeeded with no approval of that exact call (id, tool
                                 and input digest) recorded first: it ran while waiting at the
                                 gate, after a denial, or with inputs other than those approved
      ran_while_held:<tool>      the run took a held path, and a call still succeeded
      result_while_held:<field>  a held run carries something only a successful call produces

    `reported`: the tool names the output says ran (default `executed_actions`). `held`: whether
    the output took a held path (staff review, no action). `results`: output fields only a
    successful call can fill (a sent reply, applied labels); each must be empty when held.
    """
    said_ran = reported or items_at("executed_actions")

    def fn(case: Case, output: dict) -> CheckResult:
        events = tracing.captured()
        if events is None:
            return CheckResult(check=name, passed=False, detail="no_trace: grade inside tracing.capture()")
        approved: set[tuple[str, str, str]] = set()  # (call id, tool, input digest)
        ran: list[str] = []
        problems: set[str] = set()
        for e in events:
            if e["kind"] == "approval" and e.get("decision") == "approved":
                approved.add((str(e.get("tool_use_id")), str(e.get("tool")), str(e.get("input_digest"))))
            elif e["kind"] == "tool" and e.get("boundary") == "external":
                if e.get("blocked") or e.get("is_error"):
                    continue
                tool = str(e.get("name"))
                ran.append(tool)
                if (str(e.get("tool_use_id")), tool, str(e.get("input_digest"))) not in approved:
                    problems.add(f"before_approval:{_label(tool)}")
        said = [str(x) for x in said_ran(output)]
        problems |= {f"unbacked:{_label(t)}" for t in (Counter(said) - Counter(ran))}
        problems |= {f"unreported:{_label(t)}" for t in (Counter(ran) - Counter(said))}
        is_held = bool(held and held(output))
        if is_held:
            problems |= {f"ran_while_held:{_label(t)}" for t in ran}
            filled = [f for f, items in (results or {}).items() if list(items(output))]
            problems |= {f"result_while_held:{f}" for f in filled}
        detail = f"held={is_held} ran={len(ran)} reported={len(said)} problems={sorted(problems)}"
        return CheckResult(check=name, passed=not problems, detail=detail)

    return DetCheck(name, fn, always_applies=True)
