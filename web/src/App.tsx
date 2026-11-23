import { useEffect, useState } from 'react'
import type { ExamplesResponse, ExtractResponse, Target } from './api'
import { getExamples, startReview } from './api'
import AskPanel from './components/AskPanel'
import FormScreen, { draftFromExtract, emptyDraft } from './components/FormScreen'
import Header from './components/Header'
import type { Role } from './components/Header'
import Landing from './components/Landing'
import ReviewScreen from './components/ReviewScreen'
import SignOff from './components/SignOff'

type View = 'landing' | 'form' | 'review' | 'ask'
type Seed = { target: Target; question: string; n: number }

export default function App() {
  const [role, setRole] = useState<Role>('intern')
  const [name, setName] = useState('')
  const [view, setView] = useState<View>('landing')
  const [examples, setExamples] = useState<ExamplesResponse | null>(null)
  const [examplesError, setExamplesError] = useState<string | null>(null)
  const [extracted, setExtracted] = useState<ExtractResponse | null>(null)
  const [formKey, setFormKey] = useState(0)
  const [runId, setRunId] = useState<string | null>(null)
  const [unknownIds, setUnknownIds] = useState<string[]>([])
  const [seed, setSeed] = useState<Seed | null>(null)

  useEffect(() => {
    getExamples().then(setExamples).catch((e: Error) => setExamplesError(e.message))
  }, [])

  const submitter = name.trim()

  function openForm(x: ExtractResponse | null) {
    setExtracted(x)
    setFormKey((k) => k + 1)
    setView('form')
  }

  function ask(target: Target, question: string) {
    setSeed({ target, question, n: Date.now() })
    setView(runId ? 'review' : 'ask')
  }

  async function submit(sub: Parameters<typeof startReview>[0]) {
    const { run_id, unknown_ids } = await startReview(sub)
    setRunId(run_id)
    setUnknownIds(unknown_ids ?? [])
    setView('review')
  }

  return (
    <>
      <Header role={role} name={name} onRole={setRole} onName={setName} onHome={() => setView('landing')} />
      <main>
        {role !== 'intern' ? (
          <SignOff role={role} name={name} />
        ) : (
          <>
            {runId && view !== 'review' && (
              <p><button type="button" onClick={() => setView('review')}>Back to the current review</button></p>
            )}
            {view === 'landing' && (
              <Landing
                examples={examples}
                examplesError={examplesError}
                onExtracted={openForm}
                onManual={() => openForm(null)}
                onAsk={ask}
              />
            )}
            {view === 'form' && (
              <FormScreen
                key={formKey}
                initial={extracted ? draftFromExtract(extracted, submitter) : emptyDraft(submitter)}
                missing={extracted?.missing ?? []}
                quotes={extracted?.quotes ?? {}}
                warnings={extracted?.warnings ?? []}
                onSubmit={submit}
              />
            )}
            {view === 'review' && runId && <ReviewScreen runId={runId} examples={examples} seed={seed} unknownIds={unknownIds} />}
            {view === 'ask' && (
              <div className="narrow">
                <AskPanel examples={examples} runId={null} hasReview={false} seed={seed} />
              </div>
            )}
          </>
        )}
      </main>
    </>
  )
}
