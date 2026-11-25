import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '@/shared/api/axios'
import { UCKC_PHASES } from '@/shared/config/mitre'

export const Login = () => {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setErr('')

    try {
      const body = new URLSearchParams({ username: email, password })
      await api.post('/auth/access-token', body, {
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
      })
      // session lives in HttpOnly cookies set by the backend, no storage here

      navigate('/', { replace: true })
    } catch (err) {
      const detail = err.response?.data?.detail

      if (Array.isArray(detail)) {
        setErr(detail.map((e) => e.msg).join(', '))
      } else {
        setErr(detail || 'Sign in failed')
      }
    }
  }

  return (
    <div className="flex min-h-screen bg-app text-primary">
      <aside className="hidden w-[38%] max-w-[520px] shrink-0 flex-col justify-between border-r border-subtle bg-nav p-8 lg:flex">
        <div>
          <div className="text-[18px] font-medium tracking-[0.02em] text-primary">EAGLE</div>
          <p className="mt-3 max-w-[34ch] text-[13px] leading-relaxed text-secondary">
            Emulated attack generator for replaying Unified Kill Chain stages on a lab range
          </p>
        </div>

        <div>
          <div className="mb-3 font-mono text-[11px] text-muted">18 stages · one chain</div>
          <ol className="grid grid-cols-2 gap-x-6 gap-y-1.5">
            {UCKC_PHASES.map((phase) => (
              <li
                key={phase.id}
                className="flex items-baseline gap-2 font-mono text-[11px] text-muted"
              >
                <span className="tabular-nums text-strong">
                  {String(phase.order).padStart(2, '0')}
                </span>
                <span className="truncate">{phase.name}</span>
              </li>
            ))}
          </ol>
        </div>
      </aside>

      <main className="flex flex-1 items-center justify-center px-6">
        <div className="w-full max-w-[360px]">
          <h1 className="text-[20px] font-medium text-primary">Sign in</h1>
          <p className="mt-1 text-[12px] text-muted">
            Use the account created by your administrator
          </p>

          <form onSubmit={handleSubmit} className="mt-8 flex flex-col gap-4">
            <div>
              <label htmlFor="email" className="mb-1.5 block text-[11px] text-muted">
                Email
              </label>
              <input
                type="email"
                id="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="username"
                required
                className="h-9 w-full rounded-md border border-subtle bg-input px-3 text-[13px] text-primary outline-none transition-colors focus:border-accent"
              />
            </div>

            <div>
              <label htmlFor="password" className="mb-1.5 block text-[11px] text-muted">
                Password
              </label>
              <input
                type="password"
                id="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                className="h-9 w-full rounded-md border border-subtle bg-input px-3 text-[13px] text-primary outline-none transition-colors focus:border-accent"
              />
            </div>

            {err && <p className="text-[12px] text-danger">{err}</p>}

            <button
              type="submit"
              className="mt-2 h-9 w-full rounded-md bg-accent text-[13px] font-medium text-app transition-colors hover:bg-accent/90"
            >
              Sign in
            </button>
          </form>

          <p className="mt-6 text-[11px] text-muted">
            Lost access? Ask an administrator to reset the password
          </p>
        </div>
      </main>
    </div>
  )
}
