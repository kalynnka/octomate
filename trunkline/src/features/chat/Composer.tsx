import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { SyntheticEvent } from 'react'
import { ComposerPrimitive, useAui, useAuiState } from '@assistant-ui/react'
import { useAuth } from '@/state/auth'
import { useConsole } from '@/state/console'
import { usePermissionModes, useRoutes } from '@/lib/api/hooks'
import { channelMeta } from '@/lib/api/live'
import { Icon } from '@/components/Icon'
import { ellipsis, fieldLabel, label, microSection, mono } from '@/components/text'

const EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh'] as const

// Shared by the input and its caret mirror — the block cursor lands where the
// caret is only if both wrap text with identical metrics.
const composerType = {
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  lineHeight: 1.75,
} as const

interface RouteGroup {
  name: string
  code: string
  desc: string
  models: { id: string | null; model: string }[]
}

function RouteSelector() {
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const ntEffort = useConsole((s) => s.ntEffort)
  const ntMenu = useConsole((s) => s.ntMenu)
  const { setNtMenu, setNtRoute } = useConsole((s) => s.actions)
  const { data: routesData } = useRoutes()
  const groups: RouteGroup[] = useMemo(() => {
    const byAgent = new Map<string, { id: string | null; model: string }[]>()
    for (const r of routesData?.routes ?? []) {
      const models = byAgent.get(r.agent) ?? []
      models.push({ id: r.id, model: r.model ?? 'Harness default' })
      byAgent.set(r.agent, models)
    }
    return [...byAgent.entries()].map(([name, models]) => ({
      name,
      code: name.slice(0, 2).toUpperCase(),
      desc: 'bound agent',
      models,
    }))
  }, [routesData])
  const open = ntMenu === 'sel'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', marginRight: 2, minWidth: 0 }}>
      <span style={{ position: 'relative', display: 'inline-flex', zIndex: 76, minWidth: 0 }}>
        {open && (
          <span onClick={() => setNtMenu('sel')} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />
        )}
        <span
          onClick={() => setNtMenu('sel')}
          title="agent · model · effort — routes this session"
          className="hov-border"
          style={{
            ...label(8, '.06em'),
            background: 'var(--surface-raised)',
            border: `1px solid ${open ? 'var(--color-accent)' : 'var(--line-divider)'}`,
            borderRadius: 2,
            height: 20,
            boxSizing: 'border-box',
            padding: '0 7px',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            cursor: 'pointer',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          <span style={{ color: 'var(--color-accent)' }}>{ntAgent}</span>
          <span style={{ color: 'var(--fg-3)' }}>·</span>
          <span style={{ color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{ntModel}</span>
          <span style={{ color: 'var(--fg-3)' }}>[{ntEffort}]</span>
          <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
        </span>
        {open && (
          <span
            style={{
              position: 'absolute',
              bottom: 'calc(100% + 6px)',
              right: 0,
              width: 302,
              maxHeight: '60vh',
              overflowY: 'auto',
              // Above the strip's ntMenu click-away overlay (zIndex 75), like
              // its own menus — below it, every click lands on the overlay.
              zIndex: 80,
              background: 'var(--surface-raised)',
              border: '1px solid var(--line-color)',
              boxShadow: 'var(--shadow-soft)',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 12px 7px', borderBottom: '1px solid var(--line-divider)' }}>
              <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Agent</span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em' }}>owns this session</span>
            </span>
            {groups.map((ar) => {
              const on = ntAgent === ar.name
              return (
                <span
                  key={ar.name}
                  onClick={(e) => {
                    e.stopPropagation()
                    if (!on)
                      setNtRoute({
                        ntAgent: ar.name,
                        ntModel: ar.models[0]?.model ?? '',
                        ntRouteId: ar.models[0]?.id ?? null,
                      })
                  }}
                  className="hov-wash"
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 9,
                    padding: '8px 12px',
                    cursor: 'pointer',
                    background: on ? 'color-mix(in srgb, var(--color-accent) 7%, transparent)' : 'transparent',
                    borderBottom: '1px solid var(--line-color)',
                  }}
                >
                  <span
                    style={{
                      width: 20,
                      height: 20,
                      flexShrink: 0,
                      boxSizing: 'border-box',
                      border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                      color: on ? 'var(--color-accent)' : 'var(--fg-2)',
                      ...mono(8, 700),
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      marginTop: 1,
                    }}
                  >
                    {ar.code}
                  </span>
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <span style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
                      <span style={{ ...mono(10, 700), color: 'var(--fg-1)' }}>{ar.name}</span>
                      <span style={{ ...mono(8), color: 'var(--color-accent)' }}>{on ? ntModel : (ar.models[0]?.model ?? '')}</span>
                    </span>
                    <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.55, letterSpacing: '.02em' }}>{ar.desc}</span>
                    {on && (
                      <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4, paddingTop: 2 }}>
                        {ar.models.map((m) => {
                          const mOn = ntModel === m.model
                          return (
                            <span
                              key={m.id}
                              onClick={(e) => {
                                e.stopPropagation()
                                setNtRoute({ ntModel: m.model, ntRouteId: m.id })
                              }}
                              className="hov-border-accent"
                              style={{
                                ...mono(7.5, 700),
                                padding: '2px 6px',
                                border: `1px solid ${mOn ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                                color: mOn ? 'var(--color-accent)' : 'var(--fg-2)',
                                background: mOn ? 'color-mix(in srgb, var(--color-accent) 10%, transparent)' : 'transparent',
                                cursor: 'pointer',
                              }}
                            >
                              {m.model}
                            </span>
                          )
                        })}
                      </span>
                    )}
                  </span>
                  <span style={{ width: 12, flexShrink: 0, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right', marginTop: 1 }}>
                    {on ? '✓' : ''}
                  </span>
                </span>
              )
            })}
            <span style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '9px 12px', background: 'var(--surface-sunken)' }}>
              <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Effort</span>
              <span style={{ ...mono(8), color: 'var(--fg-3)' }}>({ntEffort})</span>
              <span style={{ flex: 1 }} />
              <span style={{ display: 'inline-flex', gap: 5, alignItems: 'center' }}>
                {EFFORTS.map((v, i) => {
                  const cur = EFFORTS.indexOf(ntEffort as (typeof EFFORTS)[number])
                  const onStep = cur >= i
                  return (
                    <span
                      key={v}
                      onClick={(e) => {
                        e.stopPropagation()
                        setNtRoute({ ntEffort: v })
                      }}
                      title={`effort_${v}`}
                      className="hov-border-accent"
                      style={{
                        width: 9,
                        height: 9,
                        boxSizing: 'border-box',
                        border: `1px solid ${onStep ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                        background: onStep ? 'var(--color-accent)' : 'transparent',
                        cursor: 'pointer',
                      }}
                    />
                  )
                })}
              </span>
            </span>
          </span>
        )}
      </span>
    </span>
  )
}

/**
 * Choose the working agent's approval posture from its menu, or cycle with ⇧⇥.
 *
 * Show the agent's configured default until the conversation chooses a mode.
 * There is no chip until an agent mode is available.
 */
function PermissionChip() {
  const ntOn = useConsole((s) => s.ntOn)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntPermissionMode = useConsole((s) => s.ntPermissionMode)
  const ntMenu = useConsole((s) => s.ntMenu)
  const ntMenuPos = useConsole((s) => s.ntMenuPos)
  const detail = useConsole((s) => s.detail)
  const { setPermissionMode, setNtMenu, closeNtMenu } = useConsole((s) => s.actions)
  const { data: vocabularies } = usePermissionModes()
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()

  const session = detail?.sessions.at(-1)
  const agent = ntOn ? ntAgent : session?.agent
  const postures = agent ? vocabularies?.[agent] : undefined
  const vocabulary = postures?.modes ?? []
  const switchable = vocabulary.length > 0
  const declared = ntOn ? ntPermissionMode : (session?.mode ?? null)
  const mode = declared ?? postures?.default ?? null
  if (mode === null) return null
  const selected = vocabulary.find((option) => option.value === mode)
  const open = ntMenu === 'perm' && switchable
  return (
    <span
      onKeyDown={(event) => {
        if (event.key === 'Escape' && open) {
          event.stopPropagation()
          closeNtMenu()
          trigger.current?.focus()
        }
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) closeNtMenu()
      }}
      style={{ position: 'relative', display: 'inline-flex', marginRight: 2, flexShrink: 0, zIndex: 76 }}
    >
      {open && (
        <span onClick={closeNtMenu} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />
      )}
      <button
        ref={trigger}
        type="button"
        disabled={!switchable}
        aria-label="Permission mode"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          setNtMenu('perm', {
            top: rect.top,
            right: Math.max(16, Math.min(window.innerWidth - rect.right, window.innerWidth - 318)),
          })
        }}
        title={selected?.description ?? 'Permission mode — ⇧⇥ to switch'}
        className={switchable ? 'hov-border' : undefined}
        style={{
          ...label(8, '.06em'),
          background: 'var(--surface-raised)',
          border: `1px solid ${open ? 'var(--color-accent)' : 'var(--line-divider)'}`,
          borderRadius: 2,
          height: 20,
          boxSizing: 'border-box',
          padding: '0 7px',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
          cursor: switchable ? 'pointer' : 'default',
          opacity: switchable ? 1 : 0.55,
          whiteSpace: 'nowrap',
          position: 'relative',
          zIndex: 76,
        }}
      >
        {switchable && <span style={{ color: 'var(--fg-3)' }}>⇧⇥</span>}
        <span style={{ color: 'var(--color-gold)' }}>{selected?.name ?? mode}</span>
        {switchable && <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1 }}>▾</span>}
      </button>
      {open && (
        <span
          id={menuId}
          role="group"
          aria-label="Permission modes"
          style={{
            position: 'fixed',
            bottom: `calc(100dvh - ${ntMenuPos.top}px + 6px)`,
            right: ntMenuPos.right,
            width: 302,
            maxWidth: 'calc(100vw - 32px)',
            maxHeight: '60vh',
            overflowY: 'auto',
            zIndex: 80,
            background: 'var(--surface-raised)',
            border: '1px solid var(--line-color)',
            boxShadow: 'var(--shadow-soft)',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 12px 7px', borderBottom: '1px solid var(--line-divider)' }}>
            <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Permissions</span>
            <span style={{ flex: 1 }} />
            <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em' }}>{agent}</span>
          </span>
          {vocabulary.map((option) => {
            const on = option.value === mode
            return (
              <button
                key={option.value}
                type="button"
                aria-pressed={on}
                autoFocus={on}
                onClick={() => {
                  void setPermissionMode(option.value)
                  closeNtMenu()
                  trigger.current?.focus()
                }}
                className="hov-wash"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 9,
                  padding: '5px 12px',
                  cursor: 'pointer',
                  background: on ? 'color-mix(in srgb, var(--color-accent) 7%, transparent)' : 'transparent',
                  border: 0,
                  borderBottom: '1px solid var(--line-color)',
                  borderRadius: 0,
                  textAlign: 'left',
                }}
              >
                <span style={{ width: 20, height: 20, flexShrink: 0, boxSizing: 'border-box', border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`, color: on ? 'var(--color-accent)' : 'var(--fg-2)', ...mono(8, 700), display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
                  {option.name.slice(0, 2).toUpperCase()}
                </span>
                <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                  <span style={{ ...mono(10, 700), color: 'var(--fg-1)' }}>{option.name}</span>
                  {option.description && <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.55, letterSpacing: '.02em' }}>{option.description}</span>}
                </span>
                <span aria-hidden="true" style={{ width: 12, flexShrink: 0, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right' }}>
                  {on ? '✓' : ''}
                </span>
              </button>
            )
          })}
          <span style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '9px 12px', background: 'var(--surface-sunken)' }}>
            <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>⇧⇥</span>
            <span style={{ ...mono(8), color: 'var(--fg-3)' }}>cycle permission modes</span>
          </span>
        </span>
      )}
    </span>
  )
}

export function Composer() {
  const aui = useAui()
  const canSend = useAuiState((s) => s.composer.canSend)
  const composerText = useAuiState((s) => s.composer.text)
  // The comp's terminal cursor: an accent block at the caret, drawn in a
  // mirror overlay (a real textarea cannot style its caret as a block).
  const [caretAt, setCaretAt] = useState<number | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const syncCaret = (e: SyntheticEvent<HTMLTextAreaElement>) =>
    setCaretAt(e.currentTarget.selectionStart)
  const selThreadId = useConsole((s) => s.selThreadId)
  const selChannel = useConsole((s) => s.selChannel)
  const detail = useConsole((s) => s.detail)
  const queue = useConsole((s) => s.queue)
  const ntOn = useConsole((s) => s.ntOn)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const ntEffort = useConsole((s) => s.ntEffort)
  const { removeQueued } = useConsole((s) => s.actions)
  const username = useAuth((s) => s.user?.username ?? 'operator')

  const isReview = useConsole((s) => s.pvOpen)
  const lastSes = detail?.sessions[detail.sessions.length - 1]
  // The channel list is the connected tentacles only, so a native channel is not
  // in it; its display name is the same table the sidebar reads.
  const routeChip = `${channelMeta(selChannel).label.toLowerCase()}/${detail?.key ?? selThreadId}`
  const modelChip = ntOn ? `${ntAgent} · ${ntModel}[${ntEffort}]` : (lastSes?.route ?? '')
  const [sesAgent, sesModel] = (lastSes?.route ?? '').split(' · ')

  useEffect(() => {
    aui.composer.setText('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selThreadId])

  const placeholder = ntOn
    ? 'first directive — registers the thread and boots a session on send'
    : isReview
      ? queue.length
        ? `add a directive — ${queue.length} note${queue.length > 1 ? 's' : ''} ride along`
        : 'directive — sweep lines to quote · + on a line to comment'
      : ''

  return (
    <div className="lt-fade-in" style={{ flexShrink: 0 }}>
      <div style={{ borderTop: '2px solid var(--trk-bracket)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: 'var(--trk-comp-head-pad, 7px 24px 6px)', borderBottom: '1px solid var(--trk-vline)' }}>
          <span style={{ ...mono(13, 700), color: 'var(--color-accent)', lineHeight: 1 }}>&gt;_</span>
          <span style={{ ...mono(10.5), ...ellipsis, minWidth: 0 }}>
            <span style={{ fontWeight: 700, color: 'var(--color-accent)' }}>{username}@trunkline</span>
            <span style={{ color: 'var(--info-strong)' }}>:{routeChip}</span>{' '}
            <span style={{ color: 'var(--fg-2)' }}>{modelChip}</span>
          </span>
          <span style={{ flex: 1 }} />
          <span
            title="markdown renders live as you type"
            style={{ ...microSection, color: 'var(--color-gold)', border: '1px solid var(--color-gold)', padding: '2px 6px', whiteSpace: 'nowrap' }}
          >
            MD·Live
          </span>
        </div>
        <div style={{ padding: 'var(--trk-comp-pad, 9px 24px 11px)' }}>
          {queue.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '0 0 8px' }}>
              {queue.map((q) => (
                <div
                  key={q.qid}
                  className="lt-fade-in"
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    padding: '5px 10px',
                    background: 'var(--card-bg)',
                    border: '1px solid var(--line-divider)',
                    boxShadow: `inset 2px 0 0 ${q.kind === 'cmt' ? 'var(--color-accent)' : 'var(--info)'}`,
                    maxWidth: 560,
                  }}
                >
                  <span
                    style={{
                      ...mono(8, 700),
                      letterSpacing: '.06em',
                      color: q.kind === 'cmt' ? 'var(--color-accent)' : 'var(--info-strong)',
                      whiteSpace: 'nowrap',
                      paddingTop: 2,
                      flexShrink: 0,
                    }}
                  >
                    {q.label}
                  </span>
                  {q.kind === 'quote' ? (
                    <span style={{ flex: 1, minWidth: 0, ...mono(9), lineHeight: 1.55, color: 'var(--fg-3)', ...ellipsis }}>{q.body}</span>
                  ) : (
                    <span style={{ flex: 1, minWidth: 0, fontFamily: "'Noto Serif SC', serif", fontSize: 11.5, fontStyle: 'italic', lineHeight: 1.5, color: 'var(--fg-2)' }}>
                      {q.body}
                    </span>
                  )}
                  <span
                    onClick={() => removeQueued(q.qid)}
                    title="Remove"
                    className="hov-red"
                    style={{ ...mono(10), color: 'var(--fg-3)', cursor: 'pointer', padding: '0 2px', flexShrink: 0 }}
                  >
                    ×
                  </span>
                </div>
              ))}
            </div>
          )}
          <ComposerPrimitive.Root style={{ display: 'block' }}>
            <span style={{ position: 'relative', display: 'block', overflow: 'hidden' }}>
              <ComposerPrimitive.Input
                rows={2}
                placeholder={placeholder}
                onFocus={syncCaret}
                onBlur={() => setCaretAt(null)}
                onSelect={syncCaret}
                onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
                style={{
                  display: 'block',
                  width: '100%',
                  boxSizing: 'border-box',
                  border: 'none',
                  outline: 'none',
                  resize: 'none',
                  background: 'transparent',
                  ...composerType,
                  color: 'var(--fg-1)',
                  // The block in the mirror is the caret (the comp's terminal
                  // cursor); the native bar only returns if tracking is off.
                  caretColor: caretAt === null ? 'var(--color-accent)' : 'transparent',
                }}
              />
              {caretAt !== null && (
                <span
                  aria-hidden
                  style={{
                    position: 'absolute',
                    inset: 0,
                    top: -scrollTop,
                    pointerEvents: 'none',
                    whiteSpace: 'pre-wrap',
                    overflowWrap: 'break-word',
                    ...composerType,
                    color: 'transparent',
                  }}
                >
                  {composerText.slice(0, caretAt)}
                  <span
                    style={{
                      display: 'inline-block',
                      width: 8,
                      height: 14,
                      background: 'var(--color-accent)',
                      verticalAlign: 'text-bottom',
                      marginLeft: 1,
                      animation: 'trkBlink 1.1s step-end infinite',
                    }}
                  />
                </span>
              )}
            </span>
          </ComposerPrimitive.Root>
        </div>
        <div className="trk-comp-hint" style={{ display: 'flex', justifyContent: 'flex-end', padding: '0 24px 5px' }}>
          <span style={{ ...mono(8), color: 'var(--fg-3)', letterSpacing: '.08em', textTransform: 'uppercase', ...ellipsis, minWidth: 0 }}>
            ↵ send · ⇧↵ newline · ⇧⇥ posture · **b** _i_ `code` ``` fence
          </span>
        </div>
        <div
          style={{ display: 'flex', alignItems: 'center', gap: 4, padding: 'var(--trk-comp-bar-pad, 3px 24px 4px 18px)', borderTop: '1px solid var(--trk-vline)' }}
        >
          <span className="trk-comp-tools" style={{ display: 'contents' }}>
            <span style={{ padding: 6, display: 'inline-flex', color: 'var(--fg-3)' }}>
              <Icon name="paperclip" size={15} />
            </span>
            <span style={{ padding: 6, display: 'inline-flex', color: 'var(--fg-3)' }}>
              <Icon name="code" size={15} />
            </span>
            <span style={{ padding: 6, display: 'inline-flex', color: 'var(--fg-3)' }}>
              <Icon name="globe" size={15} />
            </span>
            <span style={{ width: 1, height: 16, background: 'var(--trk-vline)', margin: '0 6px' }} />
            <span style={{ ...fieldLabel, color: 'var(--color-accent)' }}>Directive</span>
          </span>
          {queue.length > 0 && (
            <span style={{ ...mono(8, 700), color: 'var(--color-accent)', letterSpacing: '.08em', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
              · {String(queue.length).padStart(2, '0')} queued — sends as one turn
            </span>
          )}
          <span style={{ flex: 1 }} />
          <PermissionChip />
          {ntOn ? (
            <RouteSelector />
          ) : (
            <span
              title="agent · model — routing of this session"
              style={{
                ...label(8, '.06em'),
                background: 'var(--surface-raised)',
                border: '1px solid var(--line-divider)',
                borderRadius: 2,
                height: 20,
                boxSizing: 'border-box',
                padding: '0 7px',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                whiteSpace: 'nowrap',
                marginRight: 2,
                minWidth: 0,
              }}
            >
              <span style={{ color: 'var(--color-accent)' }}>{sesAgent || '—'}</span>
              {/* A session nothing routed carries its runtime alone, and no read
                  reports the effort a run went out at. */}
              {sesModel && (
                <>
                  <span style={{ color: 'var(--fg-3)' }}>·</span>
                  <span style={{ color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{sesModel}</span>
                </>
              )}
            </span>
          )}
          <span style={{ marginLeft: 8, flexShrink: 0 }}>
            <button
              type="button"
              onClick={() => aui.composer.send()}
              disabled={!canSend}
              className="hov-panel"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                ...label(8.5, '.16em'),
                padding: '5px 10px',
                border: '1px solid var(--color-accent)',
                background: 'var(--color-accent)',
                color: '#fff',
                cursor: canSend ? 'pointer' : 'default',
                opacity: canSend ? 1 : 0.55,
                transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear',
              }}
            >
              Send ↵
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}
