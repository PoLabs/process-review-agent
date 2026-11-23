import { useCallback, useEffect, useState } from 'react'
import type { ReportView } from '../api'
import { decide, getReport, getRun, listRuns } from '../api'
import type { Role } from './Header'
import ReportPanel from './ReportPanel'

interface Item {
  runId: string
  toolUseId: string
  digest: string
  report: ReportView | null
  error: string | null
}

export default function SignOff({ role, name }: { role: Exclude<Role, 'intern'>; name: string }) {
  const [items, setItems] = useState<Item[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const runs = (await listRuns()).filter((r) => r.flow === 'process_review' && r.status === 'waiting')
      const found: Item[] = []
      for (const r of runs) {
        const full = await getRun(r.id)
        for (const p of full.pending ?? []) {
          if (p.input.role !== role || !p.input.report_digest) continue
          let report: ReportView | null = null
          let err: string | null = null
          try {
            report = await getReport(p.input.report_digest)
          } catch (e) {
            err = (e as Error).message
          }
          found.push({ runId: r.id, toolUseId: p.tool_use_id, digest: p.input.report_digest, report, error: err })
        }
      }
      setItems(found)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoaded(true)
    }
  }, [role])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), 3000)
    return () => clearInterval(t)
  }, [refresh])

  async function act(it: Item, decision: 'approved' | 'denied') {
    setBusy(it.toolUseId)
    setMsg(null)
    try {
      await decide(it.runId, it.toolUseId, decision, `${role}:${name || 'demo'}`)
      setMsg(decision === 'approved' ? 'Approved. Recorded.' : 'Rejected. Recorded.')
      await refresh()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="stack">
      <h1>Sign-off ({role === 'ehs' ? 'EHS' : 'Supervisor'})</h1>
      <p className="muted">Reports waiting for your decision. Your decision is bound to the exact report shown.</p>
      <p role="status" aria-live="polite">{msg}</p>
      {error && <p className="error" role="alert">{error}</p>}
      {loaded && !items.length && !error && <p className="panel">Nothing is waiting for {role === 'ehs' ? 'EHS' : 'supervisor'} sign-off.</p>}
      {items.map((it) => (
        <section key={it.toolUseId} className="panel" aria-label={`Report for run ${it.runId}`}>
          <p className="muted small">Run {it.runId}</p>
          {it.report ? (
            <ReportPanel
              title={it.report.product}
              recommendation={it.report.recommendation}
              summary={it.report.summary}
              rationale={it.report.rationale}
              required={it.report.required_approvers}
              lanes={it.report.lanes}
              flags={it.report.flags}
            />
          ) : (
            <p className="error">Report unavailable: {it.error}</p>
          )}
          <div className="row">
            <button type="button" className="primary" disabled={!it.report || busy === it.toolUseId} onClick={() => act(it, 'approved')}>
              Approve
            </button>
            <button type="button" className="danger" disabled={!it.report || busy === it.toolUseId} onClick={() => act(it, 'denied')}>
              Reject
            </button>
          </div>
        </section>
      ))}
    </div>
  )
}
