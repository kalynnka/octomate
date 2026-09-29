import { useId, useRef, useState } from 'react'
import { awaitingEndpoint } from '@/lib/api'
import { useMutation } from '@tanstack/react-query'
import { forkThread } from '@/lib/api/client'
import { queryClient } from '@/lib/queryClient'
import type { SurfaceInfo } from '@/lib/api/types'
import { useConsole } from '@/state/console'
import { useThreads } from '@/lib/api/hooks'
import { Icon } from '@/components/Icon'
import { ellipsis, label, mono, statusNote } from '@/components/text'

const operations = {
  fork: { label: 'Fork', description: 'Start an independent thread with this history. The original stays available.' },
  teleport: { label: 'Teleport', description: 'Carry this chat to another destination with the same agent and history.' },
  summon: { label: 'Summon', description: 'Let another agent take over from a prepared brief.' },
}
type Operation = keyof typeof operations

export function ChatHeader() {
  const selThreadId = useConsole((s) => s.selThreadId)
  const detail = useConsole((s) => s.detail)
  const ntOn = useConsole((s) => s.ntOn)
  const ntTitle = useConsole((s) => s.ntTitle)
  const surface = useConsole((s) => s.surface)
  const teleporting = useConsole((s) => s.teleporting)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const theme = useConsole((s) => s.theme)
  const sysDark = useConsole((s) => s.sysDark)
  const { teleport, toggleTheme, toggleTrace } = useConsole((s) => s.actions)
  const surfaces = awaitingEndpoint<SurfaceInfo[]>()
  const { data: threads } = useThreads()
  const [choice, setChoice] = useState<{ threadId: string; operation: Operation } | null>(null)
  const [menu, setMenu] = useState<{ threadId: string; kind: 'operations' | 'destinations' } | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const operation = choice?.threadId === selThreadId ? choice.operation : detail?.canFork ? 'fork' : 'teleport'
  const selected = operations[operation]
  const openMenu = menu?.threadId === selThreadId ? menu.kind : null
  const fork = useMutation({
    mutationFn: ({ sourceThreadId }: { sourceThreadId: string }) => forkThread(sourceThreadId),
    onError(error, { sourceThreadId }) {
      useConsole.getState().actions.reportThreadError(sourceThreadId, `fork failed — ${error.message}`)
    },
    async onSuccess(thread, { sourceThreadId }) {
      await queryClient.invalidateQueries({ queryKey: ['threads'] })
      if (useConsole.getState().selThreadId !== sourceThreadId) return
      await useConsole.getState().actions.selectThread(thread.channel_tentacle_id, thread.id)
    },
  })
  const busyReason = fork.isPending ? 'Wait for the fork to finish.' : teleporting ? 'Wait for the teleport to finish.' : undefined
  const threadReason = ntOn || !detail ? 'Open an existing thread first.' : undefined
  const unavailable: Record<Operation, string | undefined> = {
    fork: threadReason ?? (detail?.canFork ? undefined : 'Fork is available for native Codex threads only.'),
    teleport: threadReason ?? (surfaces?.some((destination) => destination.id !== surface) ? undefined : 'No other destinations are available.'),
    summon: 'Summon is not available in this view yet.',
  }
  const disabledReason = busyReason ?? unavailable[operation]

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
      {cur && (
        <span
          title="Current surface"
          className="trk-head-chip"
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
          title={disabledReason}
          onClick={() => {
            if (disabledReason) return
            if (operation === 'fork') {
              setMenu(null)
              fork.mutate({ sourceThreadId: selThreadId })
            } else if (operation === 'teleport') {
              setMenu(openMenu === 'destinations' ? null : { threadId: selThreadId, kind: 'destinations' })
            }
          }}
          className={disabledReason ? undefined : 'hov-teal-ghost'}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            width: 88, flexShrink: 0, justifyContent: 'center', whiteSpace: 'nowrap',
            ...label(8.5, '.1em'), color: 'var(--trk-on-fill)',
            background: 'var(--color-teal)', border: '1px solid var(--color-teal)',
            padding: '0 8px', height: 'var(--trk-btn, 22px)', boxSizing: 'border-box',
            cursor: disabledReason ? 'not-allowed' : 'pointer',
            position: 'relative', zIndex: 60,
          }}
        >
          <Icon name={operation === 'teleport' ? 'orbit' : operation === 'fork' ? 'gitBranch' : 'wandSparkles'} size={12} style={{ flexShrink: 0 }} />
          {fork.isPending && fork.variables.sourceThreadId === selThreadId ? (
            <span aria-label="Forking" style={{ display: 'inline-flex', alignItems: 'baseline' }}>
              Forking<span className="lt-fork-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
            </span>
          ) : teleporting ? 'Moving…' : selected.label}
        </button>
        <button
          ref={trigger}
          type="button"
          disabled={fork.isPending || teleporting}
          aria-label="Choose thread operation"
          aria-expanded={openMenu === 'operations'}
          aria-controls={openMenu === 'operations' ? menuId : undefined}
          onClick={() => setMenu(openMenu === 'operations' ? null : { threadId: selThreadId, kind: 'operations' })}
          className="hov-teal-ghost"
          style={{
            ...label(8.5, '.1em'), color: 'var(--trk-on-fill)',
            background: 'var(--color-teal)', border: '1px solid var(--color-teal)',
            borderLeft: '1px solid var(--trk-on-fill)', width: 20, flexShrink: 0, padding: 0,
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
              width: 302, maxWidth: 'calc(100vw - 32px)',
              background: 'var(--surface-raised)', border: '1px solid var(--color-ink)',
              boxShadow: 'var(--shadow-card)', zIndex: 60,
            }}
          >
            {(['fork', 'teleport', 'summon'] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={operation === value}
                aria-disabled={Boolean(unavailable[value])}
                title={unavailable[value]}
                onClick={() => {
                  if (unavailable[value]) return
                  setChoice({ threadId: selThreadId, operation: value })
                  setMenu(null)
                  trigger.current?.focus()
                }}
                className={unavailable[value] ? undefined : 'hov-wash'}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                  padding: '10px 12px', textAlign: 'left', cursor: unavailable[value] ? 'not-allowed' : 'pointer',
                  background: 'transparent', color: 'var(--fg-1)', border: 0,
                  borderBottom: '1px solid var(--line-divider)',
                }}
              >
                <span style={{ display: 'flex', flexDirection: 'column', gap: 5, flex: 1 }}>
                  <span style={{ ...label(9, '.1em'), color: unavailable[value] ? 'var(--fg-3)' : operation === value ? 'var(--color-teal)' : 'var(--fg-1)' }}>
                    {operations[value].label}
                  </span>
                  <span style={{ ...mono(9), color: 'var(--fg-3)', lineHeight: 1.6 }}>{operations[value].description}</span>
                  {unavailable[value] && <span style={{ ...mono(8), color: 'var(--fg-3)' }}>{unavailable[value]}</span>}
                </span>
                {operation === value && <Icon name="check" size={13} style={{ color: unavailable[value] ? 'var(--fg-3)' : 'var(--color-teal)', flexShrink: 0 }} />}
              </button>
            ))}
          </div>
        )}
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
          data-open={openMenu === 'destinations' ? '' : undefined}
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
          <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--line-divider)', ...statusNote, color: 'var(--fg-3)' }}>
            Continue this thread in…
          </div>
          {(surfaces ?? []).length === 0 && (
            <div style={{ padding: '10px 12px', ...mono(9), color: 'var(--fg-3)', lineHeight: 1.6 }}>
              no other surface is wired yet
            </div>
          )}
          {(surfaces ?? []).map((sf) => {
            const here = sf.id === surface
            return (
              <div
                key={sf.id}
                onClick={() => {
                  setMenu(null)
                  if (cur) teleport(sf.id, sf.label, cur.label)
                }}
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
