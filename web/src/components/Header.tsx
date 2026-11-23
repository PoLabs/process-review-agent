export type Role = 'intern' | 'supervisor' | 'ehs'
export const ROLE_LABEL: Record<Role, string> = { intern: 'Intern', supervisor: 'Supervisor', ehs: 'EHS' }

interface Props {
  role: Role
  name: string
  onRole: (r: Role) => void
  onName: (n: string) => void
  onHome: () => void
}

export default function Header({ role, name, onRole, onName, onHome }: Props) {
  return (
    <header className="app-header">
      <div className="bar">
        <button type="button" className="brand" onClick={onHome}>
          Process Review Desk
        </button>
        <div className="identity">
          <label>
            Role{' '}
            <select value={role} onChange={(e) => onRole(e.target.value as Role)}>
              {(Object.keys(ROLE_LABEL) as Role[]).map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Name{' '}
            <input value={name} onChange={(e) => onName(e.target.value)} placeholder="EMP-012" autoComplete="off" />
          </label>
        </div>
      </div>
      <p className="demo-banner">Demo identity — a real deployment uses SSO</p>
    </header>
  )
}
