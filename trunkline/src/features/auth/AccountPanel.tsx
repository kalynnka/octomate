/** Account details, password dialog, and the dedicated API keys panel. */
import { useEffect, useRef, useState, type SubmitEvent } from 'react'
import { Button } from '@/components/Button'
import { Table, type TableColumn } from '@/components/Table'
import { chipLabel, fieldLabel, label, mono, serif } from '@/components/text'
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

function PasswordForm({ onCancel }: { onCancel: () => void }) {
  const { changePassword } = useAuth((s) => s.actions)
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const matches = confirm === password

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
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
    <form onSubmit={submit}>
      <Field name="Current password">
        <input
          className="trk-input"
          type="password"
          name="current_password"
          autoFocus
          required
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
          required
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
          required
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </Field>
      {error && <Refusal>{error}</Refusal>}
      <div className="trk-dialog-actions">
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="accent" disabled={busy || !current || !password || !matches}>
          {busy ? 'Changing…' : 'Change password'}
        </Button>
      </div>
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

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
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

function KeyState({ item }: { item: ApiApiKey }) {
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
    <div style={{ opacity: live ? 1 : 0.7 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', flexWrap: 'wrap', gap: 8 }}>
        <span style={{ ...label(9, '.1em'), color: state.color }}>
          {state.text}
        </span>
        {live && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void revoke()}
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
          </button>
        )}
      </div>
      {error && <Refusal>{error}</Refusal>}
    </div>
  )
}

const keyColumns: TableColumn<ApiApiKey>[] = [
  {
    key: 'name', label: 'Name', mono: true,
    render: (item) => <strong style={{ color: 'var(--fg-1)', overflowWrap: 'anywhere' }}>{item.name}</strong>,
  },
  { key: 'key_prefix', label: 'Key', mono: true, render: (item) => `${item.key_prefix}…` },
  {
    key: 'scopes', label: 'Scopes',
    render: (item) => (
      <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 3 }}>
        {item.scopes.map((scope) => (
          <span key={scope} style={{
            ...chipLabel, color: 'var(--color-accent)',
            border: '1px solid var(--color-accent)', padding: '1px 4px',
          }}>{scope}</span>
        ))}
      </span>
    ),
  },
  { key: 'created_at', label: 'Issued', mono: true, render: (item) => day(item.created_at) },
  { key: 'state', label: 'Status', align: 'right', render: (item) => <KeyState item={item} /> },
]

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const username = useAuth((s) => s.user?.username)
  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])

  return (
    <dialog ref={dialog} className="trk-dialog" aria-labelledby="password-title" aria-describedby="password-effect" onCancel={onClose}>
      <div className="trk-dialog-layout">
        <aside className="trk-dialog-panel">
          <span className="trk-dialog-index" aria-hidden="true">04</span>
          <span className="trk-dialog-eyebrow">Account security</span>
          <h2 id="password-title">Change password</h2>
          <dl className="trk-dialog-summary">
            <div><dt>Account</dt><dd>@{username}</dd></div>
            <div><dt>Access</dt><dd>Password</dd></div>
          </dl>
          <p id="password-effect" className="trk-dialog-caption">
            Changing your password signs out all browser sessions. Sign in again with your new password.
          </p>
        </aside>
        <div className="trk-dialog-main">
          <header className="trk-dialog-header">
            <span>Password details</span>
            <button type="button" className="trk-dialog-close hov-wash" aria-label="Close password dialog" onClick={onClose}>×</button>
          </header>
          <PasswordForm onCancel={onClose} />
        </div>
      </div>
    </dialog>
  )
}

export function AccountPanel() {
  const user = useAuth((s) => s.user)
  const [passwordOpen, setPasswordOpen] = useState(false)

  return (
    <div className="trk-control-scroll">
      <dl className="trk-profile-info">
        {[
          ['Name', user?.name],
          ['Username', user?.username],
          ['Nickname', user?.nickname],
          ['User ID', user?.id],
        ].map(([name, value]) => (
          <div key={name}>
            <dt style={{ ...label(10), color: 'var(--fg-3)' }}>{name}</dt>
            <dd style={{ ...mono(13), color: 'var(--fg-1)' }}>{value || '—'}</dd>
          </div>
        ))}
      </dl>
      <Button onClick={() => setPasswordOpen(true)} style={{ padding: '5px 10px', fontSize: 9 }}>
        Change password
      </Button>
      {passwordOpen && <PasswordDialog onClose={() => setPasswordOpen(false)} />}
    </div>
  )
}

export function ApiKeysPanel() {
  const { data: keys, error: loadError } = useApiKeys()
  const [issuing, setIssuing] = useState(false)
  const [issued, setIssued] = useState<ApiIssuedKey | null>(null)

  return (
    <>
      <div className="trk-control-scroll" style={{ flexShrink: 0, maxHeight: '60%' }}>
        <p style={{ margin: '0 0 8px', ...serif(12), lineHeight: 1.6, color: 'var(--fg-2)' }}>
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
          <button
            type="button"
            onClick={() => setIssuing(true)}
            className="trk-create-button hov-accent-border-wash"
          >
            + new key
          </button>
        )}
        {loadError && (
          <div style={{ padding: '0 16px' }}>
            <Refusal>{refusalText(loadError) ?? 'The key list could not be read.'}</Refusal>
          </div>
        )}
      </div>
      {keys && <Table columns={keyColumns} rows={keys} rowKey={(item) => item.id} dense empty="No keys issued yet." />}
    </>
  )
}
