/**
 * Bottom status bar — the brand cell, the channel in view as an underlined
 * tab, dotted signal chips on the left, tick-separated meta on the right. The
 * relay chip is derived live from /api/health; the account chip from the
 * session. Ported from the comp's "Bottom bar iterations · 09 Red dot".
 */
import { useState, type CSSProperties, type ReactNode } from 'react'
import { Icon } from '@/components/Icon'
import { mono } from '@/components/text'
import { useHealth } from '@/lib/api/hooks'
import type { StatusChip } from '@/lib/api/types'
import { refusalText } from '@/lib/api/auth'
import { useAuth } from '@/state/auth'
import { useConsole } from '@/state/console'

function Chip({
  tip,
  onClick,
  className = 'hov-wash',
  style,
  children,
}: {
  tip: string
  onClick?: () => void
  className?: string
  style?: CSSProperties
  children: ReactNode
}) {
  return (
    <span
      title={tip}
      onClick={onClick}
      className={className}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        height: 24,
        padding: '0 9px',
        whiteSpace: 'nowrap',
        flexShrink: 0,
        cursor: onClick ? 'pointer' : 'default',
        ...style,
      }}
    >
      {children}
    </span>
  )
}

function Dot({ tone }: { tone: NonNullable<StatusChip['dot']> }) {
  return <i style={{ width: 5, height: 5, borderRadius: 9999, background: `var(--color-${tone})` }} />
}

export function StatusBar() {
  const { toggleControl, setControlSection } = useConsole((s) => s.actions)
  const onAccount = useConsole((s) => s.mgmtSec === 'profile')
  const selChannel = useConsole((s) => s.selChannel)
  const user = useAuth((s) => s.user)
  const { signOut } = useAuth((s) => s.actions)
  const [menuOpen, setMenuOpen] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const health = useHealth().data

  const leave = async () => {
    setSigningOut(true)
    setSignOutError(null)
    try {
      await signOut()
    } catch (error) {
      setSignOutError(refusalText(error) ?? 'Sign-out failed. Check your connection and try again.')
      setSigningOut(false)
    }
  }

  const relay: StatusChip | null = health
    ? !health.reachable
      ? {
          t: 'relay offline · 127.0.0.1:8000',
          dot: 'red',
          tip: 'gateway unreachable — /api/health did not answer',
        }
      : health.ok
        ? { t: 'relay nominal', dot: 'teal', tip: 'gateway loop healthy' }
        : { t: 'relay degraded', dot: 'gold', tip: 'gateway reachable · health check failing' }
    : null
  // Gateway internals (hooks, tailers, MCP pools) await a status endpoint —
  // see README's gap list; until then the bar carries only what is real.
  const left = relay ? [relay] : []
  const utc = -new Date().getTimezoneOffset() / 60
  const meta: StatusChip[] = [{ t: `UTC${utc >= 0 ? '+' : ''}${utc}`, tip: 'local timezone' }]

  return (
    <div
      style={{
        height: 24,
        flexShrink: 0,
        background: 'var(--panel)',
        color: 'var(--on-panel)',
        boxSizing: 'border-box',
        display: 'flex',
        alignItems: 'stretch',
        ...mono(9, 500),
        letterSpacing: '.06em',
        overflow: 'hidden',
      }}
    >
      <span
        onClick={toggleControl}
        title="Toggle control rail"
        className="hov-bright"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
          padding: '0 10px',
          flexShrink: 0,
          cursor: 'pointer',
          background: 'var(--color-accent)',
          color: 'var(--trk-on-fill)',
          fontWeight: 700,
          letterSpacing: '.14em',
          textTransform: 'uppercase',
        }}
      >
        ⌗ Octomate
      </span>
      {selChannel && (
        <span
          title="Channel in view"
          className="trk-sbar-channel hov-wash"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            padding: '0 11px',
            flexShrink: 0,
            whiteSpace: 'nowrap',
            fontWeight: 700,
            boxShadow: 'inset 0 -2px 0 var(--color-accent)',
          }}
        >
          #{selChannel}
        </span>
      )}
      <span
        style={{
          flex: '1 1 auto',
          minWidth: 8,
          height: 24,
          display: 'flex',
          flexWrap: 'wrap',
          alignContent: 'flex-start',
          overflow: 'hidden',
        }}
      >
        {left.map((s) => (
          <Chip key={s.t} tip={s.tip}>
            {s.dot && <Dot tone={s.dot} />}
            {s.t}
          </Chip>
        ))}
      </span>
      <span
        className="trk-sbar-meta"
        style={{
          flex: '0 1 auto',
          height: 24,
          display: 'flex',
          flexWrap: 'wrap',
          alignContent: 'flex-start',
          justifyContent: 'flex-end',
          overflow: 'hidden',
        }}
      >
        {meta.map((s) => (
          <Chip
            key={s.t}
            tip={s.tip}
            className="hov-accent hov-op"
            style={{ gap: 10, padding: '0 0 0 10px', fontVariantNumeric: 'tabular-nums', opacity: 0.9 }}
          >
            {s.t}
            <i style={{ width: 1, height: 10, background: 'color-mix(in srgb, var(--on-panel) 30%, transparent)' }} />
          </Chip>
        ))}
        {/* The signed-in account, opening its menu: Profile, and sign out. The
            bar clips its overflow, so the menu is fixed above it rather than
            absolute within it; the click-away sits under it, as the
            composer's menus do. */}
        <span>
          {menuOpen && <span onClick={() => setMenuOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />}
          <Chip
            tip={`${user?.name ?? ''} · account`}
            onClick={() => setMenuOpen((open) => !open)}
            style={{ color: onAccount || menuOpen ? 'var(--color-accent)' : undefined }}
          >
            <Dot tone="teal" />@{user?.username}
            <span style={{ fontSize: 7, lineHeight: 1, marginTop: 1 }}>▾</span>
          </Chip>
          <div
            className="lt-menu"
            data-open={menuOpen ? '' : undefined}
            style={{
              position: 'fixed',
              right: 0,
              bottom: 24,
              minWidth: 160,
              zIndex: 80,
              background: 'var(--surface-raised)',
              color: 'var(--fg-1)',
              border: '1px solid var(--color-ink)',
              boxShadow: 'var(--shadow-card)',
              letterSpacing: '.06em',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '7px 12px',
                borderBottom: '1px solid var(--line-divider)',
                color: 'var(--fg-3)',
              }}
            >
              <Icon name="user" size={11} />
              {user?.name}
            </div>
            <div
              onClick={() => {
                setMenuOpen(false)
                setControlSection('profile')
              }}
              className="hov-wash"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '8px 12px',
                cursor: 'pointer',
                borderBottom: '1px solid var(--line-color)',
              }}
            >
              <Icon name="idCard" size={11} style={{ opacity: 0.7 }} />
              profile
            </div>
            <div
              title={signOutError ?? 'end this session on the relay'}
              onClick={signingOut ? undefined : () => void leave()}
              className="hov-wash"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '8px 12px',
                cursor: signingOut ? 'default' : 'pointer',
                color: signOutError ? 'var(--color-red)' : undefined,
              }}
            >
              <Icon name="logOut" size={11} style={{ opacity: 0.7 }} />
              {signingOut ? 'signing out…' : signOutError ? 'sign-out failed · retry' : 'sign out'}
            </div>
          </div>
        </span>
      </span>
    </div>
  )
}
