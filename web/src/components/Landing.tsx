import { useRef, useState } from 'react'
import type { ExamplesResponse, ExtractResponse, Target } from '../api'
import { extract } from '../api'
import Chips from './Chips'

const MAX_BYTES = 5 * 1024 * 1024
const NEEDS_REVIEW: Target[] = ['my_process', 'report']

interface Props {
  examples: ExamplesResponse | null
  examplesError: string | null
  onExtracted: (r: ExtractResponse) => void
  onManual: () => void
  onAsk: (target: Target, question: string) => void
}

export default function Landing({ examples, examplesError, onExtracted, onManual, onAsk }: Props) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hint, setHint] = useState<string | null>(null)

  async function onFile(file: File | undefined) {
    if (!file) return
    setError(null)
    if (!/\.(pdf|docx|txt)$/i.test(file.name)) return setError('Use a .pdf, .docx or .txt file.')
    if (file.size > MAX_BYTES) return setError('That file is over 5 MB.')
    setBusy(true)
    try {
      onExtracted(await extract(file))
    } catch (e) {
      setError(`Could not read the document: ${(e as Error).message}`)
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  return (
    <div className="stack">
      <section className="panel">
        <h1>Review a formulation or process</h1>
        <p>
          This desk reviews a formulation or process against the plant's current safety, maintenance and quality-control
          documents. The agent recommends and shows its sources; people approve.
        </p>
        <div className="row">
          <input
            ref={fileRef}
            id="doc-file"
            type="file"
            accept=".pdf,.docx,.txt"
            className="visually-hidden"
            onChange={(e) => onFile(e.target.files?.[0])}
          />
          <button type="button" className="primary big" disabled={busy} onClick={() => fileRef.current?.click()}>
            Upload formulation or process document
          </button>
          <button type="button" className="big" onClick={onManual}>
            Fill in the form by hand
          </button>
        </div>
        <p className="muted small">PDF, Word or text, up to 5 MB. You confirm everything before the review runs.</p>
        <p role="status" aria-live="polite">
          {busy && 'Reading the document…'}
        </p>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      {examplesError && <p className="error" role="alert">Examples unavailable: {examplesError}</p>}

      {examples && (
        <>
          <section aria-labelledby="wf-h">
            <h2 id="wf-h">How it works</h2>
            <div className="cards">
              {examples.workflows.map((w) => (
                <article key={w.title} className="panel">
                  <h3>{w.title}</h3>
                  <p className="muted">{w.description}</p>
                  <ol>
                    {w.steps.map((s) => (
                      <li key={s}>{s}</li>
                    ))}
                  </ol>
                </article>
              ))}
            </div>
          </section>

          <section className="panel" aria-labelledby="q-h">
            <h2 id="q-h">Example questions</h2>
            {hint && <p className="notice" role="status">{hint}</p>}
            {examples.targets.map((t) => (
              <div key={t.id}>
                <h3>{t.label}</h3>
                <p className="muted small">{t.description}</p>
                <Chips
                  label={`${t.label} questions`}
                  items={t.questions}
                  onPick={(q) =>
                    NEEDS_REVIEW.includes(t.id)
                      ? setHint(
                          `“${t.label}” questions need a review first. Upload a document or fill in the form, run the review, then ask.`,
                        )
                      : onAsk(t.id, q)
                  }
                />
              </div>
            ))}
          </section>

          <section className="panel" aria-labelledby="wont-h">
            <h2 id="wont-h">What it won't answer</h2>
            <ul>
              {examples.wont_answer.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  )
}
