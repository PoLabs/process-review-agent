import { useEffect, useState } from 'react'
import type { ExamplesResponse, QAOutcome, RefusalReason, Target } from '../api'
import { ask } from '../api'
import Chips from './Chips'

const MAX = 1000
const REASON: Record<RefusalReason, string> = {
  harmful_intent: 'This question seeks to make something more hazardous or to get around a control, so it was not answered.',
  harm_screen_failed: 'The safety screen for this question could not complete, so it was not answered.',
  injection: 'The question tried to give the assistant instructions, so it was not answered.',
  asks_approval: 'Only the sign-offs can say a process is approved or safe to run. This tool only explains the documents.',
  asks_rewrite: 'It does not write or correct recipes. Change the form and run the review again.',
  other_submission: 'It can only discuss the submission in this session.',
  out_of_scope: 'That is outside the plant documents for this topic.',
  wrong_target: 'That question belongs to a different topic.',
  no_bound_run: 'Run a review first, then you can ask about your process or the report.',
  no_report_yet: 'The report is not ready yet. Ask again when the review has finished.',
  too_long: 'The question is too long. Keep it under 1000 characters.',
}

interface Entry { question: string; target: Target; outcome: QAOutcome }

interface Props {
  examples: ExamplesResponse | null
  runId: string | null
  hasReview: boolean
  seed: { target: Target; question: string; n: number } | null
}

export default function AskPanel({ examples, runId, hasReview, seed }: Props) {
  const [target, setTarget] = useState<Target>('safety')
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<Entry[]>([])
  const [last, setLast] = useState<QAOutcome | null>(null)

  useEffect(() => {
    if (seed) {
      // seeded from a landing-page chip
      setTarget(seed.target)
      setQuestion(seed.question)
    }
  }, [seed])

  const info = (t: Target) => examples?.targets.find((x) => x.id === t)
  const locked = (t: Target) => (t === 'my_process' || t === 'report') && !hasReview

  async function submit(t: Target = target, q: string = question) {
    if (!q.trim() || locked(t)) return
    setBusy(true)
    setError(null)
    try {
      const out = await ask(runId, t, q.trim())
      setLast(out)
      setHistory((h) => [{ question: q.trim(), target: t, outcome: out }, ...h])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel stack" aria-labelledby="ask-h">
      <h2 id="ask-h">Ask a question</h2>
      <p className="muted small">Each question stands alone. Answers quote the documents; they are advice, not approval.</p>
      <div role="group" aria-label="What to ask about" className="chips">
        {(examples?.targets ?? []).map((t) => (
          <button
            key={t.id}
            type="button"
            className={`chip target${target === t.id ? ' selected' : ''}`}
            aria-pressed={target === t.id}
            disabled={locked(t.id)}
            title={locked(t.id) ? 'Needs a review first' : t.description}
            onClick={() => setTarget(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {info(target) && <p className="muted small">{info(target)!.description}</p>}
      {!hasReview && <p className="muted small">“My process” and “The report” unlock once a review exists.</p>}

      <form onSubmit={(e) => { e.preventDefault(); void submit() }}>
        <label htmlFor="q">Your question</label>
        <textarea id="q" rows={3} maxLength={MAX} value={question} onChange={(e) => setQuestion(e.target.value)} />
        <div className="row spread">
          <span className="muted small" aria-live="off">{question.length}/{MAX}</span>
          <button type="submit" className="primary" disabled={busy || !question.trim() || locked(target)}>
            {busy ? 'Asking…' : 'Ask'}
          </button>
        </div>
      </form>
      {info(target) && <Chips label="Example questions" items={info(target)!.questions} onPick={setQuestion} />}

      <div aria-live="polite">
        {error && <p className="error" role="alert">{error}</p>}
        {last && <Outcome o={last} onTarget={(t) => { setTarget(t); }} onExample={(q) => setQuestion(q)} />}
      </div>

      {history.length > 0 && (
        <details>
          <summary>Past questions ({history.length})</summary>
          <ol className="history">
            {history.map((h, i) => (
              <li key={i}>
                <p><strong>{info(h.target)?.label ?? h.target}:</strong> {h.question}</p>
                <Outcome o={h.outcome} compact />
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  )
}

function Outcome({ o, onTarget, onExample, compact }: {
  o: QAOutcome; onTarget?: (t: Target) => void; onExample?: (q: string) => void; compact?: boolean
}) {
  return (
    <div className={`answer ${o.status}`}>
      {o.status === 'answered' && <p>{o.answer}</p>}
      {o.status === 'withheld' && (
        <p>
          An answer was drafted but could not be verified against its sources, so it is withheld. The passages
          it relied on are shown below; please read them directly or ask your supervisor.
        </p>
      )}
      {o.status === 'refused' && (
        <>
          <p><strong>Not answered.</strong> {o.refusal_reason ? REASON[o.refusal_reason] : ''}</p>
          {!compact && o.suggested_target && onTarget && (
            <button type="button" onClick={() => onTarget(o.suggested_target!)}>Ask {o.suggested_target.replace('_', ' ')} instead</button>
          )}
          {!compact && onExample && o.examples.length > 0 && (
            <Chips label="Questions that can be answered" items={o.examples} onPick={onExample} />
          )}
          {o.ehs_notice && <p className="notice">A note was recorded for the EHS team (no question text is stored).</p>}
        </>
      )}
      {o.citations.map((c, i) => (
        <figure key={i} className="cite">
          <figcaption>
            {c.doc_id ? `${c.doc_id} @ ${c.version}` : `${c.kind}: ${c.ref}`}
          </figcaption>
          <blockquote>{c.quote}</blockquote>
        </figure>
      ))}
    </div>
  )
}
