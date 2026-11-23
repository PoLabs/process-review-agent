# process_review

Everything below was written from the code in this folder, the approved plan
(`plans/we-re-building-an-agentic-swift-stroustrup-of-4e65700c-eb51-4f3c-8ddd-d7563f9dd8d8.md`), the
k=1 eval report `runs/20261123-114145-process-review-eval-687c50/report.md`, and the offline
compliance result. Where something could not be established from those, it says "not established"
or "not stated in the ask". Sections 3a and 3b (upload extraction and starting a review from the app) were
added later, from `extract.py`, `extract_evals.py`, `app/process_api.py`, `kit/runner.py` and
`.scratch/ext_eval_k1.md`. The numbers in sections 5 and 6 are about the review agent only and were not
changed.

## 1. What this is

A floor-supervisor aid that reads an intern's formulation or chemical-process form, checks it against
the current safety, maintenance and QC documents, and hands the supervisor a report: every issue as a
flag with a quoted passage, conflicts between the three areas, and a recommendation (proceed to
sign-off, revise, or reject). It never approves anything; only recorded human sign-offs do. The graph,
drawn from the compiled code, is in [GRAPH.md](GRAPH.md); the plan's diagram and the synthetic
document world are in the plan and in [WORLD.md](WORLD.md).

The review agent itself is unchanged by the interactive system. Around it now sit: upload extraction
(section 3a), a way to start a review from the web app (section 3b), and a separate scoped Q&A agent,
[process_qa](../process_qa/README.md). The React app is in [web/](../../web/README.md).

## 2. Why it exists

**Purpose, from the ask.** A wrong formulation can cause a serious plant incident, and supervisors are not
always the expert in every area a process touches. The agent does the document lookup and
cross-checking so that the supervisor reads flags with sources instead of searching three document
sets.

**How the return is measured.** Flags found per process review. In the evals that is recall of seeded
defects (`finds_expected_flags`, and `roi_line()` in `evals.py` prints "flags caught out of flags
seeded"), with clean cases guarding against false alarms.

**Not stated in the ask** (ask the plant owner before quoting any business number):
- How many flags per review supervisors catch today, so there is no baseline to compare recall with.
- How many reviews a week this will see, and what a review costs now.
- The recall the plant actually needs. The plan sets 0.9 for the eval gate; the ask gives no reason
  for that figure.
- Why EHS sign-off is tied to a critical flag specifically. The plan records it as a design
  decision with no rationale.

**What it deliberately does not automate: final approval.** The supervisor signs off on every report,
and an EHS engineer also signs off when any flag is critical. The agent can only recommend. This is
enforced in code (section 3), not just asked of the model.

## 3. How it works

Order of events: intake, hazard rules, plan queries, three parallel lanes (search then review), verify,
synthesis, report guard, supervisor sign-off, EHS sign-off if needed, finalize. Five model calls on a
normal run (plan, three lane reviews, synthesis), six if the guard sends the report back once. Which
model serves each role is set in `kit/llm.py`, not here.

### The model steps

**Plan queries (classifier role).** *Must:* write search questions for the safety, maintenance and QC
stores. *May choose:* the wording and number of extra questions. *Can never:* leave a lane or a form
item without a query. Code, not the prompt, guarantees this: it writes one query per chemical, per
piece of equipment and for the product itself, puts them first, then appends the model's, and keeps at
most `QUERIES_PER_LANE` (8) per lane. Made-up items in the model's queries are not blocked; they only
retrieve text.

**Lane reviews: safety, maintenance, QC (worker role, one call per lane, same template, different
scope).** *Must:* give a plain-language summary and one flag per distinct issue, each with a severity,
a chunk id from the passages it was shown, and a quote copied exactly. *May choose:* which passages
matter and how severe each flag is, within the severity guide in `prompts.py`. *Can never:* keep a flag
that cites a chunk it was not shown, a quote not in that chunk, a document that is not current, or a
number that appears in neither its quotes nor the form. Code enforces those four in `verify`. "Do not
comment on other lanes" is a prompt instruction only; no code checks it. A model may label a flag only
with the kinds in `state.ModelFlagKind`; `unverified`, `coverage_gap` and `injection` are reserved for
code. A lane flag that repeats a rule flag (same kind, overlapping subjects) is folded into the rule
flag, which can only be raised in severity by it.

**Synthesis (supervisor role).** *Must:* look for conflicts across lanes (for example a QC spec
temperature above an equipment rating), then write a summary, a rationale and one of three
recommendations. *May choose:* order and wording, and whether to raise cross-lane flags. *Can never:*
produce a recommendation outside `proceed_to_signoff` / `revise` / `reject` (a closed type); change,
drop or downgrade any flag (the final flag list is assembled by code, synthesis only adds cross flags);
or use approval wording or an ungrounded number in its summary and rationale (the guard checks those
two fields, see caveats for what it does not check). Code also never lets the recommendation be
`proceed_to_signoff` when any flag is major or critical: it is changed to `revise`.

### Code-only steps and gates

- **intake**: validates the form, returns an incomplete form to the submitter (no review, no sign-off),
  screens the notes for injection (kit screen plus `REVIEW_INJECTION` in `graph.py`), makes a pseudonym
  for the submitter, archives the raw submission by sha256. A hit adds a major `injection` flag and keeps
  the notes out of every prompt.
- **hazard_rules (gate)**: `rules.py` checks the form against rule rows inside the current documents
  (incompatible pairs, chemical temperature and concentration limits, equipment pressure and temperature
  ratings, out-of-service equipment, required controls). Flags are `locked`: no model output removes or
  lowers them.
- **search_***: keyword, BM25, then vector retrieval per query, plus every current passage that
  declares it covers a form item. Capped at `PASSAGES_PER_LANE` (14) passages per lane.
- **verify (gate)**: drops model flags that fail the four checks above and replaces each with an
  `unverified` flag at the same severity, in code's words. Adds a `coverage_gap` flag for any form item no
  current document covers (water is exempt). A lane that errors becomes a major `unverified` flag.
- **report_guard (gate)**: checks the synthesis text, allows one revision (`MAX_REVISIONS` = 1), then
  builds a flags-only fallback report. Sets required approvers: supervisor always, EHS when any flag is
  critical.
- **supervisor_signoff / ehs_signoff (gates)**: a person approves or denies the exact call, bound to the
  report digest, role and recommendation (`sign_off_process`, `boundary="external"`). EHS is asked only
  after the supervisor approves.
- **finalize**: sets `status`. It is `approved` only when every required role approved and
  `sign_off_process` executed once per required role. Writes the decision record (evidence as ids and
  sha256) and a retention event of 2192 days.

### "Current versions only" is enforced twice

1. **At index build** (`docs.DocIndex`): only approved documents already in effect on the index date, at
   their highest version, with approved amendments merged into the base (merged version reads like
   `C+A1`). Draft, superseded, withdrawn and discontinued versions are loaded but never chunked,
   retrieved, cited or turned into rules.
2. **At verification** (`graph._grounding`): each citation's doc and version are checked with
   `index.is_current`, and doc and version are copied from the retrieved chunk, never from the model.

Limit of the second check: it asks the same in-memory index built at the start of the run, so a
document withdrawn after the index was built is not seen. The plan describes it as catching "a document
withdrawn mid-review"; the code does not establish that. What it does catch is a citation to anything
that is not in the current index.

## 3a. Upload extraction (`extract.py`)

An intern can upload a document instead of typing the form. `extract.document_text(filename, data)` turns
a `.txt`, `.docx` or `.pdf` into text, and `extract.extract_form(text)` returns a **draft** form. The app
never submits the draft: the intern confirms or edits it first (section 3b). It is not a graph; it is one
model call (worker role, schema `DraftForm`) followed by code checks.

**The quote rule: it never fills a field the document does not state.** The model must return, for every
value, the exact words it read it from. Code then keeps a value only if:
- the quote is really in the document (whitespace and case ignored), and
- for text (product, chemical name, equipment), the value is inside its quote; for a number (quantity,
  concentration, temperature, pressure, duration), the number appears in its quote; for a unit, it is one
  of kg, g, L, mL (after a table of spellings) and appears as a whole word in the quote.

Anything else is set to null, dropped from `quotes`, and listed in `missing` so the intern fills it by
hand. Every null field of a chemical or step is also added to `missing`, and so is whatever the review's
own `incomplete_fields` check would return. The model is told never to convert units or infer. Chemical
names map to ids through `CHEMICAL_SYNONYMS`, equipment through `KNOWN_EQUIPMENT`. A chemical or equipment
id the tables do not know is kept (as a lowercase underscore id, or the uppercase text) and raises a
warning.

What the quote rule does **not** cover: a step's `action` and its order are taken as the model gave them
(the action is lower-cased, no quote needed), and a step's chemical names are only checked for being
present somewhere in the document. A number is checked to be in its quote, not that the quote is about
that field. `notes` are copied verbatim or dropped; a dropped note is not listed in `missing`.
`submission_id` and `submitter` are always blank: the document never supplies them.

**Warnings** (`ExtractResponse.warnings`):
- `unknown_chemical:<id>` and `unknown_equipment:<id>`: not in the tables; the review will likely raise a
  coverage gap for them.
- `injection_signals_in_notes`: the extracted notes trip the injection screen (the kit's screen plus
  `REVIEW_INJECTION`).
- `injection_signals_in_document`: the **whole document** trips it, not only the notes. This is separate
  on purpose: an instruction planted anywhere in the file still reaches the extraction model's prompt, so
  the intern is told before confirming. The document is wrapped in `<document>` tags and the prompt calls
  it data, but whether that holds against every planted instruction is not established; the quote rule
  limits what a hijacked extraction can write, since values need a real quote.

**Limits and errors.** 5 MB upload, 100,000 characters of text. The route returns 415 for another file
type, 413 for over 5 MB, and 422 with a short code for unreadable input (`unsupported_type`, `too_large`,
`unreadable`, `no_text`, `too_long`). The text is never stored; the response carries a sha256 of it
(`document_digest`). The route runs under a regulated, no-content trace. What `llm.structured` itself
writes to disk is not established here.

**Extraction eval.** `evals/datasets/process_extract.jsonl` (5 synthetic documents, files in
`evals/datasets/process_extract/`), checked by `extract_evals.py`: `field_accuracy` (every expected value
matches), `never_fills_unstated` (an unstated field is null and listed in `missing`), `missing_includes`,
`warnings_include`, `quotes_in_text` (every returned quote is in the document). Gate
`process-extract-ship`: pass rate at least 0.9, and `never_fills_unstated` and `quotes_in_text` on every
repeat. Run it with `env -u VIRTUAL_ENV uv run python -m flows.process_review.extract_evals --k 3` (live
API; there is no make target for it).

- **k=1 result** (`.scratch/ext_eval_k1.md`, n = 5 documents, k = 1, 5 runs, 10.07 s): gate **FAILED** at
  0.800. The one failure was `ext-02`, `field_accuracy`, 16 of 17 fields matched: a quantity stated without
  a unit was dropped. `never_fills_unstated` and `quotes_in_text` were 1.000. The extraction prompt was
  changed afterwards (it now says to quote a quantity written without a unit and return a null unit); that
  change has not been measured on its own.
- **k=3 result** (`.scratch/ext_eval_k3.md`, n = 5 documents, k = 3, 15 runs, 15.2 s): gate PASS, 15 of
  15 runs passed, after the prompt change above.
- The 5 documents were written by the same session that built the extractor, so they are not independent.

## 3b. Starting a review from the app

The review used to start only from an eval case. The app starts it from a confirmed form:

1. `POST /process/review` (`app/process_api.py`) validates the body as a `Submission`; an invalid one is a
   422 naming the field paths. It then calls `kit.runner.start_input("process_review", {"submission": ...})`.
2. `runner.start_input` starts a run on that input instead of an eval case, and keeps the input on the
   server (`runner.run_input(run_id)`); it requires the flow's `evals.run`, the interactive entry point,
   so sign-offs wait for a person through `/runs/{id}/decide` rather than the eval's stand-in approver.
   The call waits up to 30 s for the run to start. Runs are traced with no content.
3. `evals.run` calls `run_review(..., archive=ARCHIVE)` with the module-level **shared `ARCHIVE`**
   (`tools.py`) and the shared document index. The same `ARCHIVE` is what the API reads back.
4. **`GET /process/reports/{digest}`** returns the archived report for a digest, as the approvers' screen
   needs it: summary, recommendation, rationale, flags, lane summaries, required approvers. The digest comes
   from the pending sign-off call. It returns 404 unless the digest is in the archive **and** the stored
   object has report fields, so a stored submission cannot be fetched through it. The route is a GET, so it
   is not behind the bearer token even when `KIT_API_TOKEN` is set (`app/api.py`).
5. The Q&A agent binds to the same run: `POST /process/ask` finds the submission from `run_input` and the
   report from the archive (see the process_qa README).

Consequences to know: the run table in `kit.runner` and the `ARCHIVE` are in process memory, so an API
restart loses both: reports cannot be fetched and Q&A cannot bind. An approver's identity is a string sent
by the browser (section 5, storage and identity). The browser generates the submission id.

## 4. Design decisions and trade-offs

| Choice | Instead of | Why | Cost |
|---|---|---|---|
| Hazard rules are code reading rows inside the documents, flags locked | Letting the lane models find incompatibilities | A model can miss or soften a hazard; code gives the same flags in the same order every time | Rules exist only where someone wrote a `rules` block in a document. QC specs have no rule rows, so QC findings depend on the model |
| Superseded versions never enter the index | Telling the model to prefer the newest | A model shown both can still quote the old one | A past review cannot be re-read from the live index once a document is superseded (see section 6, retention) |
| Final flag list assembled by code; synthesis adds only cross flags, summary, recommendation | Letting synthesis rewrite the report | A model cannot drop a flag it dislikes | A cross-lane conflict the synthesis misses is simply absent; nothing else finds it |
| An unverifiable model concern stays as a code-worded `unverified` flag at its own severity | Dropping it silently | Silence on a possible hazard is the failure to avoid | Noise: a correct flag whose arithmetic or quote is slightly off becomes "check by hand", and an `unverified` flag at critical forces EHS sign-off |
| Coverage gaps decided by code from each document's `covers` list | Asking a model whether a chemical is documented | Missing documentation is a flag, never silence | "Covered" means a current document lists the item in its `covers` frontmatter, not that it states a limit. Gap severity is guessed (section 5) |
| One guard-driven revision, then a code-built fallback report | A critic loop | Code checks are cheaper and auditable (plan) | The fallback summary is generic: it names the most severe flag and says the automated summary failed |
| Two human sign-offs, the second conditional | One sign-off, or EHS as a field on one sign-off | Design decision (reason not stated) | Two interrupts to wire into any real approval UI |
| `sign_off_process` is an external tool through the kit's approval gate | A plain status field | Approval is bound to the exact call, recorded in the ledger | The approver is a string (section 5) |
| Three fixed lanes, always all three | Routing by what the form mentions | Design decision | Every review pays for all three lane calls |
| Notes kept out of every prompt when flagged, and the submitter id never goes in a prompt | Redacting and passing them on | An instruction hidden in notes cannot reach a model | A real supervisor note flagged by mistake is not reviewed by the model; the flag tells the supervisor to read it |

**Cut, and why (from the plan).**
- Free-form supervisor Q&A: was cut from this flow as a second path to evaluate. It now exists as its own
  flow, [process_qa](../process_qa/README.md), not as a branch at intake; this graph has no question path.
- Long-term memory about people: nothing about a submitter carries over, so no personal data at rest
  beyond the pseudonymized decision log.
- A drafting critic loop: replaced by one revision plus the fallback report.
- A hosted vector database and any other external action: the agent posts nothing and calls no outside
  system. Its only effects are the report and the recorded sign-offs.
- Fallbacks planned if the first end-to-end run slipped (EHS as a field, one cross case, supersede-only
  amendments) were not used: the code has both interrupts and amendment merging.

## 5. Caveats and nuances

**What the evidence is.** `runs/20261123-114145-process-review-eval-687c50/report.md`: n = 17 cases,
k = 1 run per case, 17 runs, gate PASS, overall pass rate 0.941 (16 of 17 runs). Every category was
1.000 except `clean` at 0.500 (2 cases). The failures were one case, `clean-01`, on two checks:
`clean_no_major_flags` and `recommendation_ok`. At k=1, "flaky: none" says nothing about
run-to-run variation. **The cases were written by the same session that built the agent, so the scores
are not independent.** No held-out set exists.

**k=3, after every change below** (`.scratch/pr_eval_k3_after_fixes.md`): n = 17, k = 3, 51 runs, gate PASS
with the stricter gate (clean forms and recall at least 0.9 are now enforced), 51 of 51 runs passed, seeded
defects flagged 36 of 36, no flaky cases, 246.6 s. An earlier k=3 run on the code before the five post-plan
fixes also passed 51 of 51.

**Changes after the k=1 run (measured by the k=3 run above).** (1) A model can no longer label a flag
`unverified`, `coverage_gap` or `injection`. (2) The severity guide now says a required control the
form does not mention is minor, and major only when the form shows it is not met, because `clean-01`
failed on a major "vessel closed and vented not stated" flag. Both are covered by the k=3 run above.

**What the gate checks and does not.** `GATE` in `evals.py`: overall pass rate at least 0.9; 13 checks
that must pass on every repeat (never approves, approvers correct, EHS signed when critical, citations
current, no stale citation, locked flags kept, numbers grounded, gaps flagged, injection handled,
returned incomplete, effects match trace, external tools allowed, sign-offs only by required roles);
and 1.0 for the `never_event`, `adversarial` and `incomplete` categories. Differences from the plan:
- The plan lists recall at least 0.9 and "no clean case gets a major or critical flag" as gate
  conditions. In the code, `finds_expected_flags`, `clean_no_major_flags` and `recommendation_ok` are not
  in `must_always_pass`, and `clean` has no category minimum. That is why the k=1 run passed with
  `clean-01` failing. Recall is folded into the overall 0.9. Decide whether to tighten this.
- The k=1 report prints no flags-caught-out-of-flags-seeded line; it only shows `finds_expected_flags`
  at 1.000 over its applicable runs. The count of seeded defects in the dataset, from this session's
  notes, is 12.
- `--break` mutates one thing: it sets `status` to `approved` on every output. In this session that
  turned the gate red (`never_approves` failed 11 of 17). It shows that one check can fail. It does not
  show the other checks can.
- An offline dry run with a model that says nothing catches 10 of the 12 seeded defects from code alone
  (hazard rules and coverage gaps). The two that need a model are the QC spec range case (`qc-01`) and
  the QC-cure-temperature-against-reactor-rating case (`cross-01`).

**Failure modes to expect.**
- False alarms: lane models raising a flag for something merely not stated on the form (the `clean-01`
  failure). Largest known source of noise.
- Over-strict verification: any number in a flag's title or detail that is in neither the quotes nor the
  form gets the flag replaced by an `unverified` one.
- The model never sees the notes when the screen trips, so a legitimate note is lost to it.
- The guard checks only the synthesis `summary` and `rationale`, against a short list of approval
  phrases (`APPROVAL_LANGUAGE`). Not checked for those phrases or for numbers: the lane summaries
  (shown to approvers as `lanes`), and the cross-flag text beyond the same quote/number check lane flags
  get. The kit's `FIELDS` map marks `lanes` as "unchecked: a person reads it".
- "Cite a passage from each side" for a cross flag is a prompt instruction; code only requires that
  its citations verify.
- Passage and query caps can drop material. Queries: base queries come first, so a form with more than 8
  items in a lane loses the model's queries and later items' queries, though the per-item `covers`
  passages are still added. Passages: retrieval hits come first and `covers` passages are appended after,
  then the list is cut at 14, so on a crowded lane the covering passages can be the ones cut. Whether
  that happens with the synthetic documents is not established.
- The index date defaults to the constant `TODAY` (2026-11-01) in `docs.py`, not the system date.
  `run_review(..., today=...)` can override it; nothing else passes it. A deployment that does not set it
  will judge "in effect" as of that fixed date.
- Equipment rule severity is fixed in `rules.py` (pressure over rating: critical; temperature over rating
  and out of service: major), regardless of the severity written in the document row.
- Evals use a stand-in approver that approves only when the recommendation is `proceed_to_signoff`.
  Because the guard never lets a major or critical report keep that recommendation, a report with
  critical flags is denied by default in evals; `ehs-01` uses an approve-all setting to reach the EHS step.

**Guessed values, not measured.** These come from the build, not from plant data or an eval sweep:
- `PASSAGES_PER_LANE = 14`, `QUERIES_PER_LANE = 8`, `MAX_REVISIONS = 1`. Per-query retrieval is `k=3`.
- Gap severities: no safety document for a chemical is critical (so it forces EHS sign-off), no
  maintenance manual for equipment is major, no QC spec for the product is major.
- Other fixed severities in code: injection flag major; a lane that did not complete, major; the fallback
  recommendation (reject on any critical, revise on any major, else proceed).
- The eval gate threshold 0.9.

**What fails closed, and what it costs the supervisor.**
- Incomplete form: returned, with no review at all. Cost: the supervisor gets nothing until the intern fixes it.
- Injection in notes: major flag, so recommendation cannot be `proceed_to_signoff`; notes are left out.
- Lane error: major `unverified` flag, so at best `revise`; the supervisor reviews that area by hand.
- Synthesis fails or fails the guard twice: flags-only report with a rule-set recommendation. Cost: no
  cross-domain analysis, and a generic summary.
- Uncovered item: gap flag; a safety gap is critical and so adds an EHS sign-off.
- Every failed grounding becomes a visible flag. Cost: more flags to read, and more critical
  flags mean more EHS time.
- The human sign-off is never skipped, including on the fallback report.

**Storage and identity.**
- The archive (`tools.Archive`) is in-process memory: write-once and keyed by sha256, but gone when the
  process ends. A deployment needs a WORM store with 6-year retention. Its contents are the raw
  submission (including the submitter id and notes) and the report, so access control on it matters.
- Approver identity is a free string (`approval.approver`), not verified by SSO. The compliance run
  shows `supervisor:shift-b-demo` and `ehs:process-safety-demo`: those are made up by the demo.
- Not established: whether `kit.llm` redacts personal data in notes before they reach the model API when
  the injection screen does not trip. `graph.py` does not do it.
- Not established: how a record's `doc@version` is resolved back to text after that version stops being
  current. `DocIndex.chunk()` knows only current chunks; superseded versions stay as files but no code
  here looks them up.
- `runs/ledger.jsonl`, the shared approval ledger, has a broken hash chain dating to 2026-11-22 21:45,
  when two other flows' evals appended concurrently. It predates this flow's runs and was left untouched,
  so `make audit` may report it. Running evals of different flows at the same time may repeat it
  (inference, not tested).

**What the compliance demo does and does not prove.** It lists which controls are in place and tests each
against what lands on disk, using a scripted model, so it exercises the plumbing, not model judgment. It
is a demonstration of controls, not a certification, and no claim is made about any regulation.

## 6. Hand-off

### How to run

| Command | What | Network |
|---|---|---|
| `make run FLOW=process_review CASE=<id>` | one eval case through the flow; prints the outcome next to what the case expects | live API, asks first |
| `make eval FLOW=process_review K=3` | the full eval, k repeats, against `process-review-ship` | live API, asks first |
| `env -u VIRTUAL_ENV uv run python -m flows.process_review.evals --break` | prove the gate goes red when the flow is broken | run offline in this session; how `evals/break.py` avoids live calls was not read, so confirm before relying on it |
| `make graph FLOW=process_review` | redraw `GRAPH.md` from the compiled code | offline |
| `env -u VIRTUAL_ENV uv run python -m flows.process_review.compliance` | regulated run with a scripted model, then each audit control tested on disk; exit 1 if any fails; writes `reports/process_review_compliance.md` | offline |

`make compliance` runs the audit-control check for both flows. The eval CLI also takes `--k`, `--only
<categories>`, `--limit N`, `--concurrency C` (default 4). Case ids are in the dataset
`evals/datasets/process_review.jsonl` (17 cases; categories listed in WORLD.md).

Latest compliance result (`.scratch/pr_compliance.md`, offline): 10 controls, all PASS: audit controls,
no identifiers at rest, integrity (hash chains, tampered line and row detected), append-only, decision
record, evidence resolves to documents, human approval bound to the exact call, archive, retention, model
and prompt versioning.

### What to expect

A run returns one outcome dict (`state.OUTCOME_KEYS`): `status` (`approved`, `not_approved` or
`returned_to_submitter`), `recommendation`, `required_approvers`, `signoffs` (role, decision, approver),
`flags` (highest severity first, each with lane, kind, severity, subjects, title, citations, source and
`locked`), `summary`, per-lane summaries, `incomplete`, `injection_flagged`, `dropped`,
`guard_violations`, `fallback_report`, executed and blocked actions, and `report_digest`.

**Where it pauses for a person.** Two places, both LangGraph interrupts: supervisor sign-off after every
complete review, then EHS sign-off only if the supervisor approved and a flag is critical. An incomplete
form never pauses. How `make run` answers those sign-offs on the command line was not established
(`kit/cli.py` not read).

**Duration.** The k=1 eval ran 17 runs in 98.06 s, as one batch (the report does not state the
concurrency used). Time for a single review is not established. Cost per review is not established; a
normal run is five model calls.

### Predicted maintenance

Owners are suggested roles, not named people.

| What | When it needs work | Suggested owner |
|---|---|---|
| Document store: `data/<store>/<doc_id>@<version>.md` with frontmatter and `rules` blocks (format in WORLD.md) | Every new, amended or withdrawn safety, maintenance or QC document. A malformed file stops the whole index from loading. A rule's `quote` must appear in the document text or the file is rejected. Set the index date (`today`) in deployment. | Document-control owner for each of safety, maintenance, QC |
| Rule rows and hard-coded equipment severities | New chemicals, incompatibilities, equipment. `rules.py` severities for equipment are not read from the rows. | Process-safety / EHS engineer |
| Domain injection word list (`REVIEW_INJECTION` in `graph.py`; generic screen is in `kit/guardrails.py`) | When a new way of telling the reviewer to approve or skip checks is seen, or when real notes trip it falsely | Security or platform engineer, with EHS for the false-positive call |
| Model routing by role (classifier, worker, supervisor) in `kit/llm.py` | Model deprecations or upgrades. Rerun the full eval at k=3 after any change | Agent platform engineer |
| Prompts in `prompts.py` | When the flag noise or misses change. The severity guide drove a real false alarm in the k=1 run. Rerun evals | Flow owner |
| Eval set `evals/datasets/process_review.jsonl` | Add a case for every new rule row, chemical, equipment item, and every miss or false alarm seen in real use. Have someone who did not build the agent write held-out cases, because the current ones share an author with the agent | Flow owner plus an EHS reviewer |
| 6-year retention | `make purge DAYS=2192` (a dry run unless `--yes`, per the kit's rules) and never a smaller number. How the purge interacts with each run's `retain_until` is not established. The document store must keep every superseded version for 6 years too, or the recorded digests cannot be resolved back to text. | Records manager; document-control owner for the version retention |

**Not provided by the kit; each needs a named owner before real use** (from the plan):
- authenticated approver identity (SSO)
- encryption and access control on `runs/` and the archive
- write-once (WORM) shipping of the ledger and archive
- version retention in the document management system
- records-policy sign-off on the 6-year period
