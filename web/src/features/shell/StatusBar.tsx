/**
 * Bottom status bar — brand block, gateway/relay chips on the left, clock
 * chips on the right. The relay chip is derived live from /api/health; the
 * rest come from the status feed. Ported from the comp's "STATUS BAR" block.
 */
import { Icon } from '@/components/Icon'
import { mono } from '@/components/text'
import { useHealth, useStatusData } from '@/lib/api/hooks'
import type { StatusChip } from '@/lib/api/types'
import { useConsole } from '@/state/console'

export function StatusBar() {
  const { toggleControl } = useConsole((s) => s.actions)
  const status = useStatusData().data
  const health = useHealth().data

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
  const left = [...(relay ? [relay] : []), ...(status?.left.slice(1) ?? [])]
  const right = status?.right ?? []

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
          color: '#F6F1E5',
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
            <span
              key={s.t}
              title={s.tip}
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
              }}
            >
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
            </span>
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
          {right.map((s) => (
            <span
              key={s.t}
              title={s.tip}
              className="hov-wash"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                height: 25,
                padding: '0 9px',
                opacity: 0.92,
                whiteSpace: 'nowrap',
                flexShrink: 0,
              }}
            >
              {s.t}
            </span>
          ))}
        </span>
      </span>
    </div>
  )
}
