import { useConsole } from '@/state/console'
import type { ControlSection } from '@/state/console'
import { useRailDrag } from '@/lib/useRailDrag'
import { TriStripe } from '@/components/TriStripe'
import { ellipsis, label, mono } from '@/components/text'

// Hints name what a section holds, never how much of it: no read counts any of
// this yet, and a number here would be one nobody measured.
const defs: { id: ControlSection; label: string; hint: string }[] = [
  { id: 'agents', label: 'Agents', hint: 'routes · models' },
  { id: 'mcp', label: 'MCP', hint: 'servers · connectors' },
  { id: 'users', label: 'Users', hint: 'identities · grants' },
  { id: 'dash', label: 'Dashboard', hint: 'ledger · verbs' },
  { id: 'settings', label: 'Settings', hint: 'providers · hooks' },
]

/** Management rail — the Control sections index beside the sidebar. */
export function ControlRail() {
  const mgmtOpen = useConsole((s) => s.mgmtOpen)
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const railDrag = useConsole((s) => s.railDrag)
  const mgmtW = useConsole((s) => s.widths.mgmt)
  const { toggleControl, setControlSection } = useConsole((s) => s.actions)
  const dragStart = useRailDrag('mgmt', 'trk-mgmt-panel', 180, 420)

  return (
    <aside
      id="trk-mgmt-panel"
      className="trk-rail"
      data-folded={mgmtOpen ? undefined : ''}
      data-dragging={railDrag === 'mgmt' ? 'true' : undefined}
      style={{
        width: mgmtW ? `${mgmtW}px` : 'clamp(200px,21vw,272px)',
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        position: 'relative',
        backgroundColor: 'var(--card-bg)',
        boxShadow: 'inset -1px 0 0 var(--line-divider)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 14px',
          height: 44,
          boxSizing: 'border-box',
          borderBottom: '1px solid var(--line-divider)',
          flexShrink: 0,
        }}
      >
        <span style={{ ...label(10, '.22em'), color: 'var(--fg-1)', lineHeight: '17px' }}>
          Control
        </span>
        <span style={{ ...label(8, '.14em'), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>
          serves the chat
        </span>
        <span style={{ flex: 1 }} />
        <span
          onClick={toggleControl}
          title="Collapse"
          className="hov-border-line"
          style={{
            width: 20,
            height: 20,
            flexShrink: 0,
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'pointer',
            color: 'var(--fg-2)',
            fontFamily: 'var(--font-display)',
            fontSize: 12,
            border: '1px solid transparent',
          }}
        >
          ←
        </span>
      </div>
      <div
        style={{
          margin: '8px 14px 4px',
          height: 8,
          backgroundImage:
            'repeating-linear-gradient(115deg, var(--color-accent) 0 2px, transparent 2px 9px)',
          opacity: 0.45,
          flexShrink: 0,
        }}
      />
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, paddingBottom: 8 }}>
        {defs.map((d, i) => {
          const on = mgmtSec === d.id
          return (
            <div
              key={d.id}
              onClick={() => setControlSection(d.id)}
              className="hov-wash"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '10px 16px 10px 13px',
                cursor: 'pointer',
                borderLeft: `3px solid ${on ? 'var(--color-accent)' : 'transparent'}`,
                background: on ? 'var(--card-bg-hover)' : 'transparent',
              }}
            >
              <span
                style={{
                  ...mono(10, 700),
                  letterSpacing: '.1em',
                  color: on ? 'var(--color-accent)' : 'var(--fg-3)',
                  width: 26,
                  flexShrink: 0,
                }}
              >
                {`${d.label.charAt(0)}0${i + 1}`}
              </span>
              <span
                style={{
                  ...mono(12, on ? 700 : 500),
                  color: on ? 'var(--fg-1)' : 'var(--fg-2)',
                  flexShrink: 0,
                }}
              >
                {d.label}
              </span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
                {d.hint}
              </span>
              {on && (
                <i
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 9999,
                    background: 'var(--color-accent)',
                    flexShrink: 0,
                  }}
                />
              )}
            </div>
          )
        })}
      </div>
      <div
        style={{
          flexShrink: 0,
          borderTop: '1px solid var(--line-divider)',
          padding: '7px 16px 9px',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <span style={{ ...mono(8, 700), letterSpacing: '.1em', color: 'var(--fg-1)', flexShrink: 0 }}>
          127.0.0.1:8000
        </span>
        <span style={{ flex: 1 }} />
        <span
          title="default.yaml → octomate.yaml → OCTOMATE__* env"
          style={{ ...mono(7.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}
        >
          default.yaml → octomate.yaml → env
        </span>
      </div>
      <TriStripe />
      <span
        onMouseDown={dragStart}
        title="Drag to resize"
        className="hov-resize"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, width: 6, cursor: 'col-resize', zIndex: 40 }}
      />
    </aside>
  )
}
