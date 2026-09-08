/**
 * The signed-out pages' shared frame and field voices: one dossier card on the
 * paper, mono field labels over sunken inputs, refusals in red prose, and the
 * relay's own state on a notice line.
 */
import type { ReactNode } from 'react'
import { Brackets } from '@/components/Brackets'
import { TriStripeInline } from '@/components/TriStripe'
import { display, ellipsis, fieldLabel, label, mono, serif, statusNote } from '@/components/text'
import type { RelayState } from '@/state/auth'

/** Full-viewport paper with one card on it — the console before anyone is in. */
export function AuthPage({
  title,
  sub,
  width = 400,
  children,
  foot,
}: {
  title: string
  sub: string
  width?: number
  children: ReactNode
  foot?: ReactNode
}) {
  return (
    <div
      className="paper-texture"
      style={{
        minHeight: 'calc(100vh / var(--trk-zoom, 1))',
        boxSizing: 'border-box',
        padding: 24,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--page-bg)',
        color: 'var(--fg-1)',
        fontFamily: 'var(--font-sans)',
      }}
    >
      <div
        className="lt-entry"
        style={{
          width: '100%',
          maxWidth: width,
          boxSizing: 'border-box',
          position: 'relative',
          background: 'var(--card-bg)',
          border: '1px solid var(--line-divider)',
          boxShadow: 'var(--shadow-card)',
          padding: '26px 30px 24px',
        }}
      >
        <Brackets />
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span
            style={{
              ...display(22),
              lineHeight: 1,
              letterSpacing: '-.01em',
              textTransform: 'uppercase',
              color: 'var(--fg-1)',
            }}
          >
            Octomate
          </span>
          <span style={{ ...label(7.5, '.18em'), color: 'var(--fg-3)', whiteSpace: 'nowrap' }}>
            Trunkline · console
          </span>
        </div>
        <TriStripeInline style={{ width: 64, margin: '12px 0 18px' }} />
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ ...display(15, 600), textTransform: 'uppercase', color: 'var(--fg-1)' }}>
            {title}
          </span>
          <span style={{ flex: 1 }} />
          <span style={{ ...statusNote, color: 'var(--fg-3)', textAlign: 'right' }}>{sub}</span>
        </div>
        {children}
        {foot && (
          <div
            style={{
              marginTop: 18,
              paddingTop: 12,
              borderTop: '1px solid var(--line-divider)',
              display: 'flex',
              alignItems: 'baseline',
              gap: 6,
              ...mono(9),
              color: 'var(--fg-3)',
            }}
          >
            {foot}
          </div>
        )}
      </div>
    </div>
  )
}

/** A field name over its input, with the relay's refusal of it underneath. */
export function Field({
  name,
  hint,
  error,
  children,
}: {
  name: string
  hint?: string
  error?: string | null
  children: ReactNode
}) {
  return (
    <label style={{ display: 'block', marginTop: 14 }}>
      <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 5 }}>
        <span style={{ ...fieldLabel, color: 'var(--fg-2)', whiteSpace: 'nowrap' }}>{name}</span>
        {hint && (
          <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>{hint}</span>
        )}
      </span>
      {children}
      {error && <Refusal>{error}</Refusal>}
    </label>
  )
}

/** Why the relay said no — a sentence, in prose, under the field it is about. */
export function Refusal({ children }: { children: ReactNode }) {
  return (
    <p
      className="lt-fade-in"
      style={{ margin: '6px 0 0', ...serif(12.5), lineHeight: 1.6, color: 'var(--color-red)' }}
    >
      {children}
    </p>
  )
}

/** A notice line in the feeler's voice: colored left edge, prose body. */
export function Notice({ tone, children }: { tone: 'gold' | 'red' | 'sage'; children: ReactNode }) {
  return (
    <div
      className="lt-fade-in"
      style={{
        marginTop: 14,
        borderLeft: `3px solid var(--color-${tone})`,
        background: 'var(--trk-wash)',
        padding: '7px 11px',
        ...serif(12.5),
        lineHeight: 1.55,
        color: 'var(--fg-1)',
      }}
    >
      {children}
    </div>
  )
}

/** The relay's state when it is the reason a form cannot go through. */
export function RelayNotice({ relay }: { relay: RelayState }) {
  if (relay === 'ok') return null
  return (
    <Notice tone="red">
      {relay === 'unconfigured'
        ? 'Local accounts are not configured on this relay — the operator has to add auth.yaml with its three salts (docs/users.md) and restart.'
        : 'The relay did not answer. Check that Octomate is running, then try again.'}
    </Notice>
  )
}

/** A link-styled action in the footer line. */
export function FootLink({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <span
      onClick={onClick}
      className="hov-accent"
      style={{
        color: 'var(--info-strong)',
        borderBottom: '1px solid var(--info)',
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}
