"""Scoped questions about a process review, as a LangGraph graph. Design rules: state.py.

    run_qa(question, target, *, submission, report) -> outcome dict (state.QAOutcome)

The server binds `submission` and `report` from the session's own review run; nothing in the
question can reach other material. Three gates: a harm screen (word list + classifier, fails
closed), a scope gate (code rules, then a classifier), and an answer guard (citations, numbers,
wording, recipes, ids). No external tools: the flow reads and answers, nothing else.
"""

from __future__ import annotations

import re
import uuid
from functools import cache

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from flows.process_qa import prompts
from flows.process_qa.examples import QUESTIONS
from flows.process_qa.state import STORE_TARGETS, DraftAnswer, QAOutcome, QAState, ScopeVerdict
from flows.process_review.docs import DocIndex
from flows.process_review.graph import APPROVAL_LANGUAGE, REVIEW_INJECTION, numbers
from kit import guardrails, llm, tracing
from kit.llm import Role
from kit.patterns.retrieve import build_retrieve_node
from kit.patterns.screen import RiskScreen

FLOW_VERSION = "1"
# Mirrors flows/process_review/graph.py report_guard, where the approvers are decided.
APPROVER_RULE = "The supervisor signs off on every report; EHS must also sign off when any flag is critical."
MAX_QUESTION_CHARS = 1000
MAX_PASSAGES = 8
MAX_REVISIONS = 1

GATES = {
    "harm_screen": "word list + classifier: intent to cause harm is refused and noted for EHS; fails closed",
    "scope_gate": "code rules (approval, rewrite, other submission, injection), then a scope classifier",
    "answer_guard": "exact current citations, grounded numbers, no safe-to-run wording, no recipe, own ids",
}
ROUTES = {
    ("harm_screen", "refuse"): "harmful intent (or screen failed)",
    ("harm_screen", "scope_gate"): "no risk",
    ("scope_gate", "refuse"): "out of scope",
    ("scope_gate", "search_store"): "safety / maintenance / qc",
    ("scope_gate", "load_own"): "my process / the report",
    ("answer_guard", "answer"): "fails guard: revise once",
    ("answer_guard", "finalize"): "passed, or withheld",
}

# --- the word lists ------------------------------------------------------------------------------

HARM_LEXICON: dict[str, re.Pattern[str]] = {
    "maximize_hazard": re.compile(
        r"\b(?:most|more|maximum|max|biggest|largest|strongest)\s+(?:violent|powerful|explosive|toxic|"
        r"poisonous|dangerous|damage|explosion|fire|reaction|blast|fumes?|gas)\b"
        r"|\bmake\s+(?:a|an|the)?\s*(?:bomb|explosive|explosion|toxic\s+gas|poison)",
        re.IGNORECASE,
    ),
    "defeat_control": re.compile(
        r"\b(?:bypass|defeat|disable|override|get\s+around|work\s+around|jump(?:er)?)\s+(?:the\s+|an?\s+)?"
        r"(?:interlock|lockout|lock-out|alarm|scrubber|relief\s+valve|safety|control|sensor|detector)",
        re.IGNORECASE,
    ),
    "evade_detection": re.compile(
        r"\bwithout\s+(?:anyone|anybody|the\s+supervisor|ehs|them)\s+(?:noticing|knowing|finding\s+out)\b"
        r"|\bhide\s+(?:it|this)\s+from\b",
        re.IGNORECASE,
    ),
    "harm_people": re.compile(
        r"\b(?:hurt|harm|poison|injure)\s+(?:someone|somebody|people|a\s+coworker)\b", re.IGNORECASE
    ),
}
HARM = RiskScreen(
    lexicon=HARM_LEXICON,
    system=prompts.HARM_SYSTEM,
    wrap=prompts.wrap_question,
    imminent=frozenset({"maximize_hazard", "defeat_control", "harm_people"}),
    role=Role.WORKER,  # the call that matters most gets the stronger model
    name="harm_screen",
)

# Decided by code before any model sees the question: what chat must never do, however it's asked.
ASKS_APPROVAL = re.compile(
    r"\b(?:approve|sign\s+off(?:\s+on)?)\s+(?:it|this|my|the)\b"
    r"|\bis\s+(?:it|this|my\s+\w+)\s+(?:approved|safe|ok|okay|fine|good|ready)\b"
    r"|\bsafe\s+to\s+(?:run|proceed|start|use|go)\b|\b(?:can|may|should)\s+(?:i|we)\s+(?:run|start|go\s+ahead|proceed)\b"
    r"|\bgo\s+ahead\b|\byes\s+or\s+no\b|\bgreen\s*light\b",
    re.IGNORECASE,
)
ASKS_REWRITE = re.compile(
    r"\b(?:re-?write|fix|correct|redo|reformulate|adjust|change|modify)\s+(?:my|the|this)\s+"
    r"(?:formulation|recipe|process|quantities|amounts|mix)\b|\bnew\s+(?:quantities|recipe|formulation|amounts)\b"
    r"|\bwhat\s+(?:quantities|amounts|ratio)\s+should\s+(?:i|we)\s+use\b|\bso\s+(?:that\s+)?it\s+passes\b",
    re.IGNORECASE,
)
SUBMISSION_ID = re.compile(r"\bSUB-\d+\b", re.IGNORECASE)
OTHERS_WORK = re.compile(
    r"\b(?:someone|somebody|another\s+\w+|other\s+\w+)(?:\s+else)?'s\s+(?:submission|report|process|formulation)\b",
    re.IGNORECASE,
)
# Chat must not read as permission, whatever the wording of the question.
SAFE_LANGUAGE = re.compile(
    r"\b(?:safe|ok|okay|fine|good|ready)\s+to\s+(?:run|proceed|start|go|use)\b|\byou\s+(?:can|may|should)\s+"
    r"(?:run|proceed|start|go\s+ahead)\b|\bgo\s+ahead\b|\bit\s+is\s+safe\b",
    re.IGNORECASE,
)
QUANTITY = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(kg|g|L|mL|ml|litres?|liters?|kilograms?|grams?)\b")
SUBSTITUTE = re.compile(
    r"\b(?:replace|substitute|swap)\s+\w+(?:\s+\w+)?\s+(?:with|for)\b|\binstead\s+use\b", re.I
)


def _user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def _flatten(obj, prefix: str = "") -> list[tuple[str, object]]:
    """Leaf values with their paths: steps.0.temp_c -> 45."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _flatten(v, f"{prefix}{k}.")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _flatten(v, f"{prefix}{i}.")]
    return [(prefix[:-1], obj)]


def _as_text(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def own_fields(submission: dict) -> list[dict]:
    """The bound submission as citable fields. Notes are left out: they are free text from the
    submitter, the one part of the form that can carry instructions."""
    body = {k: v for k, v in submission.items() if k not in ("notes", "submitter")}
    return [{"ref": p, "value": _as_text(v)} for p, v in _flatten(body) if v is not None]


def report_material(report: dict) -> list[dict]:
    out = [
        {"ref": f"report.{k}", "value": _as_text(report[k]) if not isinstance(report[k], list) else report[k]}
        for k in ("summary", "recommendation", "rationale", "required_approvers", "status")
        if report.get(k) not in (None, "", [])
    ]
    # How approvers are chosen is a rule in code, not in the report text: state it so it can be cited.
    out.append({"ref": "report.approver_rule", "value": APPROVER_RULE})
    for i, f in enumerate(report.get("flags", []), start=1):
        out.append(
            {
                "ref": f"flag:{i}",
                **{k: f.get(k) for k in ("severity", "lane", "kind", "subjects", "title", "detail")},
                "cites": [
                    {"doc": c["doc_id"], "version": c["version"], "quote": c["quote"]}
                    for c in f.get("citations", [])
                ],
            }
        )
    return out


@cache
def default_index() -> DocIndex:
    return DocIndex()


def build_graph(index: DocIndex):
    # Keyword and BM25 only: the question never goes to the embedding vendor (plan: data handling).
    retrieve = {
        s: build_retrieve_node([t for t in index.tiers(s) if t.name != "vector"], mode="cascade", k=4)
        for s in STORE_TARGETS
    }

    def intake(state: QAState) -> dict:
        q = state["question"]
        screen = guardrails.screen_injection(q)
        signals = sorted({*screen.signals, *(n for n, rx in REVIEW_INJECTION.items() if rx.search(q))})
        tracing.record(
            "intake",
            node="intake",
            target=state["target"],
            chars=len(q),
            injection=bool(signals),
            signals=signals,
            bound_submission=state.get("submission") is not None,
            bound_report=state.get("report") is not None,
            evidence=[tracing.evidence("input", "question", q)],
        )
        return {"injection_flagged": bool(signals)}

    def harm_screen(state: QAState) -> dict:
        verdict = HARM.assess(state["question"])
        harmful = verdict["risk"] != "none"
        refusal = None
        if harmful:
            reason = "harm_screen_failed" if verdict["source"] == "classifier_error" else "harmful_intent"
            refusal = {"reason": reason, "suggested_target": None}
        return {"harm": {**verdict, "harmful": harmful}, **({"refusal": refusal} if refusal else {})}

    def _rule(state: QAState) -> str | None:
        q, target = state["question"], state["target"]
        sub, report = state.get("submission"), state.get("report")
        if len(q) > MAX_QUESTION_CHARS:
            return "too_long"
        if state.get("injection_flagged"):
            return "injection"
        own = str((sub or {}).get("submission_id", "")).upper()
        if any(m.upper() != own for m in SUBMISSION_ID.findall(q)) or OTHERS_WORK.search(q):
            return "other_submission"
        if ASKS_APPROVAL.search(q):
            return "asks_approval"
        if ASKS_REWRITE.search(q):
            return "asks_rewrite"
        if target in ("my_process", "report") and sub is None:
            return "no_bound_run"
        if target == "report" and not report:
            return "no_report_yet"
        return None

    def scope_gate(state: QAState) -> dict:
        rule = _rule(state)
        if rule:
            scope = {
                "in_scope": False,
                "category": "out_of_scope",
                "suggested_target": None,
                "reason_code": rule,
                "rule": rule,
            }
            tracing.record("scope", node="scope_gate", outcome="refuse", rule=rule)
            return {"scope": scope, "refusal": {"reason": rule, "suggested_target": None}}
        try:
            v = llm.structured(
                Role.CLASSIFIER,
                system=prompts.scope_system(state["target"]),
                messages=_user(prompts.wrap_question(state["question"])),
                schema=ScopeVerdict,
                node="scope_gate",
            ).model_dump()
        except Exception as e:  # no verdict: refuse (fails closed)
            tracing.record(
                "scope", node="scope_gate", outcome="refuse", rule="classifier_error", error=type(e).__name__
            )
            return {
                "scope": {
                    "in_scope": False,
                    "category": "out_of_scope",
                    "suggested_target": None,
                    "reason_code": "classifier_error",
                    "rule": "classifier_error",
                },
                "refusal": {"reason": "out_of_scope", "suggested_target": None},
            }
        scope = {**v, "rule": "classifier"}
        ok = v["in_scope"] and v["category"] != "out_of_scope"
        tracing.record(
            "scope",
            node="scope_gate",
            outcome="answer" if ok else "refuse",
            rule="classifier",
            category=v["category"],
            suggested=v["suggested_target"],
        )
        if ok:
            return {"scope": scope}
        other = v["suggested_target"] if v["suggested_target"] not in (None, state["target"]) else None
        if other in ("my_process", "report") and state.get("submission") is None:
            other = None  # don't suggest what this session can't reach
        reason = "wrong_target" if other else "out_of_scope"
        return {"scope": scope, "refusal": {"reason": reason, "suggested_target": other}}

    def search_store(state: QAState) -> dict:
        store, q = state["target"], state["question"]
        hits: dict[str, dict] = {}
        for h in retrieve[store]({"query": q})["retrieval"]["hits"]:
            hits.setdefault(h["chunk_id"], {"text": h["text"], "meta": h["meta"]})
        # Every current passage about an item the question names (a chemical, a machine, a product).
        ql = q.lower()
        for c in index.chunks(store):
            if any(item.replace("_", " ").lower() in ql for item in c.meta.get("covers", [])):
                hits.setdefault(c.id, {"text": c.text, "meta": c.meta})
        material = [
            {
                "ref": cid,
                "doc_id": h["meta"]["doc_id"],
                "version": h["meta"]["version"],
                "heading": h["meta"].get("heading", ""),
                "text": h["text"],
            }
            for cid, h in list(hits.items())[:MAX_PASSAGES]
        ]
        return {"material": material}

    def load_own(state: QAState) -> dict:
        if state["target"] == "my_process":
            return {"material": own_fields(state["submission"])}
        return {"material": report_material(state["report"])}

    def answer(state: QAState) -> dict:
        parts = [
            prompts.wrap_question(state["question"]),
            prompts.wrap_material(state["target"], state["material"]),
        ]
        guard = state.get("guard") or {}
        if guard.get("violations"):
            parts.append(f"Your previous answer was rejected for: {guard['violations']}. Fix exactly those.")
        try:
            d = llm.structured(
                Role.WORKER,
                system=prompts.answer_system(state["target"]),
                messages=_user("\n\n".join(parts)),
                schema=DraftAnswer,
                node="answer",
            ).model_dump()
        except Exception as e:
            tracing.record("answer_failed", node="answer", error=type(e).__name__)
            d = {"answer": "", "citations": [], "cannot_answer": True, "failed": True}
        return {"draft": d}

    def _check_citation(c: dict, state: QAState, now: DocIndex) -> tuple[dict | None, set[float]]:
        """The verified citation and the numbers it supports, or (None, {}) if it doesn't hold."""
        material = {m["ref"]: m for m in state["material"]}
        m = material.get(c["ref"])
        if m is None:
            return None, set()
        if c["kind"] == "chunk":
            if "text" not in m or not c["quote"].strip() or _norm(c["quote"]) not in _norm(m["text"]):
                return None, set()
            if not now.is_current(m["doc_id"], m["version"]):
                return None, set()
            return {**c, "doc_id": m["doc_id"], "version": m["version"]}, numbers(c["quote"])
        if c["kind"] == "flag":
            if (
                "title" not in m
                or _norm(c["quote"]) not in _norm(m["title"])
                and _norm(m["title"]) not in _norm(c["quote"])
            ):
                return None, set()
            support = f"{m['title']} {m.get('detail', '')} " + " ".join(
                x["quote"] for x in m.get("cites", [])
            )
            return dict(c), numbers(support)
        value = m.get("value")
        if value is None:
            return None, set()
        # A list (approvers) is read as people write it: "supervisor, ehs", or any of its items.
        items = [_as_text(v) for v in value] if isinstance(value, list) else [_as_text(value)]
        shown, quote = _norm(", ".join(items)), _norm(c["quote"])
        if not (shown in quote or quote in shown or all(_norm(i) in quote for i in items)):
            return None, set()
        return dict(c), numbers(" ".join(items))

    def answer_guard(state: QAState) -> dict:
        d, sub = state["draft"], state.get("submission") or {}
        now = index.reread()  # document status as it is now
        text = d["answer"]
        cites, support, violations = [], numbers(state["question"]), []
        for c in d["citations"]:
            ok, nums = _check_citation(c, state, now)
            if ok is None:
                violations.append(f"bad_citation:{c['kind']}")
            else:
                cites.append(ok)
                support |= nums
        if d.get("failed"):
            violations.append("answer_failed")
        if not d["cannot_answer"] and not cites:
            violations.append("no_citation")
        if numbers(text) - support:
            violations.append("ungrounded_number")
        if APPROVAL_LANGUAGE.search(text) or SAFE_LANGUAGE.search(text):
            violations.append("safe_language")
        on_form = {float(c["quantity"]) for c in sub.get("chemicals", []) if c.get("quantity") is not None}
        quoted = {n for c in cites for n in numbers(c["quote"])}
        if any(float(n) not in on_form | quoted for n, _ in QUANTITY.findall(text)) or SUBSTITUTE.search(
            text
        ):
            violations.append("recipe")
        own = str(sub.get("submission_id", "")).upper()
        if any(m.upper() != own for m in SUBMISSION_ID.findall(text)):
            violations.append("other_submission")
        violations = sorted(set(violations))
        revisions = state.get("revisions", 0)
        revise = bool(violations) and revisions < MAX_REVISIONS and not d.get("failed")
        tracing.record(
            "guard",
            node="answer_guard",
            outcome="revise" if revise else ("withhold" if violations else "pass"),
            violations=violations,
            n_citations=len(cites),
        )
        guard = {
            "passed": not violations,
            "violations": violations,
            "revise": revise,
            "withheld": bool(violations) and not revise,
            "citations": cites,
        }
        return {"guard": guard, "revisions": revisions + int(revise)}

    def refuse(state: QAState) -> dict:
        r = state["refusal"]
        if r["reason"] in ("harmful_intent", "harm_screen_failed"):
            # The EHS notice: what was refused and why, as codes and a digest. Never the question.
            tracing.decision(
                "refuse",
                "ehs_notice",
                rule="process_qa:harm_screen",
                basis=r["reason"],
                evidence=[
                    tracing.evidence("input", "question", state["question"]),
                    *(tracing.evidence("rule", f"harm:{s}") for s in state["harm"].get("signals", [])),
                ],
            )
        return {}

    def finalize(state: QAState) -> dict:
        target, scope, guard = state["target"], state.get("scope") or {}, state.get("guard") or {}
        r = state.get("refusal")
        ehs = bool(r and r["reason"] in ("harmful_intent", "harm_screen_failed"))
        if r:
            suggested = r.get("suggested_target")
            out = QAOutcome(
                status="refused",
                target=target,
                refusal_reason=r["reason"],
                suggested_target=suggested,
                examples=QUESTIONS[suggested or target][:3],
                ehs_notice=ehs,
                injection_flagged=state.get("injection_flagged", False),
                category=scope.get("category"),
            )
        elif guard.get("withheld"):
            chunks = [c for c in guard.get("citations", []) if c["kind"] == "chunk"]
            out = QAOutcome(
                status="withheld",
                target=target,
                citations=chunks,
                examples=QUESTIONS[target][:3],
                category=scope.get("category"),
                guard_violations=guard["violations"],
                injection_flagged=state.get("injection_flagged", False),
            )
        else:
            out = QAOutcome(
                status="answered",
                target=target,
                answer=state["draft"]["answer"],
                citations=guard.get("citations", []),
                category=scope.get("category"),
                injection_flagged=state.get("injection_flagged", False),
            )
        ev = tracing.evidence
        refs = [ev("input", "question", state["question"])]
        refs += [ev("chunk", c.ref) if c.kind == "chunk" else ev("case", c.ref) for c in out.citations]
        if state.get("submission"):
            refs.append(ev("case", "bound_submission", state["submission"]))
        if state.get("report"):
            refs.append(ev("model_output", "bound_report", state["report"]))
        tracing.decision(
            "finalize",
            out.status,
            rule=f"process_qa:v{FLOW_VERSION}",
            basis=out.refusal_reason
            or ("guard:" + "+".join(out.guard_violations) if out.guard_violations else target),
            evidence=refs,
        )
        return {"outcome": out.model_dump()}

    g = StateGraph(QAState)
    for fn in (
        intake,
        harm_screen,
        scope_gate,
        search_store,
        load_own,
        answer,
        answer_guard,
        refuse,
        finalize,
    ):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "harm_screen")
    g.add_conditional_edges(
        "harm_screen",
        tracing.audited_route("after_harm")(lambda s: "refuse" if s["harm"]["harmful"] else "scope_gate"),
        ["refuse", "scope_gate"],
    )
    g.add_conditional_edges(
        "scope_gate",
        tracing.audited_route("after_scope")(
            lambda s: (
                "refuse"
                if s.get("refusal")
                else ("search_store" if s["target"] in STORE_TARGETS else "load_own")
            )
        ),
        ["refuse", "search_store", "load_own"],
    )
    g.add_edge("search_store", "answer")
    g.add_edge("load_own", "answer")
    g.add_edge("answer", "answer_guard")
    g.add_conditional_edges(
        "answer_guard",
        tracing.audited_route("after_guard")(lambda s: "answer" if s["guard"]["revise"] else "finalize"),
        ["answer", "finalize"],
    )
    g.add_edge("refuse", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver(), name="process_qa")


def run_qa(
    question: str, target: str, *, submission: dict | None, report: dict | None, index: DocIndex | None = None
) -> dict:
    """Answer or refuse one question. `submission` / `report` come from the server's session binding."""
    graph = build_graph(index or default_index())
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    state = graph.invoke(
        {"question": question, "target": target, "submission": submission, "report": report}, config
    )
    return state["outcome"]
