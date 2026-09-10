/**
 * Bottom status bar — brand block, gateway/relay chips on the left, the
 * account and clock chips on the right. The relay chip is derived live from
 * /api/health; the account chips from the session. Ported from the comp's
 * "STATUS BAR" block.
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
  style,
  children,
}: {
  tip: string
  onClick?: () => void
  style?: CSSProperties
  children: ReactNode
}) {
  return (
    <span
      title={tip}
      onClick={onClick}
      className="hov-wash"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        height: 25,
        padding: '0 9px',
        opacity: 0.92,
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

export function StatusBar() {
  const { toggleControl, setControlSection } = useConsole((s) => s.actions)
  const onAccount = useConsole((s) => s.mgmtSec === 'account')
  const user = useAuth((s) => s.user)
  const { signOut } = useAuth((s) => s.actions)
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
  const right: StatusChip[] = [{ t: `UTC${utc >= 0 ? '+' : ''}${utc}`, tip: 'local timezone' }]

  return (
    <div
      style={{
        height: 26,
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
        title="Control"
        className="hov-dim"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
          padding: '0 10px',
          cursor: 'pointer',
          background: 'var(--color-accent)',
          color: 'var(--trk-on-fill)',
        }}
      >
        <Icon name="spokes" size={11} strokeWidth={2.4} />
        <b style={{ fontWeight: 700, letterSpacing: '.12em' }}>Octomate</b>
      </span>
      <span
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          overflow: 'hidden',
          borderTop: '1px solid var(--line-divider)',
          boxSizing: 'border-box',
        }}
      >
        <span
          style={{
            flex: '1 1 auto',
            minWidth: 0,
            height: 25,
            display: 'flex',
            flexWrap: 'wrap',
            alignContent: 'flex-start',
            overflow: 'hidden',
          }}
        >
          {left.map((s) => (
            <Chip key={s.t} tip={s.tip}>
              {s.dot && (
                <i
                  style={{
                    width: 5,
                    height: 5,
                    borderRadius: 9999,
                    background: `var(--color-${s.dot})`,
                  }}
                />
              )}
              {s.t}
            </Chip>
          ))}
        </span>
        <span
          style={{
            flex: '0 1 auto',
            height: 25,
            display: 'flex',
            flexWrap: 'wrap',
            alignContent: 'flex-start',
            justifyContent: 'flex-end',
            overflow: 'hidden',
          }}
        >
          {/* The signed-in account, and the way out. The name opens the Account
              page — the keys this account issued, and who it is on the relay. */}
          <Chip
            tip={`${user?.name ?? ''} · account, api keys`}
            onClick={() => setControlSection('account')}
            style={{
              fontWeight: 700,
              color: onAccount ? 'var(--color-accent)' : undefined,
              boxShadow: `inset 0 -2px 0 ${onAccount ? 'var(--color-accent)' : 'transparent'}`,
            }}
          >
            @{user?.username}
          </Chip>
          <Chip
            tip={signOutError ?? 'end this session on the relay'}
            onClick={signingOut ? undefined : () => void leave()}
            style={{ color: signOutError ? 'var(--color-red)' : undefined }}
          >
            <span style={{ opacity: 0.7 }}>⏻</span>
            {signingOut ? 'signing out…' : signOutError ? 'sign-out failed · retry' : 'sign out'}
          </Chip>
          {right.map((s) => (
            <Chip key={s.t} tip={s.tip}>
              {s.t}
            </Chip>
          ))}
        </span>
      </span>
    </div>
  )
}
