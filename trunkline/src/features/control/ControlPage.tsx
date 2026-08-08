import { useConsole } from '@/state/console'
import { awaitingEndpoint } from '@/lib/api'
import type { ControlSection } from '@/state/console'
import { TriStripeInline } from '@/components/TriStripe'
import { BarChart } from '@/components/BarChart'
import { chipLabel, display, ellipsis, label, microMeta, microSection, mono, sectionLabel, statusNote } from '@/components/text'
import type { ControlData, EffortStep } from '@/lib/api/types'

const pages: Record<Exclude<ControlSection, ''>, { num: string; title: string }> = {
  agents: { num: 'A01', title: 'Agents' },
  mcp: { num: 'M02', title: 'MCP' },
  users: { num: 'U03', title: 'Users' },
  dash: { num: 'D04', title: 'Dashboard' },
  settings: { num: 'S05', title: 'Settings' },
}

const effortScale: EffortStep[] = ['minimal', 'low', 'medium', 'high', 'xhigh']

const tagColor: Record<'accent' | 'teal', string> = {
  accent: 'var(--color-accent)',
  teal: 'var(--color-teal)',
}
const stateDot: Record<'accent' | 'sage', string> = {
  accent: 'var(--color-accent)',
  sage: 'var(--color-sage)',
}
const kindColor: Record<'registered' | 'pseudo' | 'observed', string> = {
  registered: 'var(--color-accent)',
  pseudo: 'var(--color-teal)',
  observed: 'var(--fg-3)',
}
const connChip: Record<'connected' | 'pending' | 'cold', { text: string; color: string }> = {
  connected: { text: '● connected', color: 'var(--color-sage)' },
  pending: { text: '◐ pending', color: 'var(--color-gold)' },
  cold: { text: '○ cold', color: 'var(--fg-3)' },
}
const providerDot: Record<'sage' | 'teal' | 'ghost', string> = {
  sage: 'var(--color-sage)',
  teal: 'var(--color-teal)',
  ghost: 'var(--fg-3)',
}
const kvColor: Record<'ink' | 'sage' | 'ghost', string> = {
  ink: 'var(--fg-1)',
  sage: 'var(--color-sage)',
  ghost: 'var(--fg-3)',
}

/** Full-column Control page shown in the main column when a section is picked. */
export function ControlPage() {
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const { goChat } = useConsole((s) => s.actions)
  const data = awaitingEndpoint<ControlData>()
  if (!mgmtSec) return null
  const page = pages[mgmtSec]

  return (
    <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
      <div
        id="trk-page"
        className="lt-entry"
        style={{ maxWidth: 820, margin: '0 auto', padding: '26px 32px 56px' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span
            onClick={goChat}
            className="hov-invert-ink"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 7,
              ...label(8.5, '.14em'),
              color: 'var(--fg-1)',
              border: '1px solid var(--color-ink)',
              padding: '0 10px',
              height: 24,
              boxSizing: 'border-box',
              cursor: 'pointer',
              background: 'var(--card-bg)',
            }}
          >
            ← Back to chat
          </span>
          <span style={{ flex: 1 }} />
          <span style={{ ...statusNote, color: 'var(--fg-3)' }}>Control / {page.num}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 20 }}>
          <span style={{ ...mono(12, 700), color: 'var(--color-accent)' }}>{page.num}</span>
          <span
            style={{
              ...display(30),
              lineHeight: 1,
              letterSpacing: '-.01em',
              textTransform: 'uppercase',
              color: 'var(--fg-1)',
            }}
          >
            {page.title}
          </span>
        </div>
        <TriStripeInline style={{ width: 64, margin: '14px 0 22px' }} />
        <div
          style={{
            border: '1px solid var(--line-divider)',
            background: 'var(--card-bg)',
            boxShadow: 'var(--shadow-soft)',
          }}
        >
          {data && mgmtSec === 'agents' && (
            <div
              className="lt-entry"
              style={{
                borderTop: '1px solid var(--line-color)',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--surface-sunken)',
              }}
            >
              {data.agents.map((a) => (
                <div key={a.name} style={{ padding: '10px 16px', borderBottom: '1px solid var(--line-color)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ ...display(13, 600), textTransform: 'uppercase', color: 'var(--fg-1)' }}>
                      {a.name}
                    </span>
                    <span
                      style={{
                        ...label(7, '.14em'),
                        color: tagColor[a.tagTone],
                        border: `1px solid ${tagColor[a.tagTone]}`,
                        padding: '1.5px 5px',
                      }}
                    >
                      {a.tag}
                    </span>
                    <span style={{ flex: 1 }} />
                    <i style={{ width: 5, height: 5, borderRadius: 9999, background: stateDot[a.stateTone] }} />
                    <span style={{ ...microMeta, color: 'var(--fg-2)' }}>{a.state}</span>
                  </div>
                  {a.models.map((m) => (
                    <div key={m.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 0 2px' }}>
                      <span
                        style={{
                          ...mono(9.5, 700),
                          color: 'var(--color-accent)',
                          width: 140,
                          flexShrink: 0,
                          ...ellipsis,
                        }}
                      >
                        {m.name}
                      </span>
                      <span style={{ display: 'flex', gap: 2 }}>
                        {effortScale.map((e) => {
                          const on = m.efforts.includes(e)
                          return (
                            <span
                              key={e}
                              title={e}
                              style={{
                                width: 13,
                                height: 7,
                                border: `1px solid ${on ? 'var(--color-accent)' : 'var(--color-border)'}`,
                                background: on ? 'var(--color-accent)' : 'transparent',
                                display: 'inline-block',
                              }}
                            />
                          )
                        })}
                      </span>
                    </div>
                  ))}
                  <div style={{ ...mono(8), color: 'var(--fg-3)', marginTop: 5, paddingLeft: 2 }}>{a.miniNote}</div>
                </div>
              ))}
            </div>
          )}

          {data && mgmtSec === 'mcp' && (
            <div
              className="lt-entry"
              style={{
                borderTop: '1px solid var(--line-color)',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--surface-sunken)',
                padding: '6px 0',
              }}
            >
              {data.mcpRows.map((r) => (
                <div key={r.key} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 16px' }}>
                  <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', width: 58, flexShrink: 0 }}>{r.key}</span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, ...ellipsis }}>{r.url}</span>
                  <span
                    style={{
                      ...mono(8),
                      color: 'var(--fg-2)',
                      border: '1px solid var(--line-divider)',
                      padding: '1px 5px',
                    }}
                  >
                    {r.prefix}
                  </span>
                  <span style={{ ...mono(8, 700), color: 'var(--fg-2)', whiteSpace: 'nowrap' }}>{r.status}</span>
                </div>
              ))}
              <div
                style={{
                  borderTop: '1px solid var(--line-color)',
                  margin: '5px 16px 0',
                  padding: '6px 0 2px',
                  ...sectionLabel,
                  color: 'var(--fg-3)',
                }}
              >
                Integrations · per-user oauth
              </div>
              {data.integrations.map((i) => (
                <div key={i.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 16px' }}>
                  <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', width: 58, flexShrink: 0 }}>{i.name}</span>
                  <span
                    style={{
                      ...label(7.5, '.1em'),
                      color: 'var(--color-teal)',
                      border: '1px solid var(--color-teal)',
                      padding: '1px 5px',
                    }}
                  >
                    {i.flow}
                  </span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, ...ellipsis }}>{i.line}</span>
                </div>
              ))}
              <div
                style={{
                  borderTop: '1px solid var(--line-color)',
                  margin: '5px 16px 0',
                  padding: '6px 0 2px',
                  ...sectionLabel,
                  color: 'var(--fg-3)',
                }}
              >
                Connections
              </div>
              {data.connections.map((c) => (
                <div
                  key={`${c.user}-${c.connector}`}
                  style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4.5px 16px' }}
                >
                  <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', width: 58, flexShrink: 0 }}>@{c.user}</span>
                  <span style={{ ...mono(8.5), color: 'var(--fg-2)', width: 56, flexShrink: 0 }}>{c.connector}</span>
                  <span style={{ ...label(7.5, '.1em'), color: connChip[c.state].color }}>
                    {connChip[c.state].text}
                  </span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, textAlign: 'right', ...ellipsis }}>
                    {c.note}
                  </span>
                </div>
              ))}
            </div>
          )}

          {data && mgmtSec === 'users' && (
            <div
              className="lt-entry"
              style={{
                borderTop: '1px solid var(--line-color)',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--surface-sunken)',
                padding: '6px 0',
              }}
            >
              {data.users.map((u) => (
                <div key={u.username} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 16px' }}>
                  <span
                    style={{
                      ...mono(9.5, 700),
                      color: 'var(--color-accent)',
                      width: 80,
                      flexShrink: 0,
                      ...ellipsis,
                    }}
                  >
                    {u.username}
                  </span>
                  <span
                    style={{
                      ...chipLabel,
                      color: kindColor[u.kind],
                      border: `1px solid ${kindColor[u.kind]}`,
                      padding: '1px 4px',
                      flexShrink: 0,
                    }}
                  >
                    {u.kind}
                  </span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, ...ellipsis }}>{u.profileLine}</span>
                  <span style={{ ...mono(8, 700), color: 'var(--color-sage)', flexShrink: 0 }}>{u.oauth}</span>
                </div>
              ))}
            </div>
          )}

          {data && mgmtSec === 'dash' && (
            <div
              className="lt-entry"
              style={{
                borderTop: '1px solid var(--line-color)',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--surface-sunken)',
                padding: '10px 16px',
              }}
            >
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                {data.stats.map((s) => (
                  <div
                    key={s.k}
                    style={{ border: '1px solid var(--line-divider)', background: 'var(--card-bg)', padding: '8px 10px' }}
                  >
                    <div style={{ ...microSection, color: 'var(--fg-3)' }}>{s.k}</div>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginTop: 3 }}>
                      <span style={{ ...display(21, 400), lineHeight: 1, color: 'var(--fg-1)' }}>{s.v}</span>
                      <span style={{ ...mono(7.5), color: 'var(--fg-2)' }}>{s.sub}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div
                style={{
                  marginTop: 10,
                  border: '1px solid var(--line-divider)',
                  background: 'var(--card-bg)',
                  padding: '10px 12px',
                }}
              >
                <div style={{ ...microSection, color: 'var(--fg-3)', marginBottom: 8 }}>Gateway verbs · 7d</div>
                <BarChart data={data.verbBars} height={90} />
              </div>
              <div style={{ marginTop: 10, border: '1px solid var(--line-divider)', background: 'var(--card-bg)' }}>
                <div
                  style={{
                    padding: '7px 12px',
                    borderBottom: '1px solid var(--line-divider)',
                    ...microSection,
                    color: 'var(--fg-3)',
                  }}
                >
                  Relay activity
                </div>
                {data.feed.map((fd, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, padding: '4.5px 12px', alignItems: 'baseline' }}>
                    <span style={{ ...mono(8), color: 'var(--fg-3)', flexShrink: 0 }}>{fd.t}</span>
                    <span
                      style={{
                        ...chipLabel,
                        color: 'var(--color-accent)',
                        border: '1px solid var(--color-accent)',
                        padding: '1px 4px',
                        flexShrink: 0,
                        width: 58,
                        textAlign: 'center',
                      }}
                    >
                      {fd.verb}
                    </span>
                    <span style={{ fontSize: 10.5, color: 'var(--fg-2)', lineHeight: 1.5, ...ellipsis }}>{fd.text}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {data && mgmtSec === 'settings' && (
            <div
              className="lt-entry"
              style={{
                borderTop: '1px solid var(--line-color)',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--surface-sunken)',
                padding: '6px 0',
              }}
            >
              <div style={{ padding: '4px 16px 2px', ...sectionLabel, color: 'var(--fg-3)' }}>Providers</div>
              {data.providers.map((p) => (
                <div key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 16px' }}>
                  <i
                    style={{
                      width: 5,
                      height: 5,
                      borderRadius: 9999,
                      background: providerDot[p.tone],
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ ...label(9, '.08em'), color: 'var(--fg-1)', width: 70 }}>{p.name}</span>
                  <span style={{ ...mono(8), color: 'var(--fg-2)', width: 48 }}>{p.status}</span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flex: 1, ...ellipsis }}>{p.src}</span>
                </div>
              ))}
              <div
                style={{
                  borderTop: '1px solid var(--line-color)',
                  margin: '6px 16px 0',
                  padding: '6px 0 2px',
                  ...sectionLabel,
                  color: 'var(--fg-3)',
                }}
              >
                Native session hooks
              </div>
              {data.hookRows.map((r) => (
                <div key={r.k} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '4px 16px' }}>
                  <span style={{ ...mono(8.5), color: 'var(--fg-3)', flexShrink: 0 }}>{r.k}</span>
                  <span
                    style={{
                      ...mono(8.5, 700),
                      color: kvColor[r.tone],
                      textAlign: 'right',
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {r.v}
                  </span>
                </div>
              ))}
              <div
                style={{
                  borderTop: '1px solid var(--line-color)',
                  margin: '6px 16px 0',
                  padding: '6px 0 4px',
                  ...sectionLabel,
                  color: 'var(--fg-3)',
                }}
              >
                Tool output bands
              </div>
              <div style={{ margin: '0 16px' }}>
                <div style={{ display: 'flex', height: 18, border: '1px solid var(--color-ink)' }}>
                  <div
                    style={{
                      flex: 2,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      ...chipLabel,
                      color: 'var(--fg-2)',
                    }}
                  >
                    pass
                  </div>
                  <div
                    style={{
                      flex: 3,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      ...chipLabel,
                      color: 'var(--trk-on-fill)',
                      background: 'var(--color-gold)',
                    }}
                  >
                    spill &gt;10k
                  </div>
                  <div
                    style={{
                      flex: 2,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      ...chipLabel,
                      color: 'var(--trk-on-fill)',
                      background: 'var(--color-red)',
                    }}
                  >
                    summarize &gt;100k
                  </div>
                </div>
                <div style={{ ...mono(8), color: 'var(--fg-3)', padding: '4px 0 2px' }}>
                  spill: preview 1,000 ch · lossless · retention 6h
                </div>
              </div>
              <div
                style={{
                  borderTop: '1px solid var(--line-color)',
                  margin: '6px 16px 0',
                  padding: '6px 0 2px',
                  ...sectionLabel,
                  color: 'var(--fg-3)',
                }}
              >
                Observability
              </div>
              {data.obsRows.map((r) => (
                <div key={r.k} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '4px 16px' }}>
                  <span style={{ ...mono(8.5), color: 'var(--fg-3)' }}>{r.k}</span>
                  <span style={{ ...mono(8.5, 700), color: kvColor[r.tone] }}>{r.v}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
