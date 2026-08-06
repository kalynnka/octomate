/**
 * Threads × channels sidebar — brand header, the 26px channel letter-rail,
 * the channel/thread tree, and the index footer. Ported from the comp's
 * "SIDEBAR: THREADS × CHANNELS" aside.
 */
import { Disclose, Fold } from '@/components/Fold'
import { Icon } from '@/components/Icon'
import { TriStripe } from '@/components/TriStripe'
import { display, ellipsis, label, mono } from '@/components/text'
import { useChannels, useThreads } from '@/lib/api/hooks'
import type { ThreadTone } from '@/lib/api/types'
import { useRailDrag } from '@/lib/useRailDrag'
import { useConsole } from '@/state/console'

const toneColor: Record<ThreadTone, string> = {
  active: 'var(--color-accent)',
  idle: 'var(--fg-3)',
  native: 'var(--color-teal)',
}

interface ThreadRow {
  id: string
  title: string
  stColor: string
  agent: string
  on: boolean
  pick?: () => void
}

export function ThreadsSidebar() {
  const channels = useChannels().data ?? []
  const threadsByCh = useThreads().data ?? {}
  const sbFold = useConsole((s) => s.sbFold)
  const chFold = useConsole((s) => s.chFold)
  const chPins = useConsole((s) => s.chPins)
  const selThreadId = useConsole((s) => s.selThreadId)
  const ntOn = useConsole((s) => s.ntOn)
  const ntStarted = useConsole((s) => s.ntStarted)
  const ntTitle = useConsole((s) => s.ntTitle)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const mgmtOpen = useConsole((s) => s.mgmtOpen)
  const widths = useConsole((s) => s.widths)
  const railDrag = useConsole((s) => s.railDrag)
  const {
    toggleSidebar,
    toggleChannelFold,
    toggleChannelPin,
    focusChannel,
    selectThread,
    startNewThread,
    toggleControl,
  } = useConsole((s) => s.actions)
  const dragStart = useRailDrag('sb', 'trk-sb-panel', 170, 420)

  const orderedCh = [...channels].sort((a, b) => {
    const pa = chPins.indexOf(a.id)
    const pb = chPins.indexOf(b.id)
    if (pa >= 0 && pb >= 0) return pa - pb
    if (pa >= 0 || pb >= 0) return pa >= 0 ? -1 : 1
    return channels.indexOf(a) - channels.indexOf(b)
  })
  const unfolded = orderedCh.filter((c) => !chFold[c.id])
  const focusId = unfolded.length === 1 ? unfolded[0].id : null
  const threadTotal = Object.values(threadsByCh).flat().length

  return (
    <aside
      id="trk-sb-panel"
      className="trk-sb"
      data-dragging={railDrag === 'sb' ? '' : undefined}
      style={{
        width: sbFold ? '26px' : widths.sb ? `${widths.sb}px` : 'clamp(200px,21vw,272px)',
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        borderRight: `1px solid ${sbFold ? 'transparent' : 'var(--line-divider)'}`,
        backgroundColor: 'var(--card-bg)',
        minHeight: 0,
        position: 'relative',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: sbFold ? 0 : '0 14px',
          height: 44,
          boxSizing: 'border-box',
          borderBottom: '1px solid var(--line-divider)',
          flexShrink: 0,
        }}
      >
        {!sbFold && (
          <>
            <span
              style={{
                ...display(17),
                lineHeight: 1,
                letterSpacing: '-.01em',
                textTransform: 'uppercase',
                color: 'var(--fg-1)',
              }}
            >
              Octomate
            </span>
            <span style={{ ...label(7.5, '.18em'), color: 'var(--fg-3)', paddingTop: 3 }}>
              v0.4.1
            </span>
            <span style={{ flex: 1 }} />
            <span
              onClick={toggleSidebar}
              title="Channels panel"
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
                color: 'var(--color-accent)',
                border: '1px solid var(--line-divider)',
              }}
            >
              <Icon name="panelLeft" size={13} />
            </span>
          </>
        )}
        {sbFold && (
          <span
            onClick={toggleSidebar}
            title="Show channels"
            className="hov-ink-wash"
            style={{
              width: 22,
              height: 22,
              boxSizing: 'border-box',
              margin: '0 auto',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
              color: 'var(--fg-2)',
              border: '1px solid var(--line-divider)',
            }}
          >
            <Icon name="panelLeft" size={13} />
          </span>
        )}
      </div>
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <span style={{ position: 'relative', width: 26, flexShrink: 0 }}>
          <span
            className="trk-chrail"
            style={{
              position: 'absolute',
              left: 0,
              top: 0,
              bottom: 0,
              width: 26,
              zIndex: 20,
              display: 'flex',
              flexDirection: 'column',
              gap: 2,
              padding: '8px 0 8px 3px',
              background: 'var(--card-bg-hover)',
              borderRight: '1px solid var(--line-divider)',
              overflow: 'hidden',
              boxSizing: 'border-box',
            }}
          >
            {orderedCh.map((c) => {
              const on = focusId === c.id
              return (
                <span
                  key={c.id}
                  onClick={() =>
                    focusChannel(
                      c.id,
                      channels.map((x) => x.id),
                    )
                  }
                  title={`Expand only ${c.label}`}
                  className="hov-wash"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 7,
                    cursor: 'pointer',
                    flexShrink: 0,
                    padding: '1px 8px 1px 2px',
                  }}
                >
                  <span
                    style={{
                      width: 18,
                      height: 18,
                      flexShrink: 0,
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      ...mono(8, 700),
                      color: on ? '#F6F1E5' : c.brand,
                      background: on ? c.brand : 'transparent',
                      border: `1px solid ${on ? c.brand : 'transparent'}`,
                      boxSizing: 'border-box',
                    }}
                  >
                    {c.id === 'codex' ? 'X' : c.label[0].toUpperCase()}
                  </span>
                  <span
                    style={{
                      ...label(8, '.12em'),
                      color: c.brand,
                      whiteSpace: 'nowrap',
                      flex: 1,
                      textAlign: 'left',
                    }}
                  >
                    {c.label}
                  </span>
                  <span
                    style={{
                      ...mono(7.5),
                      color: 'var(--fg-3)',
                      whiteSpace: 'nowrap',
                      textAlign: 'right',
                    }}
                  >
                    {(threadsByCh[c.id] ?? []).length}
                  </span>
                </span>
              )
            })}
            <span style={{ flex: 1 }} />
            <span
              onClick={toggleControl}
              title="Control"
              className="hov-wash"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 7,
                cursor: 'pointer',
                flexShrink: 0,
                padding: '1px 8px 1px 2px',
              }}
            >
              <span
                style={{
                  width: 18,
                  height: 18,
                  flexShrink: 0,
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: mgmtOpen ? 'var(--color-accent)' : 'var(--fg-2)',
                }}
              >
                <Icon name="gear" size={13} />
              </span>
              <span
                style={{ ...label(8, '.12em'), color: 'var(--fg-2)', whiteSpace: 'nowrap', flex: 1 }}
              >
                Control
              </span>
            </span>
          </span>
        </span>
        <div
          style={{
            display: sbFold ? 'none' : 'block',
            flex: 1,
            overflowY: 'auto',
            minHeight: 0,
            padding: '4px 0 10px',
          }}
        >
          {orderedCh.map((c) => {
            const folded = !!chFold[c.id]
            const pinned = chPins.includes(c.id)
            const ths = threadsByCh[c.id] ?? []
            const rows: ThreadRow[] = [
              ...(c.id === 'dev_ui' && ntOn
                ? [
                    {
                      id: ntStarted ? selThreadId : 'THR-NEW',
                      title: ntTitle || 'untitled — new thread',
                      stColor: 'var(--color-gold)',
                      agent: `${ntAgent} · ${ntModel}`,
                      on: true,
                    },
                  ]
                : []),
              ...ths.map((t) => ({
                id: t.id,
                title: t.title,
                stColor: toneColor[t.tone],
                agent: t.agentLabel,
                on: !ntOn && t.id === selThreadId,
                pick: () => selectThread(c.id, t.id),
              })),
            ]
            return (
              <div key={c.id}>
                <div
                  onClick={() => toggleChannelFold(c.id)}
                  className="hov-wash"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 7,
                    padding: '11px 14px 5px',
                    cursor: 'pointer',
                  }}
                >
                  <Disclose
                    open={!folded}
                    style={{ ...mono(8), color: 'var(--fg-3)', width: 9, flexShrink: 0 }}
                  />
                  <span style={{ ...label(10, '.16em'), color: c.brand }}>{c.label}</span>
                  <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
                  <span style={{ ...mono(8), color: 'var(--fg-3)' }}>
                    {folded ? `${ths.length} thr` : c.sub}
                  </span>
                  <span
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleChannelPin(c.id)
                    }}
                    title={pinned ? 'Unpin' : 'Pin to top'}
                    className="hov-op"
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      flexShrink: 0,
                      color: pinned ? 'var(--color-accent)' : 'var(--fg-3)',
                      opacity: pinned ? 1 : 0.4,
                      cursor: 'pointer',
                      padding: 1,
                    }}
                  >
                    <Icon name="pin" size={10} />
                  </span>
                </div>
                <Fold open={!folded}>
                  {c.id === 'dev_ui' && (
                    <span
                      onClick={(e) => {
                        e.stopPropagation()
                        startNewThread()
                      }}
                      title="New web thread"
                      className="hov-accent-border-wash"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 5,
                        margin: '3px 14px 3px 30px',
                        height: 20,
                        boxSizing: 'border-box',
                        border: '1px dashed color-mix(in srgb, var(--color-accent) 55%, transparent)',
                        color: 'var(--color-accent)',
                        ...label(8, '.14em'),
                        cursor: 'pointer',
                      }}
                    >
                      + new thread
                    </span>
                  )}
                  {rows.map((t) => (
                    <div
                      key={t.id}
                      onClick={t.pick}
                      className="hov-wash"
                      style={{ position: 'relative', padding: '7px 14px 7px 30px', cursor: 'pointer' }}
                    >
                      {t.on && (
                        <>
                          <span
                            style={{
                              position: 'absolute',
                              inset: 0,
                              background: 'var(--hover)',
                              pointerEvents: 'none',
                            }}
                          />
                          <span
                            style={{
                              position: 'absolute',
                              left: 0,
                              top: 0,
                              bottom: 0,
                              width: 3,
                              background: c.brand,
                            }}
                          />
                        </>
                      )}
                      <div
                        style={{ display: 'flex', alignItems: 'center', gap: 7, position: 'relative' }}
                      >
                        <i
                          style={{
                            width: 5,
                            height: 5,
                            borderRadius: 9999,
                            background: t.stColor,
                            flexShrink: 0,
                          }}
                        />
                        <span style={{ fontSize: 12, color: 'var(--fg-1)', ...ellipsis, flex: 1 }}>
                          {t.title}
                        </span>
                      </div>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 7,
                          marginTop: 3,
                          paddingLeft: 12,
                          position: 'relative',
                        }}
                      >
                        <span style={{ ...mono(8.5, 700), color: c.brand, whiteSpace: 'nowrap', flexShrink: 0 }}>
                          {t.id}
                        </span>
                        <span style={{ ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>
                          {t.agent}
                        </span>
                      </div>
                    </div>
                  ))}
                </Fold>
              </div>
            )
          })}
        </div>
      </div>
      <div
        style={{
          flexShrink: 0,
          borderTop: '1px solid var(--line-divider)',
          height: 30,
          boxSizing: 'border-box',
          padding: '0 16px',
          display: sbFold ? 'none' : 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <span
          style={{
            ...mono(8, 700),
            letterSpacing: '.1em',
            color: 'var(--fg-1)',
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          {threadTotal} thr · {channels.length} ch
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(7.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>
          {'// end of index'}
        </span>
      </div>
      <TriStripe />
      {!sbFold && (
        <span
          onMouseDown={dragStart}
          title="Drag to resize"
          className="hov-resize"
          style={{
            position: 'absolute',
            top: 0,
            right: 0,
            bottom: 0,
            width: 6,
            cursor: 'col-resize',
            zIndex: 40,
          }}
        />
      )}
    </aside>
  )
}
