/**
 * The Account page's body: who is signed in, and the API keys the account
 * holds. A key is issued with a name, its scopes and an expiry, disclosed
 * exactly once, and revoked from its row; the list keeps revoked keys, as the
 * relay does, so a client that stopped authenticating can be traced to one.
 */
import { useState, type FormEvent } from 'react'
import { Button } from '@/components/Button'
import { chipLabel, ellipsis, fieldLabel, label, mono, sectionLabel, serif } from '@/components/text'
import {
  createApiKey,
  revokeApiKey,
  type ApiApiKey,
  type ApiIssuedKey,
  type ApiKeyScope,
} from '@/lib/api/auth'
import { useApiKeys } from '@/lib/api/hooks'
import { queryClient } from '@/lib/queryClient'
import { refusalText } from '@/lib/api/auth'
import { useAuth } from '@/state/auth'
import { Field, Refusal } from './parts'

const SCOPES: { id: ApiKeyScope; note: string }[] = [
  { id: 'hooks', note: 'native session hooks · transcript streams' },
  { id: 'mcp', note: 'installed MCP clients' },
]

const EXPIRIES: { label: string; days: number | null }[] = [
  { label: 'never', days: null },
  { label: '24 hours', days: 1 },
  { label: '7 days', days: 7 },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
  { label: '1 year', days: 365 },
]

const day = (iso: string) =>
  new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })

const refreshKeys = () => queryClient.invalidateQueries({ queryKey: ['api-keys'] })

function Section({ first, children }: { first?: boolean; children: string }) {
  return (
    <div
      style={{
        borderTop: first ? undefined : '1px solid var(--line-color)',
        margin: '6px 16px 0',
        padding: '8px 0 4px',
        ...sectionLabel,
        color: 'var(--fg-3)',
      }}
    >
      {children}
    </div>
  )
}

function PasswordForm() {
  const { changePassword } = useAuth((s) => s.actions)
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const matches = confirm === password

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !current || !password || !matches) return
    setBusy(true)
    setError(null)
    try {
      await changePassword({ current_password: current, password })
    } catch (caught) {
      setError(refusalText(caught) ?? 'The relay did not answer. Try again.')
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} style={{ margin: '0 16px 12px', maxWidth: 400 }}>
      <Field name="Current password">
        <input
          className="trk-input"
          type="password"
          name="current_password"
          autoComplete="current-password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
      </Field>
      <Field name="New password">
        <input
          className="trk-input"
          type="password"
          name="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <p style={{ ...serif(12), lineHeight: 1.6, color: 'var(--fg-2)' }}>
        Use 11–1024 characters with a lowercase letter, an uppercase letter, a digit,
        and a symbol. Changing your password signs out all browser sessions.
      </p>
      <Field name="Confirm new password" error={confirm && !matches ? 'The two passwords differ.' : null}>
        <input
          className="trk-input"
          type="password"
          name="confirm"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </Field>
      {error && <Refusal>{error}</Refusal>}
      <Button
        type="submit"
        variant="accent"
        disabled={busy || !current || !password || !matches}
        style={{ marginTop: 14 }}
      >
        {busy ? 'Changing…' : 'Change password'}
      </Button>
    </form>
  )
}

function NewKeyForm({
  onIssued,
  onCancel,
}: {
  onIssued: (issued: ApiIssuedKey) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<ApiKeyScope[]>(['hooks', 'mcp'])
  const [expiry, setExpiry] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ready = name.trim() !== '' && scopes.length > 0

  const toggle = (scope: ApiKeyScope) =>
    setScopes((held) =>
      held.includes(scope) ? held.filter((each) => each !== scope) : [...held, scope],
    )

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !ready) return
    setBusy(true)
    setError(null)
    const days = EXPIRIES[expiry].days
    try {
      const issued = await createApiKey({
        name: name.trim(),
        scopes: SCOPES.map((s) => s.id).filter((s) => scopes.includes(s)),
        ...(days === null
          ? {}
          : { expires_at: new Date(Date.now() + days * 86_400_000).toISOString() }),
      })
      void refreshKeys()
      onIssued(issued)
    } catch (caught) {
      setError(refusalText(caught) ?? 'The relay did not answer.')
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      className="lt-fade-in"
      style={{
        margin: '4px 16px 8px',
        padding: '10px 12px 12px',
        border: '1px solid var(--line-divider)',
        background: 'var(--card-bg)',
      }}
    >
      <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 14, alignItems: 'end' }}>
        <label style={{ display: 'block' }}>
          <span style={{ display: 'block', ...fieldLabel, color: 'var(--fg-2)', marginBottom: 5 }}>
            Name
          </span>
          <input
            className="trk-input"
            autoFocus
            placeholder="which machine or client holds it"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label style={{ display: 'block' }}>
          <span style={{ display: 'block', ...fieldLabel, color: 'var(--fg-2)', marginBottom: 5 }}>
            Expires
          </span>
          <select
            className="trk-input"
            style={{ width: 'auto', paddingRight: 24 }}
            value={expiry}
            onChange={(e) => setExpiry(Number(e.target.value))}
          >
            {EXPIRIES.map((option, index) => (
              <option key={option.label} value={index}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div style={{ ...fieldLabel, color: 'var(--fg-2)', margin: '12px 0 6px' }}>Scopes</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {SCOPES.map((scope) => {
          const on = scopes.includes(scope.id)
          return (
            <span
              key={scope.id}
              onClick={() => toggle(scope.id)}
              className="hov-wash"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 9,
                padding: '3px 4px',
                cursor: 'pointer',
              }}
            >
              <span
                style={{
                  width: 11,
                  height: 11,
                  boxSizing: 'border-box',
                  border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                  background: on ? 'var(--color-accent)' : 'transparent',
                  flexShrink: 0,
                }}
              />
              <span style={{ ...mono(10, 700), color: on ? 'var(--fg-1)' : 'var(--fg-2)', width: 44 }}>
                {scope.id}
              </span>
              <span style={{ ...mono(8), color: 'var(--fg-3)' }}>{scope.note}</span>
            </span>
          )
        })}
      </div>
      {error && <Refusal>{error}</Refusal>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
        <span style={{ ...mono(8), color: 'var(--fg-3)' }}>the token is shown once, on issue</span>
        <span style={{ flex: 1 }} />
        <Button variant="ghost" onClick={onCancel} style={{ padding: '5px 10px', fontSize: 8.5 }}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="accent"
          disabled={busy || !ready}
          style={{ padding: '5px 12px', fontSize: 8.5 }}
        >
          {busy ? 'Issuing…' : 'Issue key'}
        </Button>
      </div>
    </form>
  )
}

/** The one disclosure of a token, with the command that stores it on a client. */
function IssuedKey({ issued, onDone }: { issued: ApiIssuedKey; onDone: () => void }) {
  const [copied, setCopied] = useState<'copied' | 'failed' | null>(null)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(issued.token)
      setCopied('copied')
    } catch {
      // No clipboard on an insecure origin; the token stays selectable below.
      setCopied('failed')
    }
    setTimeout(() => setCopied(null), 2200)
  }
  const apiUrl = import.meta.env.DEV ? import.meta.env.VITE_API_URL : window.location.origin
  const configure = `octomate configure --url ${apiUrl} --token '${issued.token}'`
  return (
    <div
      className="lt-fade-in"
      style={{
        margin: '4px 16px 8px',
        padding: '10px 12px 12px',
        border: '1px solid var(--line-divider)',
        borderLeft: '3px solid var(--color-gold)',
        background: 'var(--card-bg)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{ ...label(8.5, '.12em'), color: 'var(--fg-1)' }}>new key · {issued.key.name}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...label(7.5, '.14em'), color: 'var(--color-gold)' }}>
          shown once — store it now
        </span>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <input
          className="trk-input"
          readOnly
          value={issued.token}
          onFocus={(e) => e.currentTarget.select()}
        />
        <Button
          variant={copied === 'copied' ? 'solid' : 'outline'}
          onClick={() => void copy()}
          style={{ padding: '5px 12px', fontSize: 8.5, whiteSpace: 'nowrap' }}
        >
          {copied === 'copied' ? 'Copied ✓' : copied === 'failed' ? 'Select it' : 'Copy'}
        </Button>
      </div>
      <p style={{ margin: '10px 0 0', ...serif(12), lineHeight: 1.6, color: 'var(--fg-2)' }}>
        The relay keeps only a hash. To use it from a machine, save it there with:
      </p>
      <pre
        style={{
          margin: '6px 0 0',
          padding: '8px 10px',
          border: '1px solid var(--line-divider)',
          background: 'var(--trk-wash)',
          ...mono(9.5),
          lineHeight: 1.6,
          color: 'var(--fg-1)',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
        }}
      >
        {configure}
      </pre>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
        <Button variant="ghost" onClick={onDone} style={{ padding: '5px 10px', fontSize: 8.5 }}>
          Done
        </Button>
      </div>
    </div>
  )
}

function KeyRow({ item }: { item: ApiApiKey }) {
  const [arming, setArming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const expired = item.expires_at !== null && Date.parse(item.expires_at) <= Date.now()
  const state = item.revoked_at
    ? { text: `○ revoked · ${day(item.revoked_at)}`, color: 'var(--fg-3)' }
    : item.expires_at === null
      ? { text: '● no expiry', color: 'var(--color-sage)' }
      : expired
        ? { text: `○ expired · ${day(item.expires_at)}`, color: 'var(--fg-3)' }
        : { text: `● until ${day(item.expires_at)}`, color: 'var(--color-sage)' }
  const live = !item.revoked_at && !expired

  const revoke = async () => {
    if (!arming) {
      setArming(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await revokeApiKey(item.id)
      await refreshKeys()
    } catch (caught) {
      setError(refusalText(caught) ?? 'The relay did not answer.')
    } finally {
      setBusy(false)
      setArming(false)
    }
  }

  return (
    <div style={{ padding: '5px 16px', opacity: live ? 1 : 0.7 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', width: 120, flexShrink: 0, ...ellipsis }}>
          {item.name}
        </span>
        <span style={{ ...mono(8.5), color: 'var(--fg-2)', flexShrink: 0 }}>{item.key_prefix}…</span>
        <span style={{ display: 'inline-flex', gap: 3, flexShrink: 0 }}>
          {item.scopes.map((scope) => (
            <span
              key={scope}
              style={{
                ...chipLabel,
                color: 'var(--color-accent)',
                border: '1px solid var(--color-accent)',
                padding: '1px 4px',
              }}
            >
              {scope}
            </span>
          ))}
        </span>
        <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, ...ellipsis }}>
          issued {day(item.created_at)}
        </span>
        <span style={{ ...label(7.5, '.1em'), color: state.color, whiteSpace: 'nowrap', flexShrink: 0 }}>
          {state.text}
        </span>
        {live && (
          <span
            onClick={busy ? undefined : () => void revoke()}
            onMouseLeave={() => {
              if (!busy) setArming(false)
            }}
            title={arming ? 'click again to revoke' : 'revoke this key'}
            className={arming ? 'hov-accent-fill' : 'hov-red'}
            style={{
              ...label(7.5, '.12em'),
              color: arming ? 'var(--trk-on-fill)' : 'var(--fg-3)',
              background: arming ? 'var(--color-red)' : 'transparent',
              border: `1px solid ${arming ? 'var(--color-red)' : 'var(--line-divider)'}`,
              padding: '2px 7px',
              cursor: busy ? 'default' : 'pointer',
              flexShrink: 0,
              transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear',
            }}
          >
            {busy ? 'revoking…' : arming ? 'revoke — sure?' : 'revoke'}
          </span>
        )}
      </div>
      {error && <Refusal>{error}</Refusal>}
    </div>
  )
}

export function AccountPanel() {
  const user = useAuth((s) => s.user)
  const { data: keys, error: loadError } = useApiKeys()
  const [issuing, setIssuing] = useState(false)
  const [issued, setIssued] = useState<ApiIssuedKey | null>(null)

  return (
    <div
      className="lt-entry"
      style={{
        borderTop: '1px solid var(--line-color)',
        borderBottom: '1px solid var(--line-divider)',
        background: 'var(--surface-sunken)',
        padding: '2px 0 8px',
      }}
    >
      <Section first>Signed in as</Section>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '2px 16px 6px' }}>
        <span style={{ ...mono(12, 700), color: 'var(--fg-1)' }}>{user?.name}</span>
        <span style={{ ...mono(9.5, 700), color: 'var(--color-accent)' }}>@{user?.username}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(8), color: 'var(--fg-3)' }} title="account id">
          {user?.id}
        </span>
      </div>

      <Section>Password</Section>
      <PasswordForm />

      <Section>API keys</Section>
      <p style={{ margin: '0 16px 8px', ...serif(12), lineHeight: 1.6, color: 'var(--fg-2)' }}>
        A key lets a client machine speak for this account: <b>hooks</b> for native session
        hooks and transcript streams, <b>mcp</b> for installed MCP clients. The relay keeps a
        hash and shows the token once, when it is issued.
      </p>
      {issued && (
        <IssuedKey
          issued={issued}
          onDone={() => setIssued(null)}
        />
      )}
      {issuing ? (
        <NewKeyForm
          onIssued={(key) => {
            setIssued(key)
            setIssuing(false)
          }}
          onCancel={() => setIssuing(false)}
        />
      ) : (
        <span
          onClick={() => setIssuing(true)}
          className="hov-accent-border-wash"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            margin: '2px 16px 8px',
            height: 22,
            boxSizing: 'border-box',
            border: '1px dashed color-mix(in srgb, var(--color-accent) 55%, transparent)',
            color: 'var(--color-accent)',
            ...label(8, '.14em'),
            cursor: 'pointer',
          }}
        >
          + new key
        </span>
      )}
      {loadError && (
        <div style={{ padding: '0 16px' }}>
          <Refusal>{refusalText(loadError) ?? 'The key list could not be read.'}</Refusal>
        </div>
      )}
      {keys && keys.length === 0 && (
        <div style={{ padding: '8px 16px 4px', ...mono(8.5), color: 'var(--fg-3)' }}>
          no keys issued yet
        </div>
      )}
      {(keys ?? []).map((item) => (
        <KeyRow key={item.id} item={item} />
      ))}
    </div>
  )
}
