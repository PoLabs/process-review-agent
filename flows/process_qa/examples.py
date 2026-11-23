"""Example workflows and questions: the landing page shows them, and every refusal offers them.

One source, so what the app suggests is exactly what it is designed to answer. Each question
here must be answerable from the synthetic world (flows/process_review/WORLD.md); the Q&A eval
asks a sample of them (tests/test_process_qa_examples.py checks they stay in scope by rule).
"""

from __future__ import annotations

from flows.process_qa.state import Target

TARGET_INFO: dict[Target, dict[str, str]] = {
    "my_process": {"label": "My process", "description": "What your submitted formulation or process says."},
    "safety": {
        "label": "Safety",
        "description": "Safety data sheets, incompatibilities, limits, handling SOPs.",
    },
    "maintenance": {"label": "Maintenance", "description": "Equipment ratings, service status, lockouts."},
    "qc": {"label": "Quality control", "description": "Product specifications and acceptance ranges."},
    "report": {"label": "The report", "description": "The review agent's flags, summary and recommendation."},
}

QUESTIONS: dict[Target, list[str]] = {
    "my_process": [
        "What temperature does step 2 of my process run at?",
        "Which equipment does my process use?",
        "How much toluene is in my formulation?",
    ],
    "safety": [
        "What is the maximum process temperature for acetone?",
        "Can sodium hypochlorite be mixed with ammonia?",
        "What does the acid handling SOP require for hydrochloric acid?",
    ],
    "maintenance": [
        "What is the pressure rating of reactor R-102?",
        "Is scrubber SC-1 in service?",
        "What is the maximum temperature for mixing tank M-7?",
    ],
    "qc": [
        "What is the sodium hypochlorite range in the SAN-12 specification?",
        "What cure temperature does the RX-40 specification require?",
        "What mix temperature does the CX-200 specification give?",
    ],
    "report": [
        "Why does the report recommend revising my process?",
        "What does the most severe flag mean, and which document does it cite?",
        "Which approvers does this report need, and why?",
    ],
}

WORKFLOWS: list[dict] = [
    {
        "title": "Check a new formulation before it reaches the floor",
        "description": "Upload your formulation or process document, confirm the parsed form, "
        "run the review.",
        "steps": [
            "Upload a PDF, Word or text document, or fill in the form by hand.",
            "Check every highlighted field: the parser never guesses a value the document doesn't state.",
            "Run the review: safety, maintenance and QC are checked in parallel against current documents.",
            "Read the report and ask about any flag. Your supervisor signs off; "
            "EHS too if a flag is critical.",
        ],
    },
    {
        "title": "Look up a limit or a rating",
        "description": "Ask one document store directly, with the source quoted.",
        "steps": [
            "Pick Safety, Maintenance or Quality control.",
            "Ask for one limit, rating or specification at a time.",
            "Every answer quotes the current version of the document it comes from.",
        ],
    },
    {
        "title": "Understand a review result",
        "description": "Ask why a flag was raised or why the report recommends what it does.",
        "steps": [
            "After a review, pick The report.",
            "Ask about a flag or the recommendation; answers cite the flag and its document.",
            "To change the process, edit the form and run the review again: chat never rewrites a recipe.",
        ],
    },
]

WONT_ANSWER: list[str] = [
    "Whether a process is approved or safe to run: only the sign-offs decide that.",
    "A corrected or new recipe: change the form and run the review again.",
    "Anyone else's submission or report.",
    "General chemistry or anything outside the plant's documents.",
    "How to make a hazard worse or get around a control.",
]
