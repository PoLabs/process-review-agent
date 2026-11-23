"""The examples the landing page and every refusal offer must be questions the app will take.

Offline: each example goes through the code rules and the harm word list (the parts of the gates
that need no model). A suggested question that the app then refuses is the worst first impression.
"""

from __future__ import annotations

import pytest

from flows.process_qa.examples import QUESTIONS, TARGET_INFO, WONT_ANSWER, WORKFLOWS
from flows.process_qa.graph import ASKS_APPROVAL, ASKS_REWRITE, HARM_LEXICON, OTHERS_WORK, SUBMISSION_ID
from flows.process_qa.state import TARGETS
from flows.process_review.graph import REVIEW_INJECTION
from kit import guardrails

EXAMPLES = [(t, q) for t, qs in QUESTIONS.items() for q in qs]


def test_every_target_has_examples_and_a_label():
    assert set(QUESTIONS) == set(TARGETS) == set(TARGET_INFO)
    assert all(len(QUESTIONS[t]) >= 3 for t in TARGETS)
    assert WORKFLOWS and WONT_ANSWER


@pytest.mark.parametrize("target, question", EXAMPLES)
def test_no_example_trips_a_refusal_rule(target, question):
    assert not ASKS_APPROVAL.search(question)
    assert not ASKS_REWRITE.search(question)
    assert not SUBMISSION_ID.search(question) and not OTHERS_WORK.search(question)
    assert not guardrails.screen_injection(question).flagged
    assert not any(rx.search(question) for rx in REVIEW_INJECTION.values())
    assert not any(rx.search(question) for rx in HARM_LEXICON.values())
    assert len(question) <= 1000


@pytest.mark.parametrize(
    "target, question", [(t, q) for t, q in EXAMPLES if t in ("safety", "maintenance", "qc")]
)
def test_store_examples_name_something_the_store_covers(target, question):
    from flows.process_review.docs import DocIndex

    covers = {i for d in DocIndex().current(target) for i in d.covers}
    named = {i for i in covers if i.replace("_", " ").lower() in question.lower()}
    assert named, f"{target} example names no item its current documents cover"
