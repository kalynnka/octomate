import { useId, useRef, useState } from 'react'
import { useConsole } from '@/state/console'
import { useThreadOperations, useThreads } from '@/lib/api/hooks'
import { channelMeta } from '@/lib/api/live'
import { Icon } from '@/components/Icon'
import { ellipsis, label, mono } from '@/components/text'

const operations = {
  teleport: { label: 'Teleport', description: 'Carry this chat to another destination with the same agent and history.' },
  summon: { label: 'Summon', description: 'Let another agent take over from a prepared brief.' },
  fork: { label: 'Fork', description: 'Start an independent thread with this history. The original stays available.' },
}
type Operation = keyof typeof operations
const operationOrder: Operation[] = ['teleport', 'summon', 'fork']

export function ChatHeader() {
  const selThreadId = useConsole((s) => s.selThreadId)
  const selChannel = useConsole((s) => s.selChannel)
  const detail = useConsole((s) => s.detail)
  const ntOn = useConsole((s) => s.ntOn)
  const ntTitle = useConsole((s) => s.ntTitle)
  const pending = useConsole((s) => s.gatewayPending)
  const forkPending = useConsole((s) => s.forkPending)
  const running = useConsole((s) => s.running)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const theme = useConsole((s) => s.theme)
  const sysDark = useConsole((s) => s.sysDark)
  const gatewayMode = useConsole((s) => s.gatewayMode)
  const { toggleTheme, toggleTrace, setGatewayMode, fork } = useConsole((s) => s.actions)
  const eligibility = useThreadOperations(selThreadId, Boolean(selThreadId && detail && !ntOn && !running && !pending))
  // The op the composer is expanded for, when it was opened on this thread.
  const expanded = !ntOn && gatewayMode?.threadId === selThreadId ? gatewayMode.action : null
  const expand = (action: 'teleport' | 'summon') => {
    void eligibility.refetch()
    setGatewayMode({ threadId: selThreadId, action })
  }
  const { data: threads } = useThreads()
  const [choice, setChoice] = useState<Operation | null>(null)
  const [menu, setMenu] = useState<{ threadId: string; kind: 'operations' } | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const openMenu = menu?.threadId === selThreadId ? menu.kind : null
  const busyReason = forkPending ? 'Wait for the fork to finish.'
    : pending ? `Wait for ${pending.action} to finish.` : running ? 'Wait for the current run to finish.' : undefined
  const threadReason = ntOn || !detail ? 'Open an existing thread first.' : undefined
  const loadingReason = eligibility.isError ? eligibility.error.message : !eligibility.data ? 'Checking available destinations…' : undefined
  const unavailable: Record<Operation, string | undefined> = {
    fork: threadReason ?? (detail?.canFork ? undefined : 'Fork is available for native Codex threads only.'),
    // The relay's reason is the whole answer: an empty suggestion list leaves
    // an in-place Summon, or a destination found by browsing, still open.
    teleport: threadReason ?? loadingReason ?? eligibility.data?.teleport.reason ?? undefined,
    summon: threadReason ?? loadingReason ?? eligibility.data?.summon.reason ?? undefined,
  }
  const operation = choice && !unavailable[choice]
    ? choice
    : operationOrder.find((value) => !unavailable[value]) ?? 'teleport'
  const selected = operations[operation]
  const disabledReason = busyReason ?? unavailable[operation]
  const isDark = theme === 'dark' || (theme === 'auto' && sysDark)
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
        width: 'var(--trk-btn, 22px)',
        height: 'var(--trk-btn, 22px)',
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
        padding: 'var(--trk-head-pad, 0 16px)',
        height: 'var(--trk-head-h, 44px)',
        boxSizing: 'border-box',
        borderBottom: '1px solid var(--line-divider)',
        flexShrink: 0,
        position: 'relative',
        zIndex: 80,
      }}
    >
      <span className="trk-head-title">
        <span style={{ ...label(10, '.14em'), color: 'var(--fg-1)', minWidth: 0, maxWidth: '100%', ...ellipsis }}>{title}</span>
        <span style={{ ...mono(8.5, 700), color: 'var(--color-accent)', flexShrink: 0 }}>{detail?.key ?? selThreadId}</span>
      </span>
      <span
        className="trk-head-chip"
        title="Current surface"
        style={{
          ...label(8.5, '.1em'), color: 'var(--fg-2)', border: '1px solid var(--line-divider)', padding: '0 7px',
          height: 'var(--trk-btn, 22px)', boxSizing: 'border-box', display: 'inline-flex', alignItems: 'center',
          whiteSpace: 'nowrap', flexShrink: 0,
        }}
      >
        {channelMeta(selChannel).label}
      </span>
      <span
        onKeyDown={(event) => {
          if (event.key === 'Escape' && openMenu) {
            event.stopPropagation()
            setMenu(null)
            trigger.current?.focus()
          }
        }}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setMenu(null)
        }}
        style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}
      >
        {openMenu && (
          <span onClick={() => setMenu(null)} style={{ position: 'fixed', inset: 0, zIndex: 59 }} />
        )}
        <button
          type="button"
          aria-disabled={Boolean(disabledReason)}
          aria-description={disabledReason}
          aria-pressed={operation === 'fork' ? undefined : expanded === operation}
          title={disabledReason ?? (expanded === operation ? 'Back to chat' : undefined)}
          onClick={() => {
            if (disabledReason) return
            setMenu(null)
            if (operation === 'fork') void fork(selThreadId)
            else if (expanded === operation) setGatewayMode(null)
            else expand(operation)
          }}
          className={disabledReason ? undefined : 'hov-teal-ghost'}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            flexShrink: 0, justifyContent: 'center', whiteSpace: 'nowrap',
            ...label(8.5, '.1em'), color: 'var(--trk-on-fill)',
            background: 'var(--color-teal)', border: '1px solid var(--color-teal)',
            padding: '0 9px', height: 'var(--trk-btn, 22px)', boxSizing: 'border-box',
            cursor: disabledReason ? 'not-allowed' : 'pointer',
            position: 'relative', zIndex: 60,
          }}
        >
          <span key={operation} className="trk-operation-label">
            <Icon name={operation === 'teleport' ? 'arrowRightLeft' : operation === 'fork' ? 'gitFork' : 'wandSparkles'} size={12} style={{ flexShrink: 0 }} />
            {forkPending === selThreadId ? (
              <span aria-label="Forking" style={{ display: 'inline-flex', alignItems: 'baseline' }}>
                Forking<span className="lt-fork-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
              </span>
            ) : pending?.threadId === selThreadId ? (
              <span aria-label={pending.action === 'teleport' ? 'Teleporting' : 'Summoning'}>
                {pending.action === 'teleport' ? 'Moving' : 'Summon'}<span className="lt-fork-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
              </span>
            ) : selected.label}
          </span>
        </button>
        <button
          ref={trigger}
          type="button"
          disabled={Boolean(forkPending || pending)}
          aria-label="Choose thread operation"
          aria-expanded={openMenu === 'operations'}
          aria-controls={openMenu === 'operations' ? menuId : undefined}
          onClick={() => {
            if (!running && !ntOn && detail) void eligibility.refetch()
            setMenu(openMenu === 'operations' ? null : { threadId: selThreadId, kind: 'operations' })
          }}
          className="hov-teal-ghost"
          style={{
            ...label(8, '.1em'), color: 'var(--trk-on-fill)',
            background: 'var(--color-teal)', border: '1px solid var(--color-teal)',
            width: 20, marginLeft: 2, flexShrink: 0, padding: 0,
            height: 'var(--trk-btn, 22px)', boxSizing: 'border-box', cursor: 'pointer',
            position: 'relative', zIndex: 60,
          }}
        >
          ▾
        </button>
        {openMenu === 'operations' && (
          <div
            id={menuId}
            role="group"
            aria-label="Thread operations"
            className="lt-menu"
            data-open=""
            style={{
              position: 'absolute', right: 0, top: 'calc(100% + 6px)',
              width: 340, maxWidth: 'calc(100vw - 32px)',
              background: 'var(--surface-raised)', border: '1px solid var(--line-divider)',
              boxShadow: 'var(--shadow-card)', zIndex: 60,
            }}
          >
            {operationOrder.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={operation === value}
                aria-disabled={Boolean(unavailable[value])}
                title={unavailable[value]}
                onClick={() => {
                  if (unavailable[value]) return
                  setChoice(value)
                  setMenu(null)
                  // Picking an op that fills the composer goes straight to it;
                  // Fork acts at once, so it waits for the button.
                  if (value !== 'fork' && !busyReason) return expand(value)
                  trigger.current?.focus()
                }}
                className={unavailable[value] ? undefined : 'hov-wash'}
                style={{
                  display: 'flex', gap: 12, width: '100%',
                  padding: '12px 16px', textAlign: 'left', cursor: unavailable[value] ? 'not-allowed' : 'pointer',
                  background: operation === value ? 'color-mix(in srgb, var(--color-teal) 8%, transparent)' : 'transparent',
                  border: 0, borderBottom: '1px solid var(--line-divider)', borderRadius: 0,
                }}
              >
                <span style={{ display: 'flex', flexDirection: 'column', gap: 5, flex: 1, minWidth: 0 }}>
                  <span style={{ ...label(10, '.16em'), color: operation === value ? 'var(--color-teal)' : unavailable[value] ? 'var(--fg-3)' : 'var(--fg-1)' }}>
                    {operations[value].label}
                  </span>
                  <span style={{ ...mono(9.5), color: unavailable[value] ? 'var(--fg-3)' : 'var(--fg-2)', lineHeight: 1.6, textWrap: 'pretty' }}>
                    {operations[value].description}
                  </span>
                  {unavailable[value] && <span style={{ ...mono(8.5), color: 'var(--fg-3)', lineHeight: 1.6 }}>▸ {unavailable[value]}</span>}
                </span>
                <span aria-hidden="true" style={{ width: 14, flexShrink: 0, ...mono(12, 700), color: 'var(--color-teal)', paddingTop: 12 }}>
                  {operation === value ? '✓' : ''}
                </span>
              </button>
            ))}
          </div>
        )}
      </span>
      {iconBtn(toggleTheme, isDark ? 'Switch to light' : 'Switch to dark', false, (
        <Icon name={isDark ? 'moon' : 'sun'} size={13} />
      ))}
      {iconBtn(toggleTrace, 'Timeline panel', traceOn, <Icon name="panelRight" size={13} />)}
    </div>
  )
}
