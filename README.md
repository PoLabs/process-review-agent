# Process Review Agent

Two agents for a chemical manufacturing site, built on LangGraph and the Anthropic SDK.

- **`process_review`** checks a submitted formulation or process against the *current* safety, maintenance and QC documents. It flags each issue with a quoted source and recommends a fix. It never approves anything: a supervisor signs every report, and an EHS engineer signs too when any flag is critical.
- **`process_qa`** answers questions about a submission, one document store, or a review report, inside a strict scope, with cited answers.

All data in this repo is synthetic.

## Read first

| | |
|---|---|
| [`docs/one-pager.html`](docs/one-pager.html) | One page for non-technical readers |
| [`docs/slide-deck.html`](docs/slide-deck.html) | 10-slide stakeholder deck (source in `docs/deck/`) |
| [`flows/process_review/README.md`](flows/process_review/README.md) | Design, guarantees, verification status and known gaps |
| [`flows/process_qa/README.md`](flows/process_qa/README.md) | Same, for the question agent |
| [`web/README.md`](web/README.md) | React front end (type-checks; not yet driven in a browser) |

## Run

```
uv sync
make test                       # offline, no API key needed
cp .env.example .env            # add ANTHROPIC_API_KEY to run the agents
make eval FLOW=process_review K=3   # live API calls, costs money
make serve                      # API on :8000
```

The `.env` file is gitignored. Never commit keys.

## Layout

```
flows/process_review/   review graph, hazard rules, document index and data, upload extraction
flows/process_qa/       scoped question graph
kit/                    the shared runtime the flows use: model routing, tracing, audit log,
                        approval gate, retrieval, guardrails
evals/                  eval harness, checks, and the datasets for both flows
app/                    FastAPI routes for both flows
web/                    React front end
tests/                  offline tests
```

## Status and limits

Latest saved test runs (3 repeats per case): review 51/51, question agent 54/54, upload reader 15/15, with all 36 planted defects flagged. The test cases were written by the team that built the agents, and there is no held-out set. The agents take submitted quantities and temperatures as typed, and rely on the loaded documents being current and correct. Approver identity is typed in, not verified. See the flow READMEs for the full list.

This is a reference build, not a certified safety system. The regulations named in the deck are requirements it supports, not compliance claims.
