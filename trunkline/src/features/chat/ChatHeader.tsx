import { useId, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchThreadOperations, forkThread } from '@/lib/api/client'
import { queryClient } from '@/lib/queryClient'
import { GatewayDialog } from './GatewayDialog'
import { useConsole } from '@/state/console'
import { useThreads } from '@/lib/api/hooks'
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
  const detail = useConsole((s) => s.detail)
  const ntOn = useConsole((s) => s.ntOn)
  const ntTitle = useConsole((s) => s.ntTitle)
  const pending = useConsole((s) => s.gatewayPending)
  const running = useConsole((s) => s.running)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const theme = useConsole((s) => s.theme)
  const sysDark = useConsole((s) => s.sysDark)
  const { toggleTheme, toggleTrace } = useConsole((s) => s.actions)
  const eligibility = useQuery({
    queryKey: ['thread-operations', selThreadId],
    queryFn: () => fetchThreadOperations(selThreadId),
    enabled: Boolean(selThreadId && detail && !ntOn && !running && !pending),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const [dialog, setDialog] = useState<{ threadId: string; action: 'teleport' | 'summon' } | null>(null)
  const { data: threads } = useThreads()
  const [choice, setChoice] = useState<Operation | null>(null)
  const [menu, setMenu] = useState<{ threadId: string; kind: 'operations' } | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
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
  const busyReason = fork.isPending ? 'Wait for the fork to finish.'
    : pending ? `Wait for ${pending.action} to finish.` : running ? 'Wait for the current run to finish.' : undefined
  const threadReason = ntOn || !detail ? 'Open an existing thread first.' : undefined
  const loadingReason = eligibility.isError ? eligibility.error.message : !eligibility.data ? 'Checking available destinations…' : undefined
  const unavailable: Record<Operation, string | undefined> = {
    fork: threadReason ?? (detail?.canFork ? undefined : 'Fork is available for native Codex threads only.'),
    teleport: threadReason ?? loadingReason ?? (eligibility.data?.teleport.destinations.length ? undefined : eligibility.data?.teleport.reason ?? 'No eligible destinations.'),
    summon: threadReason ?? loadingReason ?? (eligibility.data?.summon.destinations.length ? undefined : eligibility.data?.summon.reason ?? 'No eligible agents or destinations.'),
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
            } else {
              setMenu(null)
              void eligibility.refetch()
              setDialog({ threadId: selThreadId, action: operation })
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
          <span key={operation} className="trk-operation-label">
            <Icon name={operation === 'teleport' ? 'orbit' : operation === 'fork' ? 'gitBranch' : 'wandSparkles'} size={12} style={{ flexShrink: 0 }} />
            {fork.isPending && fork.variables.sourceThreadId === selThreadId ? (
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
          disabled={fork.isPending || Boolean(pending)}
          aria-label="Choose thread operation"
          aria-expanded={openMenu === 'operations'}
          aria-controls={openMenu === 'operations' ? menuId : undefined}
          onClick={() => {
            if (!running && !ntOn && detail) void eligibility.refetch()
            setMenu(openMenu === 'operations' ? null : { threadId: selThreadId, kind: 'operations' })
          }}
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
      </span>
      {dialog?.threadId === selThreadId && !ntOn && (
        <GatewayDialog
          key={`${dialog.threadId}:${dialog.action}`}
          action={dialog.action}
          availability={eligibility.data?.[dialog.action]}
          disabledReason={busyReason ?? (eligibility.isFetching ? 'Checking available destinations…' : loadingReason)}
          onClose={() => setDialog(null)}
          onSubmit={(request) => {
            setDialog(null)
            void useConsole.getState().actions.gateway(selThreadId, request)
          }}
        />
      )}
      {iconBtn(toggleTheme, isDark ? 'Switch to light' : 'Switch to dark', false, (
        <Icon name={isDark ? 'moon' : 'sun'} size={13} />
      ))}
      {iconBtn(toggleTrace, 'Timeline panel', traceOn, <Icon name="panelRight" size={13} />)}
    </div>
  )
}
