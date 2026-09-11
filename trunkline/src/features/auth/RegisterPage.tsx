import { useState, type SubmitEvent } from 'react'
import { Button } from '@/components/Button'
import { label, mono } from '@/components/text'
import { ApiError, issueField, issueText, refusalText } from '@/lib/api/auth'
import { useAuth } from '@/state/auth'
import { AuthPage, Field, FootLink, Refusal, RelayNotice } from './parts'

/**
 * The relay's password rule (octomate/types/auth.py: NewPassword), restated
 * for the checklist. Python's character classes are Unicode categories, and so
 * are these: a symbol is anything that is neither a letter, a digit, nor
 * whitespace. The relay's answer is still the one that counts.
 */
const RULES: { key: string; text: string; met: (password: string) => boolean }[] = [
  {
    key: 'length',
    text: '11 to 1024 characters',
    met: (p) => {
      const n = [...p].length
      return n >= 11 && n <= 1024
    },
  },
  { key: 'lower', text: 'a lowercase letter', met: (p) => /\p{Ll}/u.test(p) },
  { key: 'upper', text: 'an uppercase letter', met: (p) => /\p{Lu}/u.test(p) },
  { key: 'digit', text: 'a digit', met: (p) => /\p{Nd}/u.test(p) },
  {
    key: 'symbol',
    text: 'a symbol — punctuation counts, a space does not',
    met: (p) => /[^\p{L}\p{N}\s]/u.test(p),
  },
]

function Requirement({ met, children }: { met: boolean; children: string }) {
  return (
    <span style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
      <span style={{ ...mono(9, 700), color: met ? 'var(--color-sage)' : 'var(--fg-3)', width: 9 }}>
        {met ? '✓' : '○'}
      </span>
      <span style={{ ...mono(8.5), color: met ? 'var(--fg-2)' : 'var(--fg-3)' }}>{children}</span>
    </span>
  )
}

/** A field's own refusal by the relay, keyed the way its 422 names them. */
type FieldName = 'invitation' | 'username' | 'name' | 'password'
type FieldErrors = Partial<Record<FieldName, string>>

const isFieldName = (field: string): field is FieldName =>
  field === 'invitation' || field === 'username' || field === 'name' || field === 'password'

export function RegisterPage() {
  const carried = useAuth((s) => s.invitation)
  const relay = useAuth((s) => s.relay)
  const { register, goLogin } = useAuth((s) => s.actions)
  const [invitation, setInvitation] = useState(carried)
  const [username, setUsername] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<FieldErrors>({})
  const [error, setError] = useState<string | null>(null)

  const rulesMet = RULES.every((rule) => rule.met(password))
  const matches = confirm === password
  const usernameOk =
    username.length > 0 && username.length <= 100 && username === username.trim()
  const nameOk = name.length > 0 && name.length <= 100
  const ready = invitation.trim() !== '' && usernameOk && nameOk && rulesMet && matches

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !ready) return
    setBusy(true)
    setErrors({})
    setError(null)
    try {
      await register({ username, password, name, invitation: invitation.trim() })
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        // The only credential a registration carries is the invitation.
        setErrors({ invitation: 'This invitation is invalid, expired, or already used.' })
      } else if (caught instanceof ApiError && caught.status === 409) {
        setErrors({ username: refusalText(caught) ?? undefined })
      } else if (caught instanceof ApiError && Array.isArray(caught.detail)) {
        const byField: FieldErrors = {}
        const elsewhere: string[] = []
        for (const issue of caught.detail) {
          const field = issueField(issue)
          if (isFieldName(field)) {
            byField[field] = [byField[field], issueText(issue)].filter(Boolean).join(' ')
          } else {
            elsewhere.push(issueText(issue))
          }
        }
        setErrors(byField)
        if (elsewhere.length) setError(elsewhere.join(' '))
      } else {
        setError(refusalText(caught))
      }
      setBusy(false)
    }
  }

  return (
    <AuthPage
      title="Register"
      sub="one invitation · one account"
      width={440}
      foot={
        <>
          <span>Already registered?</span>
          <FootLink onClick={goLogin}>Sign in →</FootLink>
        </>
      }
    >
      <RelayNotice relay={relay} />
      <form onSubmit={submit} noValidate>
        <Field
          name="Invitation"
          hint={carried ? 'carried by your link · single use' : 'the code your operator issued · single use'}
          error={errors.invitation}
        >
          <input
            className="trk-input"
            name="invitation"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus={!carried}
            aria-invalid={errors.invitation ? 'true' : undefined}
            value={invitation}
            onChange={(e) => setInvitation(e.target.value)}
          />
        </Field>
        <Field
          name="Username"
          hint="1–100 characters · no surrounding spaces"
          error={errors.username}
        >
          <input
            className="trk-input"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus={!!carried}
            aria-invalid={errors.username ? 'true' : undefined}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field name="Display name" hint="how agents address you" error={errors.name}>
          <input
            className="trk-input"
            name="name"
            autoComplete="name"
            aria-invalid={errors.name ? 'true' : undefined}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field name="Password" error={errors.password}>
          <input
            className="trk-input"
            type="password"
            name="password"
            autoComplete="new-password"
            aria-invalid={errors.password ? 'true' : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <div
          style={{
            marginTop: 8,
            padding: '8px 10px',
            border: '1px solid var(--line-divider)',
            background: 'var(--trk-wash)',
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
          }}
        >
          <span style={{ ...label(7.5, '.16em'), color: 'var(--fg-3)', marginBottom: 2 }}>
            A password needs
          </span>
          {RULES.map((rule) => (
            <Requirement key={rule.key} met={rule.met(password)}>
              {rule.text}
            </Requirement>
          ))}
        </div>
        <Field
          name="Confirm password"
          error={confirm && !matches ? 'The two passwords differ.' : null}
        >
          <input
            className="trk-input"
            type="password"
            name="confirm"
            autoComplete="new-password"
            aria-invalid={confirm && !matches ? 'true' : undefined}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </Field>
        {error && <Refusal>{error}</Refusal>}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 20 }}>
          <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.5, flex: 1, minWidth: 0 }}>
            {ready
              ? 'signs you in as soon as the account exists'
              : 'every field above has to hold before this goes'}
          </span>
          <Button
            type="submit"
            variant="accent"
            disabled={busy || !ready}
            style={{ whiteSpace: 'nowrap', flexShrink: 0 }}
          >
            {busy ? 'Creating…' : 'Create account →'}
          </Button>
        </div>
      </form>
    </AuthPage>
  )
}
