import { useState } from 'react'
import type { ExtractResponse, Submission, Unit } from '../api'

const UNITS: Unit[] = ['kg', 'g', 'L', 'mL']

interface ChemDraft { id: string; quantity: string; unit: string; concentration_pct: string }
interface StepDraft {
  action: string; equipment: string; chemicals: string; temp_c: string; pressure_bar: string; duration_min: string
}
interface Draft { product: string; submitter: string; chemicals: ChemDraft[]; steps: StepDraft[]; notes: string }

const s = (v: unknown) => (v === null || v === undefined ? '' : String(v))
const num = (v: string): number | null => (v.trim() === '' || Number.isNaN(Number(v)) ? null : Number(v))
const newId = () => `SUB-${String(Math.floor(Math.random() * 1_000_000)).padStart(6, '0')}`

export function emptyDraft(submitter: string): Draft {
  return {
    product: '',
    submitter,
    chemicals: [{ id: '', quantity: '', unit: '', concentration_pct: '' }],
    steps: [{ action: '', equipment: '', chemicals: '', temp_c: '', pressure_bar: '', duration_min: '' }],
    notes: '',
  }
}

export function draftFromExtract(x: ExtractResponse, submitter: string): Draft {
  const f = x.form as Record<string, unknown>
  const chems = (Array.isArray(f.chemicals) ? f.chemicals : []) as Record<string, unknown>[]
  const steps = (Array.isArray(f.steps) ? f.steps : []) as Record<string, unknown>[]
  const d = emptyDraft(s(f.submitter) || submitter)
  d.product = s(f.product)
  d.notes = s(f.notes)
  if (chems.length)
    d.chemicals = chems.map((c) => ({
      id: s(c.id), quantity: s(c.quantity), unit: s(c.unit), concentration_pct: s(c.concentration_pct),
    }))
  if (steps.length)
    d.steps = steps.map((t) => ({
      action: s(t.action),
      equipment: s(t.equipment),
      chemicals: Array.isArray(t.chemicals) ? t.chemicals.join(', ') : '',
      temp_c: s(t.temp_c),
      pressure_bar: s(t.pressure_bar),
      duration_min: s(t.duration_min),
    }))
  return d
}

interface Props {
  initial: Draft
  missing: string[]
  quotes: Record<string, string>
  warnings: string[]
  onSubmit: (sub: Submission) => Promise<void>
}

export default function FormScreen({ initial, missing, quotes, warnings, onSubmit }: Props) {
  const [d, setD] = useState<Draft>(initial)
  const [errors, setErrors] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [apiError, setApiError] = useState<string | null>(null)

  const miss = new Set(missing)
  const upd = (patch: Partial<Draft>) => setD((p) => ({ ...p, ...patch }))
  const updChem = (i: number, patch: Partial<ChemDraft>) =>
    upd({ chemicals: d.chemicals.map((c, j) => (j === i ? { ...c, ...patch } : c)) })
  const updStep = (i: number, patch: Partial<StepDraft>) =>
    upd({ steps: d.steps.map((c, j) => (j === i ? { ...c, ...patch } : c)) })

  // A field's marker: highlighted border, a text cue, and the document quote as a tooltip + described-by text.
  function mark(path: string) {
    const isMissing = miss.has(path)
    const quote = quotes[path]
    return {
      isMissing,
      quote,
      props: {
        className: isMissing ? 'check' : undefined,
        title: quote ? `From the document: “${quote}”` : undefined,
        'aria-describedby': isMissing || quote ? `d-${path}` : undefined,
      },
      note:
        isMissing || quote ? (
          <span id={`d-${path}`} className="field-note">
            {isMissing && <strong className="check-text">check this</strong>}
            {quote && <span className="quote"> From the document: “{quote}”</span>}
          </span>
        ) : null,
    }
  }

  async function run() {
    const errs: string[] = []
    if (!d.product.trim()) errs.push('Product is required.')
    if (!d.submitter.trim()) errs.push('Submitter is required.')
    d.chemicals.forEach((c, i) => {
      if (!c.id.trim()) errs.push(`Chemical ${i + 1}: id is required.`)
      if (num(c.quantity) === null) errs.push(`Chemical ${i + 1}: quantity is required.`)
      if (!c.unit) errs.push(`Chemical ${i + 1}: unit is required.`)
    })
    d.steps.forEach((t, i) => {
      if (!t.equipment.trim()) errs.push(`Step ${i + 1}: equipment is required.`)
    })
    setErrors(errs)
    if (errs.length) return
    const sub: Submission = {
      submission_id: newId(),
      product: d.product.trim(),
      submitter: d.submitter.trim(),
      chemicals: d.chemicals.map((c) => ({
        id: c.id.trim(),
        quantity: num(c.quantity),
        unit: c.unit as Unit,
        concentration_pct: num(c.concentration_pct),
      })),
      steps: d.steps.map((t, i) => ({
        n: i + 1,
        action: t.action.trim(),
        equipment: t.equipment.trim(),
        chemicals: t.chemicals.split(',').map((x) => x.trim()).filter(Boolean),
        temp_c: num(t.temp_c),
        pressure_bar: num(t.pressure_bar),
        duration_min: num(t.duration_min),
      })),
      notes: d.notes,
    }
    setBusy(true)
    setApiError(null)
    try {
      await onSubmit(sub)
    } catch (e) {
      setApiError((e as Error).message)
      setBusy(false)
    }
  }

  const product = mark('product')
  const submitter = mark('submitter')
  const notes = mark('notes')

  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); void run() }} noValidate>
      <h1>Confirm the form</h1>
      <p className="muted">Check every field. Fields marked “check this” were not stated clearly in the document.</p>
      {warnings.length > 0 && (
        <div className="notice" role="status">
          <strong>Warnings</strong>
          <ul>{warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </div>
      )}

      <section className="panel grid2">
        <div className="field">
          <label htmlFor="f-product">Product</label>
          <input id="f-product" value={d.product} onChange={(e) => upd({ product: e.target.value })} {...product.props} />
          {product.note}
        </div>
        <div className="field">
          <label htmlFor="f-submitter">Submitter</label>
          <input id="f-submitter" value={d.submitter} placeholder="EMP-012" onChange={(e) => upd({ submitter: e.target.value })} {...submitter.props} />
          {submitter.note}
        </div>
      </section>

      <section className="panel">
        <h2>Chemicals</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Id</th><th>Quantity</th><th>Unit</th><th>Concentration %</th><th><span className="visually-hidden">Remove</span></th></tr>
            </thead>
            <tbody>
              {d.chemicals.map((c, i) => {
                const id = mark(`chemicals.${i}.id`), q = mark(`chemicals.${i}.quantity`)
                const u = mark(`chemicals.${i}.unit`), cp = mark(`chemicals.${i}.concentration_pct`)
                return (
                  <tr key={i}>
                    <td><input aria-label={`Chemical ${i + 1} id`} value={c.id} onChange={(e) => updChem(i, { id: e.target.value })} {...id.props} />{id.note}</td>
                    <td><input aria-label={`Chemical ${i + 1} quantity`} inputMode="decimal" value={c.quantity} onChange={(e) => updChem(i, { quantity: e.target.value })} {...q.props} />{q.note}</td>
                    <td>
                      <select aria-label={`Chemical ${i + 1} unit`} value={c.unit} onChange={(e) => updChem(i, { unit: e.target.value })} {...u.props}>
                        <option value="">—</option>
                        {UNITS.map((x) => <option key={x}>{x}</option>)}
                      </select>{u.note}
                    </td>
                    <td><input aria-label={`Chemical ${i + 1} concentration percent`} inputMode="decimal" value={c.concentration_pct} onChange={(e) => updChem(i, { concentration_pct: e.target.value })} {...cp.props} />{cp.note}</td>
                    <td><button type="button" onClick={() => upd({ chemicals: d.chemicals.filter((_, j) => j !== i) })} aria-label={`Remove chemical ${i + 1}`}>Remove</button></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <button type="button" onClick={() => upd({ chemicals: [...d.chemicals, { id: '', quantity: '', unit: '', concentration_pct: '' }] })}>Add chemical</button>
      </section>

      <section className="panel">
        <h2>Steps</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>#</th><th>Action</th><th>Equipment</th><th>Chemicals (ids, comma separated)</th><th>Temp °C</th><th>Pressure bar</th><th>Duration min</th><th><span className="visually-hidden">Remove</span></th></tr>
            </thead>
            <tbody>
              {d.steps.map((t, i) => {
                const f = (k: string) => mark(`steps.${i}.${k}`)
                const a = f('action'), e = f('equipment'), ch = f('chemicals'), tc = f('temp_c'), pb = f('pressure_bar'), du = f('duration_min')
                return (
                  <tr key={i}>
                    <td>{i + 1}</td>
                    <td><input aria-label={`Step ${i + 1} action`} value={t.action} onChange={(ev) => updStep(i, { action: ev.target.value })} {...a.props} />{a.note}</td>
                    <td><input aria-label={`Step ${i + 1} equipment`} value={t.equipment} onChange={(ev) => updStep(i, { equipment: ev.target.value })} {...e.props} />{e.note}</td>
                    <td><input aria-label={`Step ${i + 1} chemicals`} value={t.chemicals} onChange={(ev) => updStep(i, { chemicals: ev.target.value })} {...ch.props} />{ch.note}</td>
                    <td><input aria-label={`Step ${i + 1} temperature`} inputMode="decimal" value={t.temp_c} onChange={(ev) => updStep(i, { temp_c: ev.target.value })} {...tc.props} />{tc.note}</td>
                    <td><input aria-label={`Step ${i + 1} pressure`} inputMode="decimal" value={t.pressure_bar} onChange={(ev) => updStep(i, { pressure_bar: ev.target.value })} {...pb.props} />{pb.note}</td>
                    <td><input aria-label={`Step ${i + 1} duration`} inputMode="decimal" value={t.duration_min} onChange={(ev) => updStep(i, { duration_min: ev.target.value })} {...du.props} />{du.note}</td>
                    <td><button type="button" onClick={() => upd({ steps: d.steps.filter((_, j) => j !== i) })} aria-label={`Remove step ${i + 1}`}>Remove</button></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <button type="button" onClick={() => upd({ steps: [...d.steps, { action: '', equipment: '', chemicals: '', temp_c: '', pressure_bar: '', duration_min: '' }] })}>Add step</button>
      </section>

      <section className="panel field">
        <label htmlFor="f-notes">Notes</label>
        <textarea id="f-notes" rows={3} value={d.notes} onChange={(e) => upd({ notes: e.target.value })} {...notes.props} />
        {notes.note}
      </section>

      {errors.length > 0 && (
        <div className="error-box" role="alert">
          <strong>Fix these before running the review</strong>
          <ul>{errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {apiError && <p className="error" role="alert">{apiError}</p>}
      <div><button type="submit" className="primary big" disabled={busy}>{busy ? 'Starting…' : 'Run review'}</button></div>
    </form>
  )
}
