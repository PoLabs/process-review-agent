import type { Flag, Recommendation } from '../api'
import FlagList from './FlagList'

const REC: Record<Recommendation, string> = {
  proceed_to_signoff: 'Proceed to sign-off',
  revise: 'Revise',
  reject: 'Reject',
}

interface Props {
  title?: string
  recommendation: Recommendation | null
  summary: string
  rationale?: string
  required: string[]
  lanes: Record<string, string>
  flags: Flag[]
}

export default function ReportPanel({ title, recommendation, summary, rationale, required, lanes, flags }: Props) {
  return (
    <div className="stack">
      {title && <h3>{title}</h3>}
      <p>
        <strong>Recommendation:</strong> {recommendation ? REC[recommendation] : 'none'} ·{' '}
        <strong>Required approvers:</strong> {required.length ? required.join(', ') : 'none'}
      </p>
      <p>{summary}</p>
      {rationale && <p className="muted">{rationale}</p>}
      {Object.entries(lanes).length > 0 && (
        <dl className="lanes">
          {Object.entries(lanes).map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}
      <h4>Flags ({flags.length})</h4>
      <FlagList flags={flags} />
    </div>
  )
}
