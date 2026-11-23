import type { Flag } from '../api'

export default function FlagList({ flags }: { flags: Flag[] }) {
  if (!flags.length) return <p className="muted">No flags.</p>
  return (
    <ul className="flags">
      {flags.map((f, i) => (
        <li key={i} className={`flag sev-${f.severity}`}>
          <div className="flag-head">
            <span className={`badge sev-${f.severity}`}>{f.severity}</span>
            <span className="tag">{f.lane}</span>
            <span className="tag">{f.kind.replace(/_/g, ' ')}</span>
            {f.locked && <span className="tag rule" title="A fixed rule: no model can remove or downgrade it">rule</span>}
          </div>
          <h4>{f.title}</h4>
          {f.detail && <p>{f.detail}</p>}
          {f.citations.map((c, j) => (
            <figure key={j} className="cite">
              <figcaption>
                {c.doc_id} @ {c.version}
              </figcaption>
              <blockquote>{c.quote}</blockquote>
            </figure>
          ))}
        </li>
      ))}
    </ul>
  )
}
