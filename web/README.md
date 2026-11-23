# Process Review Desk (web)

React + TypeScript front end for the `/process` API (Vite, plain CSS, no router or UI library). It is the
product app; the Streamlit UI (`app/ui.py`) stays as the developer and audit console. The agents behind
it: [process_review](../flows/process_review/README.md) (the review, and upload extraction) and
[process_qa](../flows/process_qa/README.md) (scoped questions).

## Verification status (read this first)

- **Done:** an end-to-end run through the dev proxy worked: upload, confirm, review, questions, then
  supervisor and EHS sign-off.
- **Not done:** the React screens were built and they type-check (`npm run build` runs `tsc -b`), but they
  were **not driven in a browser** this session, because the browser extension was not connected. Layout,
  focus order, the accessibility behaviour the markup asks for (labels, `aria-live`, `aria-pressed`), and
  anything that only shows up in a real browser are unverified. There are no front-end tests in this
  folder (no test script in `package.json`).

## Run

1. Start the API on :8000: `make serve` (also starts the Streamlit UI), or
   `env -u VIRTUAL_ENV uv run uvicorn app.api:app --port 8000`.
2. `make web` (which runs `cd web && npm run dev`), then open http://localhost:5173. First time:
   `cd web && npm install`.

The dev server proxies `/api/*` to the API with the `/api` prefix stripped (`vite.config.ts`), so the
browser makes same-origin calls and no CORS setup is needed. The API also allows CORS for
`localhost:5173` and `127.0.0.1:5173` only.

- **`API_URL`** (environment variable, read by `vite.config.ts` when the dev server starts) points the
  proxy elsewhere; default `http://localhost:8000`. Example: `API_URL=http://host:9000 make web`. Restart
  the dev server after changing it. `make serve` sets `API_URL` for Streamlit only, not for this app.
- **`VITE_API_TOKEN`**: if set, it is sent as `Authorization: Bearer <token>` on every non-GET request.
  The API checks it against `KIT_API_TOKEN` when that is set (GET routes are always open). Vite copies
  `VITE_` variables into the JavaScript the browser loads, so the token is visible to anyone who can open
  the app: it keeps casual callers out, it does not authenticate people.

`npm run build` type-checks and builds; `npm run lint` runs oxlint. The app has no production hosting
setup in this folder; only `npm run dev` and `npm run preview` are defined.

## Screens

The header has a **role switcher** (Intern, Supervisor, EHS) and a name field. Intern shows the first
three screens; Supervisor and EHS show only the sign-off screen.

| Screen | What it does | API calls |
|---|---|---|
| **Landing** (Intern) | Upload (.pdf, .docx, .txt, up to 5 MB, checked in the browser first) or "fill in the form by hand". Example workflows, example questions per target, and the list of what it won't answer. Clicking an example for My process or The report only shows a hint, because those need a review first. | `GET /process/examples`, `POST /process/extract` (multipart field `file`) |
| **Form** (Intern) | The draft form, editable. A field the extractor could not back with a quote is marked "check this"; a field it did fill shows the quote it came from. Extraction warnings are listed above the form. "Run review" validates in the browser (product, submitter, each chemical's id, quantity and unit, each step's equipment) and then starts the review. The browser makes the submission id (`SUB-` plus six random digits) and the Submitter field starts as the header's name (editable; a document's own submitter is never read). | `POST /process/review` |
| **Review** (Intern) | Polls the run every 1.5 s: current step, a progress list, then the report while it waits for sign-off, then the outcome and the sign-offs recorded. Fields to fix are shown if the form came back incomplete. Beside it, the **Ask panel**: target buttons, a question box (1000 characters), example questions, answer or refusal with its citations, past questions on screen. | `GET /runs/{id}`, `GET /runs/{id}/events?since=`, `GET /process/reports/{digest}`, `POST /process/ask` |
| **Sign-off** (Supervisor, EHS) | Lists every `process_review` run waiting for your role, shows the exact report, Approve or Reject. Refreshes every 3 s. The EHS list only fills after a supervisor approves a report with a critical flag. | `GET /runs`, `GET /runs/{id}`, `GET /process/reports/{digest}`, `POST /runs/{id}/decide` |

Details that matter:
- **Ask without a review.** From the landing page, a question about Safety, Maintenance or Quality control
  opens the Ask panel alone (no run id). My process and The report are disabled until a review exists
  (waiting or done).
- **Questions stand alone.** Nothing in the browser or the server carries earlier questions into the next
  one; "Past questions" is only a list on the screen.
- **Reject** sends the decision `denied`.
- **Reload loses your place.** The current run id lives only in React state. After a reload the review
  screen cannot be reopened, though the run continues on the server and a sign-off screen can still find it.
- **The sign-off screen is not filtered to one user.** It shows every waiting review on the server for that
  role, which is what an approver needs and also means anyone who selects the role sees them.
- If the API restarts, runs and the report archive (both in memory) are gone, and screens show the error.

## Identity (demo caveat)

The role switcher and name field are a demo stand-in. Nothing here authenticates anyone: anyone can pick
any role. The approver recorded on a decision is the text `<role>:<name>` (or `<role>:demo` with no name),
assembled in the browser (`SignOff.tsx`), and the page shows a "Demo identity" banner. A real deployment
would put SSO in front and derive the role and approver from it; that is a kit gap with an owner still to
be named.
