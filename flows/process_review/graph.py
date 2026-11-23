"""Formulation / process review as a LangGraph graph. Diagram and design rules: state.py.

    run_review(submission) -> outcome dict (state.OUTCOME_KEYS)

Adapted from the PR-review chassis: deterministic facts first (form checks, hazard rules),
parallel specialist reviewers, a verifier that drops anything not grounded in a current
document, a guard on the final report, and human sign-off as the only way anything is approved.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from flows.process_review import prompts, rules
from flows.process_review.docs import DocIndex
from flows.process_review.state import (
    SEVERITY_RANK,
    STORES,
    Citation,
    Flag,
    LaneReport,
    QueryPlan,
    ReviewState,
    Submission,
    SynthesisDraft,
)
from flows.process_review.tools import Archive, build_registry
from kit import guardrails, llm, memory, tracing
from kit.boundary import Ledger
from kit.llm import Role
from kit.patterns import approval as ap
from kit.patterns.retrieve import build_retrieve_node

FLOW_VERSION = "1"
MAX_REVISIONS = 1
QUERIES_PER_LANE = 8
PASSAGES_PER_LANE = 14
RETENTION_DAYS = 2192  # 6 years, leap days included: the records policy for regulated decisions

# Where a code check or a person must say yes. `make graph` draws these as gates.
GATES = {
    "hazard_rules": "incompatible pairs, chemical limits, equipment ratings: locked flags, never removed",
    "verify": "every flag cites a current document passage it quotes exactly; uncovered items become gaps",
    "report_guard": "no approval language, grounded numbers, recommendation no softer than the flags",
    "supervisor_signoff": "the floor supervisor approves the exact report digest",
    "ehs_signoff": "a process-safety / EHS engineer also approves when any flag is critical",
}
# Why each routing edge is taken. `make graph` uses these as edge labels.
ROUTES = {
    ("intake", "hazard_rules"): "form complete",
    ("intake", "finalize"): "incomplete: returned to submitter",
    ("report_guard", "synthesis"): "fails guard: revise once",
    ("report_guard", "supervisor_signoff"): "passed, or code-built fallback report",
    ("supervisor_signoff", "ehs_signoff"): "approved + critical flag",
    ("supervisor_signoff", "finalize"): "rejected, or no critical flag",
}
# The agent's words must never grant approval. "requires approval from EHS" is fine.
APPROVAL_LANGUAGE = re.compile(
    r"\b(?:is|are|was|been|hereby|now|already)\s+approved\b|\bapproval\s+(?:is\s+)?granted\b|\bi\s+approve\b"
    r"|\bapproved\s+for\s+(?:use|production|release)\b",
    re.IGNORECASE,
)
# What an attack on a process review looks like, on top of the kit's generic screen: a claim that
# the work is already approved, or an instruction to ignore documents or suppress flags.
REVIEW_INJECTION = {
    "claims_approval": re.compile(
        r"\b(?:already|pre-?|verbally|been)\s*approved\b|\bmark\s+(?:it|this)?\s*(?:as\s+)?approved\b"
        r"|\b(?:formulation|process|this)\s+is\s+approved\b",
        re.IGNORECASE,
    ),
    "ignore_documents": re.compile(
        r"\b(?:ignore|disregard|skip|bypass)\s+(?:all\s+|the\s+|any\s+)?(?:safety\s+|qc\s+|maintenance\s+)?"
        r"(?:documents?|docs|procedures|sops?|checks?|rules|flags)\b",
        re.IGNORECASE,
    ),
    "suppress_flags": re.compile(
        r"\b(?:raise|report|add)\s+no\s+(?:flags|issues)\b|\bdo\s+not\s+flag\b", re.IGNORECASE
    ),  # fmt: skip
    "fake_role": re.compile(r"^\s*(?:system|assistant|admin)\s*:", re.IGNORECASE | re.MULTILINE),
}
# Ids and references, not values: R-101, SOP-SAF-012, MM-R102, "step 2" / "step:2", "flag 1".
_IDS = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z0-9]+)+\b|\b(?i:step|flag)[:\s]\s*\d+\b|\b[A-Z]{2,}\d+\b")
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w)")  # not inside a token like ar7 or C+A1


def _user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def numbers(text: str) -> set[float]:
    """Numbers a reader would take as a value: ids (R-101, SOP-SAF-012, step:2) are not numbers."""
    return {float(n) for n in _NUM.findall(_IDS.sub(" ", text or ""))}


def submission_numbers(sub: dict) -> set[float]:
    out: set[float] = set()

    def walk(v) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, int | float):
            out.add(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk({k: v for k, v in sub.items() if k not in ("notes", "submitter", "submission_id")})
    return out


def item_ids(sub: dict) -> dict[str, list[str]]:
    """Form items each store must cover: chemicals (safety), equipment (maintenance), product (qc)."""
    chems = [c["id"] for c in sub["chemicals"]]
    equipment = list(dict.fromkeys(s["equipment"] for s in sub["steps"]))
    return {"safety": chems, "maintenance": equipment, "qc": [sub["product"]]}


def _valid_subjects(sub: dict) -> set[str]:
    ids = item_ids(sub)
    return {*ids["safety"], *ids["maintenance"], *ids["qc"], *(f"step:{s['n']}" for s in sub["steps"])}


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _sorted(flags: list[dict]) -> list[dict]:
    return sorted(flags, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["lane"], f["kind"], f["subjects"]))


def incomplete_fields(raw: dict) -> list[str]:
    """Field paths that send the form back: schema errors, then what a review can't do without."""
    try:
        sub = Submission.model_validate(raw)
    except ValidationError as e:
        return sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
    missing = []
    if not sub.chemicals:
        missing.append("chemicals")
    if not sub.steps:
        missing.append("steps")
    for i, c in enumerate(sub.chemicals):
        if c.quantity is None:
            missing.append(f"chemicals.{i}.quantity")
        if c.unit is None:
            missing.append(f"chemicals.{i}.unit")
    declared = {c.id for c in sub.chemicals}
    for i, s in enumerate(sub.steps):
        if not s.equipment.strip():
            missing.append(f"steps.{i}.equipment")
        if set(s.chemicals) - declared:
            missing.append(f"steps.{i}.chemicals")
    return missing


def build_graph(index: DocIndex, archive: Archive, ledger: Ledger):
    registry = build_registry(archive)
    retrieve = {store: build_retrieve_node(index.tiers(store), mode="cascade", k=3) for store in STORES}

    def _grounding(
        f: dict, shown: dict[str, dict], sub: dict, now: DocIndex
    ) -> tuple[list[dict], str | None]:
        """Citations rebuilt from the chunk (doc and version come from the index, not the model),
        or the reason the flag can't be verified."""
        if not f["citations"]:
            return [], "no_citation"
        cites = []
        for c in f["citations"]:
            p = shown.get(c["chunk_id"])
            if p is None:
                return [], "chunk_not_shown"
            if not c["quote"].strip() or _norm(c["quote"]) not in _norm(p["text"]):
                return [], "quote_not_in_chunk"
            if not now.is_current(p["doc_id"], p["version"]):
                return [], "doc_not_current"
            cites.append(Citation(chunk_id=p["chunk_id"], doc_id=p["doc_id"], version=p["version"],
                                  quote=c["quote"].strip()).model_dump())  # fmt: skip
        allowed = submission_numbers(sub) | {n for c in cites for n in numbers(c["quote"])}
        if numbers(f"{f['title']} {f['detail']}") - allowed:
            return [], "ungrounded_number"
        return cites, None

    def _verified(lane: str, model_flags: list[dict], shown: dict[str, dict], sub: dict, now: DocIndex):
        valid = _valid_subjects(sub)
        kept, dropped = [], []
        for f in model_flags:
            subjects = [s for s in f["subjects"] if s in valid] or [sub["product"]]
            cites, reason = _grounding(f, shown, sub, now)
            if reason is None:
                kept.append(Flag(lane=lane, kind=f["kind"], severity=f["severity"], subjects=subjects,
                                 title=f["title"], detail=f["detail"], citations=cites,
                                 source="model").model_dump())  # fmt: skip
                continue
            dropped.append({"lane": lane, "reason": reason})
            # A concern that can't be checked is still a concern: it stays, in code's words, at its
            # severity, without the claim that failed. Never silence.
            kept.append(Flag(lane=lane, kind="unverified", severity=f["severity"], subjects=subjects,
                             title=f"Unverified {lane} concern about {', '.join(subjects)}",
                             detail="A reviewer raised this, but its citation or numbers could not be "
                                    "checked against a current document. Check this area by hand.",
                             source="verify").model_dump())  # fmt: skip
        return kept, dropped

    # --- deterministic facts ----------------------------------------------------------------

    def intake(state: ReviewState) -> dict:
        raw = state["submission"]
        who = memory.pseudonym(str(raw.get("submitter", "")))
        sha = archive.put({**raw, "submitter": who})
        missing = incomplete_fields(raw)
        notes = str(raw.get("notes", ""))
        screen = guardrails.screen_injection(notes)
        signals = sorted({*screen.signals, *(n for n, rx in REVIEW_INJECTION.items() if rx.search(notes))})
        flags = []
        if signals and not missing:
            flags.append(Flag(lane="intake", kind="injection", severity="major", subjects=[raw["product"]],
                              title="The submitter's notes contain instructions addressed to the reviewer",
                              detail="The notes were left out of the review. Read them yourself before "
                                     "signing off.", source="intake").model_dump())  # fmt: skip
        tracing.record("intake", node="intake", complete=not missing, n_missing=len(missing),
                       injection=bool(signals), signals=signals,
                       evidence=[tracing.evidence("input", "submission", raw)])  # fmt: skip
        return {
            "submission_digest": sha,
            "submitter_pseudonym": who,
            "incomplete": missing,
            "injection_flagged": bool(signals),
            "intake_flags": flags,
        }

    def hazard_rules(state: ReviewState) -> dict:
        locked = [
            f.model_dump() for f in rules.check(Submission.model_validate(state["submission"]), index.rules())
        ]
        tracing.record("hazard_rules", node="hazard_rules", n=len(locked),
                       kinds=sorted({f["kind"] for f in locked}),
                       evidence=[tracing.evidence("rule", c["chunk_id"]) for f in locked
                                 for c in f["citations"]])  # fmt: skip
        return {"locked_flags": locked}

    def plan_queries(state: ReviewState) -> dict:
        sub = state["submission"]
        plan = llm.structured(
            Role.CLASSIFIER,
            system=prompts.PLAN_SYSTEM,
            messages=_user(prompts.wrap_submission(sub, include_notes=not state.get("injection_flagged"))),
            schema=QueryPlan,
            node="plan_queries",
        ).model_dump()
        # The model adds queries; code guarantees one per form item, so no lane can miss an item.
        ids = item_ids(sub)
        base = {
            "safety": [f"{c.replace('_', ' ')} hazards limits" for c in ids["safety"]],
            "maintenance": [f"{e} rating service status" for e in ids["maintenance"]],
            "qc": [f"{sub['product']} specification"],
        }
        queries = {s: list(dict.fromkeys(base[s] + plan[s]))[:QUERIES_PER_LANE] for s in STORES}
        return {"queries": queries}

    def _search(store: str):
        def search(state: ReviewState) -> dict:
            sub = state["submission"]
            hits: dict[str, dict] = {}
            for q in state["queries"][store]:
                for h in retrieve[store]({"query": q})["retrieval"]["hits"]:
                    hits.setdefault(h["chunk_id"], h)
            # Small, curated stores: every current passage about a form item is shown as well, so a
            # missed search can't hide the limit that applies. Retrieval adds the rest.
            items = set(item_ids(sub)[store])
            for c in index.chunks(store):
                if items & set(c.meta.get("covers", [])):
                    hits.setdefault(c.id, {"chunk_id": c.id, "text": c.text, "meta": c.meta})
            passages = [
                {"chunk_id": cid, "doc_id": h["meta"]["doc_id"], "version": h["meta"]["version"],
                 "heading": h["meta"].get("heading", ""), "text": h["text"]}
                for cid, h in list(hits.items())[:PASSAGES_PER_LANE]
            ]  # fmt: skip
            return {"passages": {store: passages}}

        search.__name__ = f"search_{store}"
        return search

    def _review(store: str):
        def review(state: ReviewState) -> dict:
            sub = state["submission"]
            already = [{k: f[k] for k in ("kind", "severity", "subjects", "title")}
                       for f in state.get("locked_flags", []) if f["lane"] == store]  # fmt: skip
            content = (
                f"{prompts.wrap_submission(sub, include_notes=not state.get('injection_flagged'))}\n\n"
                f"{prompts.wrap_passages(state['passages'][store])}\n\n"
                f"Already raised by the plant's rule checks (do not repeat): {json.dumps(already)}"
            )
            try:
                report = llm.structured(Role.WORKER, system=prompts.LANE_SYSTEMS[store],
                                        messages=_user(content), schema=LaneReport,
                                        node=f"{store}_review").model_dump()  # fmt: skip
            except Exception as e:  # a lane that fails is a flag, never a silent pass
                tracing.record("lane_failed", node=f"{store}_review", error=type(e).__name__)
                report = {"summary": "", "flags": [], "failed": True}
            return {"lane_reports": {store: report}}

        review.__name__ = f"{store}_review"
        return review

    def verify(state: ReviewState) -> dict:
        sub = state["submission"]
        locked = [dict(f) for f in state.get("locked_flags", [])]
        flags, dropped = [], []
        now = index.reread()  # status as it is at report time, not at the start of the run
        for store in STORES:
            report = state["lane_reports"][store]
            if report.get("failed"):
                flags.append(Flag(lane=store, kind="unverified", severity="major", subjects=[sub["product"]],
                                  title=f"The {store} review did not complete",
                                  detail="Review this area by hand before signing off.",
                                  source="verify").model_dump())  # fmt: skip
                continue
            shown = {p["chunk_id"]: p for p in state["passages"][store]}
            kept, lost = _verified(store, report["flags"], shown, sub, now)
            dropped += lost
            for f in kept:
                # The same issue as a rule flag: the rule flag stays (locked), at the higher severity.
                twin = next((lf for lf in locked if lf["kind"] == f["kind"]
                             and set(lf["subjects"]) & set(f["subjects"])), None)  # fmt: skip
                if twin is None:
                    flags.append(f)
                elif SEVERITY_RANK[f["severity"]] > SEVERITY_RANK[twin["severity"]]:
                    twin["severity"] = f["severity"]
        gaps = []
        severity = {"safety": "critical", "maintenance": "major", "qc": "major"}
        what = {"safety": "safety data", "maintenance": "maintenance manual", "qc": "QC specification"}
        for store, items in item_ids(sub).items():
            for item in items:
                if not index.covers(store, item):
                    gaps.append(Flag(lane=store, kind="coverage_gap", severity=severity[store],
                                     subjects=[item],
                                     title=f"No current {what[store]} covers {item}",
                                     detail="Nothing approved and in effect covers this item, so it "
                                            "could not "
                                            "be checked. Treat it as unreviewed.",
                                     source="verify").model_dump())  # fmt: skip
        all_flags = _sorted(state.get("intake_flags", []) + locked + flags + gaps)
        tracing.record("verify", node="verify", kept=len(all_flags), gaps=len(gaps),
                       dropped=[d["reason"] for d in dropped])  # fmt: skip
        return {"flags": all_flags, "dropped": dropped}

    # --- synthesis, guard -------------------------------------------------------------------

    def synthesis(state: ReviewState) -> dict:
        sub = state["submission"]
        shown = {p["chunk_id"]: p for store in STORES for p in state["passages"][store]}
        flags = [{k: f[k] for k in ("lane", "kind", "severity", "subjects", "title")}
                 | {"quotes": [c["quote"] for c in f["citations"]]} for f in state["flags"]]  # fmt: skip
        lanes = {s: state["lane_reports"][s].get("summary", "") for s in STORES}
        parts = [
            prompts.wrap_submission(sub, include_notes=not state.get("injection_flagged")),
            f"Verified flags:\n{json.dumps(flags)}",
            f"Lane summaries:\n{json.dumps(lanes)}",
            prompts.wrap_passages(list(shown.values())),
        ]
        guard = state.get("guard") or {}
        if guard.get("violations"):
            parts.append(f"Your previous report was rejected for: {guard['violations']}. Fix exactly those.")
        try:
            d = llm.structured(Role.SUPERVISOR, system=prompts.SYNTHESIS_SYSTEM,
                               messages=_user("\n\n".join(parts)), schema=SynthesisDraft,
                               node="synthesis").model_dump()  # fmt: skip
        except Exception as e:
            tracing.record("synthesis_failed", node="synthesis", error=type(e).__name__)
            return {"synthesis": {"failed": True}}
        cross, lost = _verified(
            "cross", [{**f, "kind": "cross_conflict"} for f in d["cross_flags"]], shown, sub, index.reread()
        )
        return {"synthesis": {**d, "cross_flags": cross, "cross_dropped": len(lost)}}

    def _fallback(flags: list[dict]) -> dict:
        top = flags[0] if flags else None
        crit = sum(f["severity"] == "critical" for f in flags)
        major = sum(f["severity"] == "major" for f in flags)
        rec = "reject" if crit else "revise" if major else "proceed_to_signoff"
        summary = ("The automated summary could not be produced; the flags below are complete. "
                   + (f"Most severe: {top['title']}." if top else "No issues were found."))  # fmt: skip
        return {"summary": summary, "recommendation": rec,
                "rationale": "Set by rule from the most severe flag."}  # fmt: skip

    def report_guard(state: ReviewState) -> dict:
        sub, s = state["submission"], state.get("synthesis") or {}
        flags = _sorted(state["flags"] + s.get("cross_flags", []))
        allowed = submission_numbers(sub) | {
            n for f in flags for c in f["citations"] for n in numbers(c["quote"])
        }
        allowed |= {float(i) for i in range(len(flags) + 1)}  # "two critical issues" written as digits

        def problems(text: str) -> list[str]:
            return (["approval_language"] if APPROVAL_LANGUAGE.search(text) else []) + (
                ["ungrounded_number"] if numbers(text) - allowed else []
            )

        violations = []
        if s.get("failed"):
            violations.append("synthesis_failed")
        else:
            violations += problems(f"{s['summary']} {s['rationale']}")
            if s.get("cross_dropped"):
                violations.append("cross_flag_unverified")
        # Lane summaries are checked one by one. Synthesis can't rewrite them, so a failing one is
        # withheld (its flags still stand) rather than sending synthesis round again.
        lanes, withheld = {}, []
        for st in STORES:
            text = state["lane_reports"][st].get("summary", "")
            if problems(text):
                withheld.append(f"lane_withheld:{st}")
                text = "Withheld: this summary did not pass the report check. The flags below still apply."
            lanes[st] = text
        revisions = state.get("revisions", 0)
        if violations and revisions < MAX_REVISIONS:
            tracing.record("guard", node="report_guard", outcome="revise", violations=violations)
            return {
                "guard": {"passed": False, "violations": violations, "revise": True},
                "revisions": revisions + 1,
            }
        fallback = bool(violations)
        body = _fallback(flags) if fallback else {k: s[k] for k in ("summary", "recommendation", "rationale")}
        # The recommendation is never softer than the flags, whatever the model chose.
        worst = max((SEVERITY_RANK[f["severity"]] for f in flags), default=0)
        overridden = worst >= SEVERITY_RANK["major"] and body["recommendation"] == "proceed_to_signoff"
        if overridden:
            body["recommendation"] = "revise"
        approvers = ["supervisor"] + (["ehs"] if worst == SEVERITY_RANK["critical"] else [])
        report = {
            "submission_digest": state["submission_digest"],
            "product": sub["product"],
            **body,
            "flags": flags,
            "lanes": lanes,
            "required_approvers": approvers,
        }
        sha = archive.put(report)
        violations += withheld
        tracing.record("guard", node="report_guard", outcome="fallback" if fallback else "pass",
                       violations=violations, overridden=overridden, approvers=approvers,
                       evidence=[tracing.evidence("model_output", "report", report)])  # fmt: skip
        return {
            "guard": {
                "passed": not violations,
                "violations": violations,
                "revise": False,
                "fallback": fallback,
            },
            "report": report,
            "report_digest": sha,
        }

    # --- human sign-off: the only way anything is approved ----------------------------------

    def _sign(state: ReviewState, role: str) -> dict:
        """Everything before request_approvals() is deterministic: LangGraph re-runs it on resume."""
        report = state["report"]
        call = {"tool_use_id": f"signoff-{role}", "tool": "sign_off_process",
                "input": {"report_digest": state["report_digest"], "role": role,
                          "recommendation": report["recommendation"]}}  # fmt: skip
        approval = ap.request_approvals([call], ledger)[call["tool_use_id"]]
        result = registry.execute(
            call["tool"], call["input"], tool_use_id=call["tool_use_id"], approval=approval
        )
        ok = not result.get("is_error")
        return {
            "signoffs": [{"role": role, "decision": approval.decision, "approver": approval.approver}],
            "executed": [call["tool"]] if ok else [],
            "blocked": [] if ok else [call["tool"]],
            "tool_calls": [{"name": call["tool"]}],
        }

    def supervisor_signoff(state: ReviewState) -> dict:
        return _sign(state, "supervisor")

    def ehs_signoff(state: ReviewState) -> dict:
        return _sign(state, "ehs")

    def after_supervisor(state: ReviewState) -> str:
        signed = any(
            s["role"] == "supervisor" and s["decision"] == "approved" for s in state.get("signoffs", [])
        )
        return "ehs_signoff" if signed and "ehs" in state["report"]["required_approvers"] else "finalize"

    def finalize(state: ReviewState) -> dict:
        report = state.get("report") or {}
        required = report.get("required_approvers", [])
        signed = {s["role"] for s in state.get("signoffs", []) if s["decision"] == "approved"}
        if state.get("incomplete"):
            status = "returned_to_submitter"
        elif required and set(required) <= signed and len(state.get("executed", [])) == len(required):
            status = "approved"
        else:
            status = "not_approved"
        ev = tracing.evidence
        refs = [ev("input", "submission", state["submission"])]
        if report:
            refs.append(ev("model_output", "report", report))
        cited = {c["chunk_id"] for f in report.get("flags", []) for c in f["citations"]}
        shown = {p["chunk_id"]: p for ps in (state.get("passages") or {}).values() for p in ps}
        refs += [ev("chunk", cid, shown[cid]["text"] if cid in shown else None) for cid in sorted(cited)]
        refs += [ev("retrieval", cid) for cid in sorted(set(shown) - cited)]
        refs += [ev("rule", c["chunk_id"]) for f in state.get("locked_flags", []) for c in f["citations"]]
        refs += [ev("approval", f"{s['role']}:{s['decision']}") for s in state.get("signoffs", [])]
        tracing.decision("finalize", status, rule=f"process_review:v{FLOW_VERSION}",
                         basis=f"approvers:{'+'.join(required) or 'none'}", evidence=refs)  # fmt: skip
        tracing.record("retention", node="finalize", days=RETENTION_DAYS, archived=bool(report))
        guard = state.get("guard") or {}
        return {
            "outcome": {
                "status": status,
                "recommendation": report.get("recommendation"),
                "required_approvers": required,
                "signoffs": state.get("signoffs", []),
                "flags": report.get("flags", []),
                "summary": report.get("summary", ""),
                "lanes": report.get("lanes", {}),
                "incomplete": state.get("incomplete", []),
                "injection_flagged": state.get("injection_flagged", False),
                "dropped": len(state.get("dropped", [])),
                "guard_violations": guard.get("violations", []),
                "fallback_report": guard.get("fallback", False),
                "executed_actions": state.get("executed", []),
                "blocked_actions": state.get("blocked", []),
                "tool_calls": state.get("tool_calls", []),
                "report_digest": state.get("report_digest", ""),
            }
        }

    g = StateGraph(ReviewState)
    nodes = [intake, hazard_rules, plan_queries, *(_search(s) for s in STORES), *(_review(s) for s in STORES),
             verify, synthesis, report_guard, supervisor_signoff, ehs_signoff, finalize]  # fmt: skip
    for fn in nodes:
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges(
        "intake",
        tracing.audited_route("after_intake")(
            lambda s: "finalize" if s.get("incomplete") else "hazard_rules"
        ),
        ["hazard_rules", "finalize"],
    )
    g.add_edge("hazard_rules", "plan_queries")
    for s in STORES:  # three lanes in parallel; verify waits for all three
        g.add_edge("plan_queries", f"search_{s}")
        g.add_edge(f"search_{s}", f"{s}_review")
    g.add_edge([f"{s}_review" for s in STORES], "verify")
    g.add_edge("verify", "synthesis")
    g.add_edge("synthesis", "report_guard")
    g.add_conditional_edges(
        "report_guard",
        tracing.audited_route("after_guard")(
            lambda s: "synthesis" if s["guard"].get("revise") else "supervisor_signoff"
        ),
        ["synthesis", "supervisor_signoff"],
    )
    g.add_conditional_edges(
        "supervisor_signoff",
        tracing.audited_route("after_supervisor")(after_supervisor),
        ["ehs_signoff", "finalize"],
    )
    g.add_edge("ehs_signoff", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver(), name="process_review")


def recommendation_policy(approver: str) -> ap.Decider:
    """Stand-in approver for evals: signs off only on a proceed_to_signoff recommendation."""

    def decide(calls: list[dict]) -> dict[str, dict]:
        return {c["tool_use_id"]: {"decision": "approved" if c["input"].get("recommendation") ==
                                   "proceed_to_signoff" else "denied", "approver": approver}
                for c in calls}  # fmt: skip

    return decide


def run_review(
    submission: dict,
    *,
    decide: ap.Decider | None = None,
    index: DocIndex | None = None,
    today: date | None = None,
    ledger: Ledger | None = None,
    archive: Archive | None = None,
) -> dict:
    """Review one submission. `decide` answers each sign-off (default: the recommendation policy)."""
    index = index or DocIndex(today=today or date.today())  # evals pass a fixed-date index
    graph = build_graph(index, archive or Archive(), ledger or Ledger())
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    state = ap.run_with_approvals(
        graph, {"submission": submission}, decide or recommendation_policy("approver-sim"), config
    )
    return state["outcome"]
