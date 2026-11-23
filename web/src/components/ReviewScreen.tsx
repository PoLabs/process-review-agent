import { useEffect, useRef, useState } from 'react'
import type { ExamplesResponse, ReportView, RunStatus, Target } from '../api'
import { getEvents, getReport, getRun } from '../api'
import AskPanel from './AskPanel'
import ReportPanel from './ReportPanel'

interface Props {
  runId: string
  examples: ExamplesResponse | null
  seed: { target: Target; question: string; n: number } | null
  unknownIds?: string[]
}

const STATUS_TEXT: Record<string, string> = {
  approved: 'Approved: every required sign-off was given.',
  not_approved: 'Not approved.',
  returned_to_submitter: 'Returned to the submitter: the form was incomplete.',
}

export default function ReviewScreen({ runId, examples, seed, unknownIds = [] }: Props) {
  const [run, setRun] = useState<RunStatus | null>(null)
  const [nodes, setNodes] = useState<string[]>([])
  const [report, setReport] = useState<ReportView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const since = useRef(0)
  const fetchedDigest = useRef<string | null>(null)

  useEffect(() => {
    let stopped = false
    async function tick() {
      try {
        const r = await getRun(runId)
        if (stopped) return
        setRun(r)
        setError(null)
        const ev = await getEvents(runId, since.current)
        if (stopped) return
        since.current += ev.length
        const names = ev.map((e) => (e.node ?? e.name) as string | undefined).filter((n): n is string => !!n)
        if (names.length) setNodes((p) => [...p, ...names.filter((n, i) => n !== (i ? names[i - 1] : p[p.length - 1]))])
        if (r.status === 'waiting') {
          const digest = r.pending.find((p) => p.input.report_digest)?.input.report_digest
          if (digest && fetchedDigest.current !== digest) {
            fetchedDigest.current = digest
            const rep = await getReport(digest)
            if (!stopped) setReport(rep)
          }
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message)
      }
    }
    void tick()
    const t = setInterval(() => {
      if (run?.status === 'done' || run?.status === 'failed') return
      void tick()
    }, 1500)
    return () => {
      stopped = true
      clearInterval(t)
    }
    // run?.status is read only to stop polling; restarting on status change is fine
  }, [runId, run?.status])

  const status = run?.status ?? 'running'
  const waitingRole = run?.pending.find((p) => p.input.role)?.input.role
  const out = run?.output
  const hasReview = status === 'waiting' || status === 'done'

  return (
    <div className="review-layout">
      <div className="stack">
        <section className="panel stack" aria-labelledby="rv-h">
          <h1 id="rv-h">Review</h1>
          <p className="muted small">Run {runId}</p>
          {unknownIds.length > 0 && (
            <p className="notice" role="status">
              Not recognised as plant chemicals or equipment: {unknownIds.join(', ')}. The review treats
              each as uncovered by any document. If one is a typo, fix the form and run the review again.
            </p>
          )}
          <div role="status" aria-live="polite">
            {status === 'running' && <p>Reviewing… {nodes.length > 0 && <>current step: <strong>{nodes[nodes.length - 1].replace(/_/g, ' ')}</strong></>}</p>}
            {status === 'waiting' && <p><strong>Waiting for {waitingRole === 'ehs' ? 'EHS' : (waitingRole ?? 'supervisor')} sign-off.</strong></p>}
            {status === 'done' && out && <p><strong>{STATUS_TEXT[out.status] ?? out.status}</strong></p>}
            {status === 'failed' && <p className="error">The review failed: {run?.error ?? 'unknown error'}</p>}
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          {nodes.length > 0 && (
            <details>
              <summary>Progress ({nodes.length} steps)</summary>
              <ol className="progress">{nodes.map((n, i) => <li key={i}>{n.replace(/_/g, ' ')}</li>)}</ol>
            </details>
          )}

          {status === 'waiting' && report && (
            <ReportPanel
              recommendation={report.recommendation}
              summary={report.summary}
              rationale={report.rationale}
              required={report.required_approvers}
              lanes={report.lanes}
              flags={report.flags}
            />
          )}
          {status === 'done' && out && (
            <>
              {out.incomplete?.length > 0 && (
                <div className="notice"><strong>Fields to fix:</strong> {out.incomplete.join(', ')}</div>
              )}
              <ReportPanel
                recommendation={out.recommendation}
                summary={out.summary}
                required={out.required_approvers ?? []}
                lanes={out.lanes ?? {}}
                flags={out.flags ?? []}
              />
              <h3>Sign-offs</h3>
              {out.signoffs?.length ? (
                <ul>{out.signoffs.map((s, i) => <li key={i}>{s.role}: {s.decision} ({s.approver})</li>)}</ul>
              ) : (
                <p className="muted">No sign-offs recorded.</p>
              )}
            </>
          )}
        </section>
      </div>
      <AskPanel examples={examples} runId={runId} hasReview={hasReview} seed={seed} />
    </div>
  )
}
