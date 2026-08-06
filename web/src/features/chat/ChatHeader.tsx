import { useConsole } from '@/state/console'
import { useSurfaces, useThreads } from '@/lib/api/hooks'
import { Icon } from '@/components/Icon'
import { ellipsis, label, mono } from '@/components/text'

export function ChatHeader() {
  const selThreadId = useConsole((s) => s.selThreadId)
  const ntOn = useConsole((s) => s.ntOn)
  const ntTitle = useConsole((s) => s.ntTitle)
  const surface = useConsole((s) => s.surface)
  const teleOpen = useConsole((s) => s.teleOpen)
  const teleporting = useConsole((s) => s.teleporting)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const theme = useConsole((s) => s.theme)
  const sysDark = useConsole((s) => s.sysDark)
  const { toggleTeleMenu, teleport, toggleTheme, toggleTrace } = useConsole((s) => s.actions)
  const { data: surfaces } = useSurfaces()
  const { data: threads } = useThreads()

  const isDark = theme === 'dark' || (theme === 'auto' && sysDark)
  const cur = surfaces?.find((s) => s.id === surface) ?? surfaces?.[0]
  const title = ntOn
    ? ntTitle || 'untitled — new thread'
    : (Object.values(threads ?? {})
        .flat()
        .find((t) => t.id === selThreadId)?.title ?? '')

  const iconBtn = (onClick: () => void, title2: string, active: boolean, children: React.ReactNode) => (
    <span
      onClick={onClick}
      title={title2}
      className="hov-ink-wash"
      style={{
        width: 22,
        height: 22,
        boxSizing: 'border-box',
        flexShrink: 0,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'pointer',
        color: active ? 'var(--color-accent)' : 'var(--fg-2)',
        border: '1px solid var(--line-divider)',
      }}
    >
      {children}
    </span>
  )

  return (
    <div
      id="trk-chathead"
      className="lt-fade-in"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '0 16px',
        height: 44,
        boxSizing: 'border-box',
        borderBottom: '1px solid var(--line-divider)',
        flexShrink: 0,
        position: 'relative',
        zIndex: 80,
      }}
    >
      <span style={{ ...label(10, '.14em'), color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{title}</span>
      <span style={{ ...mono(8.5, 700), color: 'var(--color-accent)', flexShrink: 0 }}>{selThreadId}</span>
      <span style={{ flex: 1, minWidth: 8 }} />
      {cur && (
        <span
          title="Current surface"
          style={{
            ...label(8.5, '.1em'),
            color: cur.brand,
            border: `1px solid ${cur.brand}`,
            padding: '0 7px',
            height: 22,
            boxSizing: 'border-box',
            display: 'inline-flex',
            alignItems: 'center',
            whiteSpace: 'nowrap',
            flexShrink: 0,
          }}
        >
          {cur.label}
        </span>
      )}
      <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}>
        <span
          onClick={toggleTeleMenu}
          className="hov-teal-ghost"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            ...label(8.5, '.1em'),
            color: '#F6F1E5',
            background: 'var(--color-teal)',
            border: '1px solid var(--color-teal)',
            padding: '0 8px',
            height: 22,
            boxSizing: 'border-box',
            cursor: 'pointer',
          }}
        >
          ⇄ Teleport
        </span>
        {teleporting && (
          <span
            style={{
              position: 'absolute',
              right: 0,
              top: 'calc(100% + 6px)',
              whiteSpace: 'nowrap',
              ...label(9, '.14em'),
              color: 'var(--color-teal)',
              animation: 'trkPulse 1s infinite',
              zIndex: 50,
            }}
          >
            ⇄ teleporting — packing pointer card…
          </span>
        )}
        <div
          className="lt-menu"
          data-open={teleOpen ? '' : undefined}
          style={{
            position: 'absolute',
            right: 0,
            top: 'calc(100% + 6px)',
            width: 264,
            background: 'var(--surface-raised)',
            border: '1px solid var(--color-ink)',
            boxShadow: 'var(--shadow-card)',
            zIndex: 60,
          }}
        >
          <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--line-divider)', ...label(8, '.18em'), color: 'var(--fg-3)' }}>
            Continue this thread in…
          </div>
          {(surfaces ?? []).map((sf) => {
            const here = sf.id === surface
            return (
              <div
                key={sf.id}
                onClick={() => cur && teleport(sf.id, sf.label, cur.label)}
                className="hov-wash"
                style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '9px 12px', cursor: 'pointer', borderBottom: '1px solid var(--line-color)' }}
              >
                <i style={{ width: 6, height: 6, borderRadius: 9999, background: sf.brand }} />
                <span style={{ ...mono(10.5, 700), color: 'var(--fg-1)' }}>{sf.label}</span>
                <span style={{ ...mono(8.5), color: 'var(--fg-3)' }}>{sf.sub}</span>
                <span style={{ flex: 1 }} />
                {here ? (
                  <span style={{ ...label(8, '.14em'), color: 'var(--color-accent)' }}>● here</span>
                ) : (
                  <span style={{ ...mono(10), color: 'var(--color-teal)' }}>→</span>
                )}
              </div>
            )
          })}
          <div style={{ padding: '7px 12px', ...mono(8), color: 'var(--fg-3)', letterSpacing: '.06em', lineHeight: 1.6 }}>
            same thread · same context · pointer card left behind — or just tell the agent "continue this in…"
          </div>
        </div>
      </span>
      {iconBtn(toggleTheme, isDark ? 'Switch to light' : 'Switch to dark', false, (
        <Icon name={isDark ? 'moon' : 'sun'} size={13} />
      ))}
      {iconBtn(toggleTrace, 'Timeline panel', traceOn, <Icon name="panelRight" size={13} />)}
    </div>
  )
}
