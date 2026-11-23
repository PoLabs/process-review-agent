interface Props {
  items: string[]
  onPick: (q: string) => void
  label: string
}

export default function Chips({ items, onPick, label }: Props) {
  return (
    <ul className="chips" aria-label={label}>
      {items.map((q) => (
        <li key={q}>
          <button type="button" className="chip" onClick={() => onPick(q)}>
            {q}
          </button>
        </li>
      ))}
    </ul>
  )
}
