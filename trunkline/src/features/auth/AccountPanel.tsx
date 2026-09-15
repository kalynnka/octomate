/** Account details, password dialog, and the dedicated API keys panel. */
import { useEffect, useRef, useState, type SubmitEvent } from 'react'
import { Button } from '@/components/Button'
import { Brackets } from '@/components/Brackets'
import { Table, type TableColumn } from '@/components/Table'
import { chipLabel, display, fieldLabel, label, mono, serif } from '@/components/text'
import {
  createApiKey,
  revokeApiKey,
  unlinkProfile,
  type ApiApiKey,
  type ApiIssuedKey,
  type ApiKeyScope,
} from '@/lib/api/auth'
import { useApiKeys } from '@/lib/api/hooks'
import type { ApiProfileInfo, ApiUserProfile } from '@/lib/api/events'
import { channelMeta } from '@/lib/api/live'
import { closeDialog } from '@/lib/dialog'
import { queryClient } from '@/lib/queryClient'
import { useDialogDrag } from '@/lib/useDialogDrag'
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

function KeyRevoke({ item }: { item: ApiApiKey }) {
  const [arming, setArming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const expired = item.expires_at !== null && Date.parse(item.expires_at) <= Date.now()
  if (item.revoked_at || expired) return null

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
    <div>
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
          color: arming ? 'var(--trk-on-fill)' : 'color-mix(in srgb, var(--color-red) 60%, var(--fg-1))',
          background: arming ? 'var(--color-red)' : 'color-mix(in srgb, var(--color-red) 10%, transparent)',
          border: '1px solid var(--color-red)',
          cursor: busy ? 'default' : 'pointer',
          flexShrink: 0,
          transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear',
        }}
      >
        {busy ? 'revoking…' : arming ? 'revoke — sure?' : 'revoke'}
      </button>
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
  {
    key: 'state', label: 'Status',
    render: (item) => {
      const expired = item.expires_at !== null && Date.parse(item.expires_at) <= Date.now()
      const state = item.revoked_at
        ? { text: `○ revoked · ${day(item.revoked_at)}`, color: 'var(--fg-3)' }
        : item.expires_at === null
          ? { text: '● no expiry', color: 'var(--color-sage)' }
          : expired
            ? { text: `○ expired · ${day(item.expires_at)}`, color: 'var(--fg-3)' }
            : { text: `● until ${day(item.expires_at)}`, color: 'var(--color-sage)' }
      return <span style={{ ...label(9, '.1em'), color: state.color, opacity: item.revoked_at || expired ? 0.7 : 1 }}>{state.text}</span>
    },
  },
  { key: 'actions', label: '', ariaLabel: 'Actions', width: '1%', render: (item) => <KeyRevoke item={item} /> },
]

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const drag = useDialogDrag(dialog)
  const username = useAuth((s) => s.user?.username)
  const close = () => void closeDialog(dialog.current, onClose)
  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])

  return (
    <dialog ref={dialog} className="trk-dialog" aria-labelledby="password-title" aria-describedby="password-effect" onCancel={(event) => {
      event.preventDefault()
      close()
    }}>
      <div className="trk-dialog-layout" {...drag}>
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
            <button type="button" className="trk-dialog-close hov-wash" aria-label="Close password dialog" onClick={close}>×</button>
          </header>
          <PasswordForm onCancel={close} />
        </div>
      </div>
    </dialog>
  )
}

export function AccountPanel({ profiles, profilesError }: { profiles: ApiUserProfile[] | undefined; profilesError: boolean }) {
  const user = useAuth((s) => s.user)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState(false)
  const [disconnectError, setDisconnectError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const confirmationRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (confirmingId) confirmationRef.current?.querySelector('button')?.focus()
    else if (notice) pickerRef.current?.querySelector<HTMLButtonElement>('button[data-channel="false"]')?.focus()
  }, [confirmingId, notice])
  if (!user) return null
  const cards = [user, ...(profiles ?? [])]
  const selected = profiles?.find((profile) => profile.id === selectedId) ?? user

  const disconnect = async (profile: ApiUserProfile) => {
    if (disconnecting) return
    setDisconnecting(true)
    setDisconnectError(null)
    try {
      await queryClient.cancelQueries({ queryKey: ['profile'] })
      await unlinkProfile(profile.id)
      queryClient.setQueryData<ApiProfileInfo>(['profile'], (current) => current?.user.id === user.id ? {
        ...current, profiles: current.profiles.filter((linked) => linked.id !== profile.id),
      } : current)
      setSelectedId(null)
      setConfirmingId(null)
      setNotice(`${channelMeta(profile.channel_tentacle_id).label} profile disconnected.`)
      void queryClient.invalidateQueries({ queryKey: ['profile'] })
      void queryClient.invalidateQueries({ queryKey: ['threads'] })
    } catch (error) {
      setDisconnectError(refusalText(error) ?? 'Could not disconnect the profile. Try again.')
    } finally {
      setDisconnecting(false)
    }
  }

  return (
    <div className="trk-control-scroll">
      {notice && <p className="trk-control-note" role="status">{notice}</p>}
      <div className="trk-profile-workspace">
        <div ref={pickerRef} className="trk-profile-picker" role="group" aria-label="Choose a profile">
          <div className="trk-profile-picker-heading">
            <span style={label(9)}>Channels</span>
            <span style={mono(9)}>{profiles === undefined ? '—' : String(profiles.length).padStart(2, '0')}</span>
          </div>
          {cards.map((card, index) => {
            const channel = 'channel_tentacle_id' in card ? channelMeta(card.channel_tentacle_id) : null
            const id = 'username' in card ? 'account' : card.id
            const name = card.name || ('username' in card ? card.username : 'Unnamed profile')
            return (
              <button
                key={id}
                type="button"
                aria-label={`Show ${channel?.label ?? 'Octomate'} profile for ${name}`}
                aria-pressed={card === selected}
                aria-controls={`trk-profile-card-${id}`}
                data-channel={channel !== null}
                style={{ color: channel?.brand ?? 'var(--color-accent)' }}
                disabled={disconnecting}
                onClick={() => {
                  setSelectedId('username' in card ? null : card.id)
                  setConfirmingId(null)
                  setDisconnectError(null)
                  setNotice(null)
                }}
              >
                <span className="trk-profile-picker-index" aria-hidden="true" style={mono(9, 700)}>
                  {`${channel ? 'C' : 'U'}${String(index + 1).padStart(2, '0')}`}
                </span>
                <span className="trk-profile-picker-copy">
                  <span style={{ ...label(9, '.14em'), color: channel?.brand ?? 'var(--fg-1)' }}>{channel?.label ?? 'Octomate account'}</span>
                  <span title={name} style={{ ...mono(10), color: 'var(--fg-2)' }}>{name}</span>
                </span>
                <span className="trk-profile-picker-status" aria-hidden="true" />
              </button>
            )
          })}
          {profiles === undefined ? (
            <p className="trk-control-note" role="status">{profilesError ? 'Could not load channels.' : 'Loading channels…'}</p>
          ) : profiles.length === 0 && (
            <p className="trk-control-note">No channel identities are linked to your account.</p>
          )}
        </div>
        <div className="trk-profile-stack" style={{ marginBottom: Math.min(cards.length - 1, 3) * 12, marginRight: Math.min(cards.length - 1, 3) * 12 }}>
          {cards.slice(1, 4).map((card, index) => (
            <div
              key={`back-${card.id}`}
              className="trk-profile-back"
              aria-hidden="true"
              style={{ zIndex: -index - 1, transform: `translate(${(index + 1) * 12}px, ${(index + 1) * 12}px)` }}
            >
              <Brackets />
            </div>
          ))}
          {cards.map((card) => {
            const channel = 'channel_tentacle_id' in card ? channelMeta(card.channel_tentacle_id) : null
            const id = `trk-profile-card-${'username' in card ? 'account' : card.id}`
            const name = card.name || ('username' in card ? card.username : 'Unnamed profile')
            const details: [string, string | number | null][] = 'username' in card ? [['User ID', card.id]] : [
              ['Title', card.title], ['Gender', card.gender], ['Age', card.age],
              ['Account ID', card.channel_user_id], ['Profile ID', card.id],
            ]
            return (
              <article
                key={id}
                id={id}
                className="trk-account-card"
                aria-labelledby={`${id}-name`}
                aria-hidden={card !== selected}
                inert={card !== selected}
                data-front={card === selected}
              >
                <Brackets />
                <div className="trk-account-masthead">
                  <span style={{ ...display(15), letterSpacing: '-.02em' }}>Octomate<span style={{ color: 'var(--color-accent)' }}>.</span></span>
                  <span style={{ ...label(8), color: 'var(--fg-3)' }}>{channel ? 'Channel identity' : 'Personal account'}</span>
                </div>
                <header className="trk-account-identity">
                  <div className="trk-account-avatar" aria-hidden="true" style={{ color: channel?.brand }}>
                    {name.trim().split(/\s+/).slice(0, 2).map(([initial]) => initial).join('').toUpperCase()}
                  </div>
                  <div className="trk-account-name">
                    <span style={{ ...label(9), color: channel?.brand ?? 'var(--fg-3)' }}>{channel?.label ?? 'Octomate account'}</span>
                    <h2 id={`${id}-name`}>{name}<span aria-hidden="true" style={{ color: channel?.brand ?? 'var(--color-accent)' }}>.</span></h2>
                    {'username' in card && <p style={mono(12)}>@{card.username}</p>}
                    {card.nickname && card.nickname !== name && <p style={serif(13)}>Also known as {card.nickname}</p>}
                  </div>
                </header>
                <dl className="trk-account-facts">
                  {details.map(([labelText, value]) => (
                    <div key={labelText} data-technical={labelText.endsWith('ID')}>
                      <dt style={{ ...label(9), color: 'var(--fg-3)' }}>{labelText}</dt>
                      <dd style={{ ...mono(12), color: 'var(--fg-1)' }}>{value === null || value === '' ? '—' : value}</dd>
                    </div>
                  ))}
                </dl>
                {'username' in card && (
                  <footer className="trk-account-actions">
                    <Button onClick={() => setPasswordOpen(true)} style={{ padding: '7px 12px', fontSize: 9 }}>
                      Change password
                    </Button>
                  </footer>
                )}
                {'channel_tentacle_id' in card && (
                  <footer className="trk-account-actions">
                    {card.channel_tentacle_id === 'trunkline' ? (
                      <p className="trk-control-note">Uses your signed-in Octomate account directly.</p>
                    ) : confirmingId === card.id ? (
                      <div ref={confirmationRef} className="trk-profile-disconnect" role="group" aria-label="Confirm profile disconnect" aria-busy={disconnecting}>
                        <p>Disconnect {name} on {channel?.label} from your Octomate account? This profile will no longer identify you on that channel. Its history will not be deleted.</p>
                        {disconnectError && <div role="alert"><Refusal>{disconnectError}</Refusal></div>}
                        <div>
                          <Button disabled={disconnecting} onClick={() => {
                            setConfirmingId(null)
                            setDisconnectError(null)
                            pickerRef.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus()
                          }}>Cancel</Button>
                          <Button disabled={disconnecting} onClick={() => { void disconnect(card) }} style={{ color: 'var(--color-red)', borderColor: 'var(--color-red)' }}>
                            {disconnecting ? 'Disconnecting…' : 'Confirm disconnect'}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <Button onClick={() => setConfirmingId(card.id)} style={{ padding: '7px 12px', fontSize: 9 }}>Disconnect profile</Button>
                    )}
                  </footer>
                )}
              </article>
            )
          })}
        </div>
      </div>
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
