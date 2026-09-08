import { useState, type FormEvent } from 'react'
import { Button } from '@/components/Button'
import { ApiError, refusalText } from '@/lib/api/auth'
import { useAuth } from '@/state/auth'
import { AuthPage, Field, FootLink, Notice, Refusal, RelayNotice } from './parts'

export function LoginPage() {
  const notice = useAuth((s) => s.notice)
  const relay = useAuth((s) => s.relay)
  const { signIn, goRegister } = useAuth((s) => s.actions)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await signIn(username.trim(), password)
    } catch (caught) {
      // The relay's wording is "invalid or expired", which is true of a token
      // and not of a password: at this form the only thing wrong is the pair.
      setError(
        caught instanceof ApiError && caught.status === 401
          ? 'That username and password do not match.'
          : refusalText(caught),
      )
      setBusy(false)
    }
  }

  return (
    <AuthPage
      title="Sign in"
      sub="local account · invite only"
      foot={
        <>
          <span>Have an invitation?</span>
          <FootLink onClick={goRegister}>Register →</FootLink>
        </>
      }
    >
      {notice && <Notice tone="gold">{notice}</Notice>}
      <RelayNotice relay={relay} />
      <form onSubmit={submit} noValidate>
        <Field name="Username">
          <input
            className="trk-input"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field name="Password">
          <input
            className="trk-input"
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        {error && <Refusal>{error}</Refusal>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
          <Button
            type="submit"
            variant="accent"
            disabled={busy || !username.trim() || !password}
          >
            {busy ? 'Signing in…' : 'Sign in →'}
          </Button>
        </div>
      </form>
    </AuthPage>
  )
}
