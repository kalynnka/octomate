import { useEffect, useEffectEvent, useId, useMemo, useRef, useState } from 'react'
import type { SyntheticEvent } from 'react'
import { ComposerPrimitive, useAui, useAuiState } from '@assistant-ui/react'
import { useAuth } from '@/state/auth'
import { useConsole } from '@/state/console'
import { useAddresses, useAgents, useChannels, useCommandCatalog, usePermissionModes, useRoutes, useThreadOperations } from '@/lib/api/hooks'
import type { CommandContextBody } from '@/lib/api/events'
import { channelMeta } from '@/lib/api/live'
import { Icon } from '@/components/Icon'
import { ellipsis, fieldLabel, label, microSection, mono } from '@/components/text'
import { CommandPanel } from './CommandPanel'
import { DestinationPicker } from './GatewayDestination'
import { EffortSlider, SummonRoute } from './GatewayRoute'
import { COMMANDS, commandArguments, completeCommand, completion, editArguments, matching, nativeCommand, readCommand, type Argument, type GatewayOp } from './commands'
import { channelRows, destinationReason, gatewayRequest, isGatewayCommand, level as destinationLevel, modelRoute, navigateGatewayMenu, pickRoute, routeEffort, useGatewayForm, type DestinationRow, type GatewayAction, type GatewayForm } from './gateway'

// What the composer becomes for each gateway op; `max` is the relay's own cap.
const GATEWAY_MODES = {
  teleport: {
    title: 'Teleport',
    description: 'carry this chat to another destination with the same agent and history',
    placeholder: 'optional prompt — sent there as your next message…',
    hint: '⌘↵ teleport · esc back to chat',
    field: 'Prompt',
    icon: 'arrowRightLeft',
    rows: 2,
    max: 8000,
  },
  summon: {
    title: 'Summon',
    description: 'let another agent take over from a prepared brief',
    placeholder: 'brief for the next agent — goal, relevant context, decisions, and the next step…',
    hint: '⌘↵ summon · ↵ newline · esc back to chat',
    field: 'Brief',
    icon: 'wandSparkles',
    rows: 5,
    max: 8000,
  },
} as const

// Shared by the input and its caret mirror — the block cursor lands where the
// caret is only if both wrap text with identical metrics.
const composerType = {
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  lineHeight: 1.75,
  padding: 0,
} as const

interface RouteGroup {
  name: string
  code: string
  desc: string
  models: { id: string | null; model: string }[]
}

function RouteSelector() {
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLSpanElement>(null)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const ntRouteId = useConsole((s) => s.ntRouteId)
  const ntEffort = useConsole((s) => s.ntEffort)
  const ntMenu = useConsole((s) => s.ntMenu)
  const { setNtMenu, setNtRoute, closeNtMenu } = useConsole((s) => s.actions)
  const { data: routesData } = useRoutes()
  const { data: agents } = useAgents(true, false)
  const selectedModel = routesData?.routes.find((route) => route.id === ntRouteId)?.model ?? null
  const effortRoute = modelRoute(agents?.find((agent) => agent.id === ntAgent), selectedModel)
  const nativeEffort = routeEffort(effortRoute, ntEffort)
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
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus()
  }, [open])
  return (
    <span onKeyDown={(event) => {
      if (event.key !== 'Escape' || !open) return
      event.stopPropagation()
      closeNtMenu()
      trigger.current?.focus()
    }} style={{ display: 'inline-flex', alignItems: 'center', marginRight: 2, minWidth: 0 }}>
      <span style={{ position: 'relative', display: 'inline-flex', zIndex: 76, minWidth: 0 }}>
        {open && (
          <span onClick={() => setNtMenu('sel')} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />
        )}
        <button
          ref={trigger}
          type="button"
          aria-label="Agent, model and effort"
          aria-expanded={open}
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
          <span style={{ color: 'var(--fg-3)' }}>[{nativeEffort.effort}]</span>
          <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
        </button>
        {open && (
          <span
            ref={menu}
            onKeyDown={navigateGatewayMenu}
            role="group"
            aria-label="Agent and model choices"
            className="lt-menu"
            data-open=""
            style={{
              position: 'absolute',
              bottom: 'calc(100% + 6px)',
              right: 0,
              width: 360,
              maxWidth: 'calc(100vw / var(--trk-zoom) - 48px)',
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
                      <button type="button" data-gateway-option aria-label={`Agent ${ar.name}`} aria-pressed={on}
                        style={{ ...mono(10, 700), color: 'var(--fg-1)', border: 0, padding: 0, background: 'transparent', cursor: 'pointer' }}>{ar.name}</button>
                      <span style={{ ...mono(8), color: 'var(--color-accent)' }}>{on ? ntModel : (ar.models[0]?.model ?? '')}</span>
                    </span>
                    <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.55, letterSpacing: '.02em' }}>{ar.desc}</span>
                    {on && (
                      <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4, paddingTop: 2 }}>
                        {ar.models.map((m) => {
                          const mOn = ntModel === m.model
                          return (
                            <button
                              key={m.id}
                              type="button"
                              data-gateway-option
                              aria-label={`Model ${m.model}`}
                              aria-pressed={mOn}
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
                            </button>
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
            <span style={{ display: 'block', background: 'var(--surface-sunken)' }}>
              <EffortSlider
                supported={nativeEffort.efforts}
                preset={effortRoute?.claim.default_effort ?? null}
                effort={nativeEffort.effort}
                onEffort={(ntEffort) => setNtRoute({ ntEffort })}
              />
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
  const nativeReadOnly = !ntOn && detail?.kind === 'native_thread'
  const switchable = !nativeReadOnly && vocabulary.length > 0
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
        title={nativeReadOnly ? 'Synced native sessions are read-only.' : selected?.description ?? 'Permission mode — ⇧⇥ to switch'}
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
          className="lt-menu"
          data-open=""
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
  const running = useConsole((s) => s.running)
  const gatewayMode = useConsole((s) => s.gatewayMode)
  const ntRouteId = useConsole((s) => s.ntRouteId)
  const ntPermissionMode = useConsole((s) => s.ntPermissionMode)
  const { removeQueued, setGatewayMode, gateway, setEffort, startNewThread, runCommand: runNative } = useConsole((s) => s.actions)
  const username = useAuth((s) => s.user?.username ?? 'operator')
  const userId = useAuth((s) => s.user?.id)

  const isReview = useConsole((s) => s.pvOpen)
  const lastSes = detail?.sessions[detail.sessions.length - 1]
  // The channel list is the connected tentacles only, so a native channel is not
  // in it; its display name is the same table the sidebar reads.
  const channel = channelMeta(selChannel)
  const nativeReadOnly = !ntOn && detail?.kind === 'native_thread'
  const routeChip = `${channel.label.toLowerCase()}/${detail?.key ?? selThreadId}`
  const modelChip = ntOn ? `${ntAgent} · ${ntModel}[${ntEffort}]` : (lastSes?.route ?? '')
  const [sesAgent, sesModel] = (lastSes?.route ?? '').split(' · ')

  useEffect(() => {
    aui.composer.setText('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selThreadId])

  // A gateway op takes the composer over: the draft already in it is Summon's
  // brief or Teleport's prompt, and the op's controls replace the send row.
  const mode = !ntOn && gatewayMode?.threadId === selThreadId ? gatewayMode.action : null
  const copy = mode ? GATEWAY_MODES[mode] : null
  // The header keeps this fresh; here it is read, and asked again by a command.
  const eligibility = useThreadOperations(selThreadId, false)
  const operations = eligibility.data
  const availability = mode ? operations?.[mode] : undefined
  const [form, patchForm, seedForm] = useGatewayForm(selThreadId, mode)
  const commandInput = mode !== null && isGatewayCommand(composerText, mode)
  const draftInput = useRef<HTMLTextAreaElement>(null)
  const [inputRows, setInputRows] = useState<number | null>(null)
  const inputDrag = useRef<{ pointer: number; y: number; rows: number; lineHeight: number; max: number } | null>(null)
  // Summon hands this conversation over where it is, to an agent its channel runs.
  const summonRoutes = Object.values(operations?.summon.routes ?? {}).flat()
  const route = pickRoute(summonRoutes, form.agent, form.model)
  const { efforts, effort } = routeEffort(route, form.effort)
  const request = mode && availability
    ? gatewayRequest(mode, { text: composerText, destination: form.destination, route, effort })
    : null
  const blocked = !mode ? undefined
    : running ? 'Wait for the current run to finish.'
      : !availability ? 'Checking available destinations…'
        : availability.reason
          // Only a thread here can take the message after the move.
          ?? (mode === 'teleport' && !commandInput && composerText.trim() && form.destination && form.destination.address.channel_tentacle_id !== 'trunkline'
            ? 'A prompt can follow a teleport only into a Trunkline thread.' : undefined)
  const ready = Boolean(request) && !blocked
  // What a channel that cannot carry this conversation says in the destination browser.
  const carrierless = `does not run ${sesAgent || 'this agent'}`
  const exposed = mode === 'teleport' && operations && !operations.source?.shared && form.destination?.address.shared
    ? `this chat is private — everyone at ${form.destination.path.at(-1)} can read the thread it continues in`
    : undefined
  const submitGateway = () => {
    if (!mode || blocked) return
    if (!request) {
      if (commandInput || (mode === 'teleport' && !form.destination) || (mode === 'summon' && !route)) {
        patchForm({ menu: mode === 'teleport' ? 'destination' : 'route' })
      } else draftInput.current?.focus()
      return
    }
    aui.composer.setText('')
    void gateway(selThreadId, request)
  }

  const enterGateway = useEffectEvent(() => {
    if (!mode) return
    if (commandInput) patchForm({ menu: mode === 'teleport' ? 'destination' : 'route' })
    else if (form.menu === null) draftInput.current?.focus()
  })
  useEffect(() => { enterGateway() }, [mode])

  // A line that spells a command opens the command finder instead of sending.
  const { data: channels } = useChannels()
  const { data: routesData } = useRoutes()
  const newRoute = routesData?.routes.find((one) => one.id === ntRouteId)
  const [hidden, setHidden] = useState(false)
  const commandsId = useId()
  // The agent's own commands answer on the surface the conversation is on, and
  // are only discovered once a line starts naming one.
  const context: CommandContextBody | null = nativeReadOnly ? null
    : ntOn ? newRoute && userId ? {
      agent_id: newRoute.agent,
      address: { channel_tentacle_id: selChannel, chat_type: 'thread', chat_id: userId, user_id: userId, channel_thread_id: null, shared: false },
      model: newRoute.model,
      permission_mode: ntPermissionMode,
    } : null
      : lastSes && operations?.source
        ? { agent_id: lastSes.agent, address: operations.source, conversation_id: lastSes.conversationId }
        : null
  const catalog = useCommandCatalog(composerText.startsWith('/') ? context : null)
  const natives = context && catalog.data?.status === 'ready'
    ? catalog.data.descriptors.map((one) => nativeCommand(one, Boolean(context.conversation_id)))
      .sort((a, b) => a.name.localeCompare(b.name) || Number(Boolean(a.unavailable)) - Number(Boolean(b.unavailable)))
    : []
  const commands = [...COMMANDS, ...natives]
  const agentName = ntOn ? ntAgent : lastSes?.agent ?? 'agent'
  const waiting = eligibility.isError ? eligibility.error.message : 'Checking available destinations…'
  const nativeNote = !ntOn && !lastSes ? null
    : nativeReadOnly ? 'Synced native sessions are read-only. Only gateway operations are available here.'
      : !context ? ntOn ? 'Choose an agent to discover its commands.' : waiting
        : catalog.isError ? catalog.error.message
          : !catalog.data || catalog.data.status === 'loading' ? `discovering ${agentName} commands…`
            : catalog.data.message ?? (catalog.data.limitations.join(' · ')
              || (catalog.data.status === 'ready' && !catalog.data.descriptors.length ? 'No native commands are available in this context.' : null))
  // `/effort` moves this conversation along the scale its route claims.
  const { data: agents } = useAgents(true, false)
  const owner = agents?.find((one) => one.id === agentName)
  const sessionRoute = modelRoute(owner, ntOn ? newRoute?.model ?? null : lastSes?.model ?? null)
  const scale = routeEffort(sessionRoute, ntOn ? ntEffort : lastSes?.effort ?? 'auto')
  const defaultLevel = routeEffort(sessionRoute, 'auto').effort
  const closed: Partial<Record<GatewayOp, string>> = {
    summon: ntOn ? 'Start a conversation before handing it to another agent.' : operations ? operations.summon.reason ?? undefined : waiting,
    teleport: ntOn ? 'Start a conversation before moving it to another destination.' : operations ? operations.teleport.reason ?? undefined : waiting,
    effort: nativeReadOnly ? 'Synced native sessions are read-only.'
      : !ntOn && !lastSes ? 'This thread has no conversation to set it on.'
      : !agents ? 'Reading the levels this route takes…'
        : !sessionRoute ? 'The runtime model is not available in the model catalog.'
          : sessionRoute.claim.efforts.length ? undefined : `${agentName} takes no effort levels.`,
  }
  const line = !mode && (ntOn || detail) && !running && !hidden ? readCommand(composerText, commands, closed) : null
  const pickedCommand = line?.phase === 'name' ? line.matches[0]?.command : line?.command
  const commandReason = pickedCommand?.unavailable ?? (pickedCommand && !pickedCommand.native ? closed[pickedCommand.name] : undefined)
  const canSubmit = canSend && !commandReason && (!nativeReadOnly || Boolean(pickedCommand && !pickedCommand.native))
  const surfaces = channelRows(
    channels ?? [], operations?.teleport.destinations ?? [], selChannel,
    operations?.teleport.routes ?? {}, carrierless, operations?.barred ?? {},
  ).filter((row) => !row.barred)
  const surfaceValue = (row: DestinationRow) => (row.open ? `${row.key}/` : row.key)
  const agentOptions = summonRoutes
    .filter((one, index) => summonRoutes.findIndex((other) => other.agent_id === one.agent_id) === index)
    .map((one) => ({ value: one.agent_id, about: one.claim.ability }))
  const typedArguments = line?.phase === 'argument' ? commandArguments(line).values : {}
  const agent = 'agent' in typedArguments ? matching(typedArguments.agent, agentOptions)[0]?.argument.value : undefined
  const agentRoutes = summonRoutes.filter((one) => one.agent_id === agent)
  const modelOptions = agentRoutes.map((one) => ({ value: one.model, about: one.claim.ability }))
  const model = typedArguments.model ? matching(typedArguments.model, modelOptions)[0]?.argument.value : undefined
  const agentRoute = pickRoute(agentRoutes, agent ?? null, model ?? (form.agent === agent ? form.model : null))
  const agentScale = routeEffort(agentRoute, 'auto')
  const choices: Record<string, Argument[]> = {
    agent: agentOptions,
    model: modelOptions,
    effort: [agentScale.effort, ...agentScale.efforts.filter((one) => one !== agentScale.effort)]
      .map((value) => ({ value, about: value === agentScale.effort ? 'model default' : `${value} reasoning` })),
    level: [defaultLevel, ...scale.efforts.filter((one) => one !== defaultLevel)]
      .map((value) => ({ value, about: value === defaultLevel ? 'model default' : `${value} reasoning` })),
    destination: surfaces.map((row) => ({ value: surfaceValue(row), about: row.sub })),
  }
  const defaults = { effort: agentScale.effort, level: defaultLevel }
  const args = line?.phase === 'argument' ? commandArguments(line, choices, defaults) : null
  const offered = args?.offered ?? []
  const matches = args?.matches ?? []
  const agentEffort = routeEffort(agentRoute, args?.values.effort ?? 'auto')
  const surface = surfaces.find((one) => surfaceValue(one) === args?.values.destination)
  const destinations = useAddresses(selThreadId, surface?.open?.channel, surface?.open?.inside)
  const argumentReason = args?.invalid.length ? `No matching value for ${args.invalid.join(', ')}.`
    : surface?.open && (destinations.isError || (destinations.data?.length === 0
    && !operations?.teleport.destinations.some((one) => one.channel_tentacle_id === surface.open?.channel)))
    ? destinationReason(destinations.error, destinationLevel(surface.open.channel, 0).many) : undefined
  // An omitted effort uses the model's default, including when resetting a conversation.
  const levelling = line?.phase === 'argument' && !line.command.native && line.command.name === 'effort' && !closed.effort
  const level = args?.values.level || defaultLevel
  const commandChoices = Boolean(line?.phase === 'name' || (matches.length && !levelling && !args?.active?.control))
  const changeDraft = (next: string) => {
    setHidden(false)
    if (commandInput) setGatewayMode(null)
    if (mode) return
    const command = readCommand(next, COMMANDS)
    if (command?.phase !== 'argument') return
    if (!command.command.native && command.command.name === 'summon') {
      const picked = commandArguments(command, { agent: agentOptions }).values.agent
      if (!picked) return
      const models = summonRoutes.filter((one) => one.agent_id === picked).map((one) => ({ value: one.model, about: one.claim.ability }))
      const typedModel = commandArguments(command).values.model
      const model = typedModel ? matching(typedModel, models)[0]?.argument.value : undefined
      const pickedRoute = pickRoute(summonRoutes, picked, model ?? (form.agent === picked ? form.model : null))
      const supported = routeEffort(pickedRoute, 'auto')
      const levels = [supported.effort, ...supported.efforts.filter((one) => one !== supported.effort)]
      const selected = commandArguments(command, { ...choices, effort: levels.map((value) => ({ value, about: '' })) }, { effort: supported.effort }).values.effort
      seedForm('summon', { agent: picked, effort: selected, model: pickedRoute?.model ?? null })
    } else if (command.command.name === 'teleport') {
      const picked = commandArguments(command, choices).values.destination
      const surface = surfaces.find((one) => surfaceValue(one) === picked)
      if (surface) seedForm('teleport', surface.address
        ? { destination: { address: surface.address, path: [surface.label] }, crumbs: [], menu: null }
        : { destination: null, crumbs: surface.open ? [surface.open] : [], menu: 'destination' })
    }
  }
  const write = (next: string) => {
    aui.composer.setText(next)
    changeDraft(next)
    setCaretAt(next.length)
  }
  const openGateway = (action: GatewayAction, change: Partial<GatewayForm> = {}) => {
    const missing = action === 'teleport' ? !change.destination : !change.agent
    seedForm(action, { ...change, menu: missing ? action === 'teleport' ? 'destination' : 'route' : change.menu ?? null })
    if (!missing) write('')
    void eligibility.refetch()
    setGatewayMode({ threadId: selThreadId, action })
    draftInput.current?.focus()
  }
  const fillCommand = (index: number) => {
    if (line) write(completeCommand(line, matches, index))
  }
  const runCommand = (index?: number) => {
    if (!line) return
    const command = line.phase === 'name' ? line.matches[index ?? 0].command : line.command
    if (command.unavailable || (!command.native && closed[command.name])) return
    if (line.phase === 'name') fillCommand(index ?? 0)
    if ((args?.flagName || index !== undefined) && command.parameters?.some((parameter) => parameter.flag && parameter.flag === matches[index ?? 0]?.argument.value)) return fillCommand(index ?? 0)
    const selected = line.phase === 'argument' && index !== undefined
      ? completeCommand(line, matches, index).slice(command.name.length + 2)
      : line.phase === 'argument' ? line.typed : ''
    const invocation = commandArguments({ phase: 'argument', command, typed: selected }, choices, defaults)
    if (invocation.invalid.length) return
    if (command.native) {
      if (invocation.missing.length) return fillCommand(index ?? 0)
      if (!context) return
      write('')
      return void runNative(command.control ? { ...context, model: context.model ?? lastSes?.model } : context, command.native, selected)
    }
    const values = invocation.values
    if (command.name === 'summon') {
      return openGateway('summon', values.agent
        ? { agent: values.agent, effort: values.effort, model: values.model || (form.agent === values.agent ? form.model : null) }
        : { menu: 'route' })
    }
    if (command.name === 'teleport') {
      const surface = surfaces.find((row) => surfaceValue(row) === values.destination)
      if (surface?.address && index === undefined) {
        const request = gatewayRequest('teleport', {
          text: '', destination: { address: surface.address, path: [surface.label] }, route: undefined, effort: 'auto',
        })
        if (request) {
          write('')
          return void gateway(selThreadId, request)
        }
      }
      return openGateway('teleport', surface?.address
        ? { destination: { address: surface.address, path: [surface.label] }, crumbs: [], menu: null }
        : surface?.open ? { destination: null, crumbs: [surface.open], menu: 'destination' } : {})
    }
    if (command.name === 'effort') {
      write('')
      return void setEffort(!selected.trim() || values.level === 'auto' ? null : values.level)
    }
    if (command.name === 'new') {
      write('')
      return startNewThread()
    }
  }

  const placeholder = ntOn
    ? 'first directive — registers the thread on send'
    : nativeReadOnly ? 'synced session · read-only · / for gateway operations'
    : isReview
      ? queue.length
        ? `add a directive — ${queue.length} note${queue.length > 1 ? 's' : ''} ride along`
        : 'directive — sweep lines to quote · + on a line to comment'
      : 'directive… or / for commands'

  const input = {
    onFocus: syncCaret,
    onBlur: () => setCaretAt(null),
    onSelect: syncCaret,
    onScroll: (e: SyntheticEvent<HTMLTextAreaElement>) => setScrollTop(e.currentTarget.scrollTop),
    style: {
      display: 'block',
      width: '100%',
      boxSizing: 'border-box',
      border: 'none',
      outline: 'none',
      resize: 'none',
      background: 'transparent',
      ...composerType,
      color: 'var(--fg-1)',
      caretColor: 'transparent',
    },
  } as const

  return (
    <div className="lt-fade-in" style={{ flexShrink: 0 }}>
      <div className="trk-composer-frame" style={{ position: 'relative', borderTop: `2px solid ${mode || line ? 'var(--color-teal)' : 'var(--trk-bracket)'}` }}>
        <button
          type="button"
          className="trk-composer-resize"
          aria-label="Resize input area"
          title="Drag to resize input · ↑↓ adjust · double-click to reset"
          onPointerDown={(event) => {
            const input = draftInput.current
            if (!input || !event.isPrimary || event.button !== 0) return
            const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1
            const lineHeight = parseFloat(getComputedStyle(input).lineHeight) * zoom
            inputDrag.current = {
              pointer: event.pointerId, y: event.clientY,
              rows: input.getBoundingClientRect().height / lineHeight,
              lineHeight, max: Math.max(2, window.innerHeight * 0.4 / lineHeight),
            }
            event.preventDefault()
            event.currentTarget.setPointerCapture(event.pointerId)
          }}
          onPointerMove={(event) => {
            const drag = inputDrag.current
            if (!drag || drag.pointer !== event.pointerId) return
            setInputRows(Math.max(2, Math.min(drag.max, drag.rows + (drag.y - event.clientY) / drag.lineHeight)))
          }}
          onLostPointerCapture={() => { inputDrag.current = null }}
          onDoubleClick={() => setInputRows(null)}
          onKeyDown={(event) => {
            if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
            const input = draftInput.current
            if (!input) return
            event.preventDefault()
            const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1
            const lineHeight = parseFloat(getComputedStyle(input).lineHeight) * zoom
            const rows = input.getBoundingClientRect().height / lineHeight
            setInputRows(Math.max(2, Math.min(window.innerHeight * 0.4 / lineHeight, rows + (event.key === 'ArrowUp' ? 1 : -1))))
          }}
        />
        {line && (
          <CommandPanel
            id={commandsId}
            line={line}
            matches={matches}
            offered={offered.length}
            total={commands.length}
            agent={agentName}
            nativeNote={nativeNote}
            argumentReason={argumentReason}
            closed={closed}
            route={line.phase === 'argument' && agent && agentRoute ? {
              routes: agentRoutes,
              route: agentRoute,
              ...agentEffort,
              onModel: (model) => openGateway('summon', { agent, model, effort: agentEffort.effort, menu: 'route' }),
              onEffort: (effort) => write(editArguments(line, { agent, effort })),
            } : null}
            effort={line.phase === 'argument' && levelling ? {
              supported: scale.efforts,
              preset: sessionRoute?.claim.default_effort ?? null,
              level,
              current: scale.effort,
              route: ntOn ? modelChip : lastSes?.route ?? agentName,
              onEffort: (one) => write(editArguments(line, { level: one })),
            } : null}
            onRun={runCommand}
            onDismiss={() => setHidden(true)}
            onSettled={() => draftInput.current?.focus()}
          />
        )}
        {mode && copy && (
          <div
            key={mode}
            className="lt-fade-in"
            style={{
              display: 'flex', alignItems: 'center', gap: 9, padding: 'var(--trk-comp-head-pad, 7px 24px 6px)',
              borderBottom: '1px solid var(--trk-vline)', color: 'var(--color-teal)',
              background: 'color-mix(in srgb, var(--color-teal) 7%, transparent)',
            }}
          >
            <Icon name={copy.icon} size={13} style={{ flexShrink: 0 }} />
            <span style={label(9)}>{copy.title}</span>
            <span style={{ flex: 1, minWidth: 0, ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>{copy.description}</span>
            <button
              type="button"
              aria-label="Back to chat"
              title="Back to chat (Esc)"
              onClick={() => setGatewayMode(null)}
              className="hov-border-line trk-gateway-close"
              style={{
                width: 22, height: 22, flexShrink: 0, boxSizing: 'border-box', padding: 0, cursor: 'pointer',
                border: '1px solid transparent', borderRadius: 0, background: 'transparent', color: 'var(--fg-2)', fontSize: 13,
              }}
            >
              ×
            </button>
          </div>
        )}
        {mode === 'summon' && (
          <SummonRoute
            routes={summonRoutes}
            route={route}
            efforts={efforts}
            effort={effort}
            open={form.menu === 'route'}
            reason={availability?.reason}
            onOpen={(open) => patchForm({ menu: open ? 'route' : null })}
            onRoute={(agent, model) => {
              patchForm({ agent, model, ...(commandInput ? { menu: null } : {}) })
              if (commandInput) {
                aui.composer.setText('')
                draftInput.current?.focus()
              }
            }}
            onEffort={(level) => patchForm({ effort: level })}
          />
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: 'var(--trk-comp-head-pad, 7px 24px 6px)', borderBottom: '1px solid var(--trk-vline)' }}>
          <span style={{ ...mono(13, 700), color: 'var(--color-accent)', lineHeight: 1 }}>{mode || line ? '^' : '>_'}</span>
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
                ref={draftInput}
                className="trk-composer-input"
                data-resized={inputRows !== null ? '' : undefined}
                role={commandChoices ? 'combobox' : undefined}
                aria-expanded={commandChoices ? true : undefined}
                aria-autocomplete={commandChoices ? 'list' : undefined}
                aria-controls={commandChoices ? commandsId : undefined}
                aria-activedescendant={commandChoices ? `${commandsId}-0` : undefined}
                aria-label={copy?.field ?? 'Directive'}
                rows={copy?.rows ?? 2}
                minRows={inputRows ?? copy?.rows ?? 2}
                maxRows={inputRows ?? undefined}
                maxLength={copy?.max}
                placeholder={copy?.placeholder ?? placeholder}
                submitMode={mode || line || nativeReadOnly ? 'none' : 'enter'}
                cancelOnEscape={false}
                onChange={(event) => changeDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing) return
                  if (mode) {
                    if (event.key === 'Escape') setGatewayMode(null)
                    else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) submitGateway()
                    else return
                  } else if (line) {
                    if (event.key === 'Escape') setHidden(true)
                    else if (event.key === 'Tab' && !event.shiftKey) fillCommand(0)
                    else if (event.key === 'Enter' && !event.shiftKey) runCommand()
                    else return
                  } else return
                  event.preventDefault()
                }}
                {...input}
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
                  {/* The zero-width space gives an empty line its height; without
                      it the line collapses and the block draws above the input. */}
                  <span style={{ position: 'relative' }}>
                    {'\u200b'}
                    <span
                      style={{
                        position: 'absolute',
                        left: 0,
                        bottom: 0,
                        width: '1ch',
                        height: '1.2em',
                        background: 'var(--color-accent)',
                        animation: 'trkBlink 1.1s step-end infinite',
                      }}
                    />
                  </span>
                  {line && caretAt === composerText.length && <span style={{ color: 'var(--fg-3)' }}>{completion(line, matches, 0)}</span>}
                </span>
              )}
            </span>
          </ComposerPrimitive.Root>
        </div>
        {!line && (
          <div className="trk-comp-hint" style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 12, padding: '0 24px 5px', minWidth: 0 }}>
            <span
              role={copy && !blocked && exposed ? 'alert' : undefined}
              style={{ ...mono(8), color: copy && !blocked && exposed ? 'var(--color-gold)' : 'var(--fg-3)', letterSpacing: '.08em', textTransform: 'uppercase', ...ellipsis, minWidth: 0 }}
            >
              {copy ? (blocked ?? (exposed ? `▲ ${exposed}` : copy.hint))
                : '↵ send · ⇧↵ newline · / commands · ⇧⇥ posture · **b** _i_ `code` ``` fence'}
            </span>
          </div>
        )}
        <div
          // The destination picker rises from this row so it stays inside it at any width.
          style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 4, padding: 'var(--trk-comp-bar-pad, 3px 24px 4px 18px)', borderTop: '1px solid var(--trk-vline)' }}
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
            <span style={{ ...fieldLabel, color: copy ? 'var(--color-teal)' : 'var(--color-accent)' }}>{copy?.field ?? 'Directive'}</span>
          </span>
          {queue.length > 0 && (
            <span style={{ ...mono(8, 700), color: 'var(--color-accent)', letterSpacing: '.08em', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
              · {String(queue.length).padStart(2, '0')} queued — sends as one turn
            </span>
          )}
          <span style={{ flex: 1 }} />
          {mode && copy ? (
            <span className="trk-gateway" style={{ display: 'contents' }}>
              {mode === 'teleport' && (
                <span
                  className="trk-gateway-carry"
                  title="same agent and model travel with the chat"
                  style={{
                    ...label(8, '.06em'), color: 'var(--fg-3)', border: '1px solid var(--line-divider)', borderRadius: 2,
                    height: 20, boxSizing: 'border-box', padding: '0 7px', display: 'inline-flex', alignItems: 'center',
                    gap: 5, whiteSpace: 'nowrap', flexShrink: 0,
                  }}
                >
                  <span>{sesAgent || '—'}</span>
                  {sesModel && <><span>·</span><span>{sesModel}</span></>}
                </span>
              )}
              {mode === 'teleport' && availability && (
                <>
                  <span aria-hidden="true" className="trk-gateway-carry" style={{ fontFamily: 'var(--font-display)', fontSize: 11, color: 'var(--color-teal)', padding: '0 3px' }}>→</span>
                  <DestinationPicker
                    threadId={selThreadId}
                    sourceChannel={selChannel}
                    suggestions={availability.destinations}
                    routes={availability.routes}
                    unrouted={carrierless}
                    barred={operations?.barred ?? {}}
                    selection={form.destination}
                    crumbs={form.crumbs}
                    open={form.menu === 'destination'}
                    onOpen={(open) => patchForm({ menu: open ? 'destination' : null })}
                    onCrumbs={(crumbs) => patchForm({ crumbs })}
                    onSelect={(destination) => {
                      patchForm({ destination, menu: null })
                      if (commandInput) aui.composer.setText('')
                      draftInput.current?.focus()
                    }}
                  />
                </>
              )}
              <button
                type="button"
                aria-disabled={!ready}
                title={blocked}
                onClick={submitGateway}
                className={ready ? 'hov-teal-ghost' : undefined}
                style={{
                  marginLeft: 8, flexShrink: 0, display: 'inline-flex', alignItems: 'center', ...label(8.5, '.16em'),
                  padding: '5px 10px', border: '1px solid var(--color-teal)', borderRadius: 0, background: 'var(--color-teal)',
                  color: 'var(--trk-on-fill)', cursor: ready ? 'pointer' : 'not-allowed', opacity: ready ? 1 : 0.45,
                  whiteSpace: 'nowrap', transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear',
                }}
              >
                {copy.title} ⌘↵
              </button>
            </span>
          ) : (
            <>
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
              {/* A session nothing routed carries its runtime alone, and one with
                  no effort set runs at whatever its runtime defaults to. */}
              {sesModel && (
                <>
                  <span style={{ color: 'var(--fg-3)' }}>·</span>
                  <span style={{ color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{sesModel}</span>
                </>
              )}
              {lastSes?.effort && <span style={{ color: 'var(--fg-3)' }}>[{lastSes.effort}]</span>}
            </span>
          )}
          <span style={{ marginLeft: 8, flexShrink: 0 }}>
            <button
              type="button"
              onClick={() => line ? runCommand() : aui.composer.send()}
              disabled={!canSubmit}
              title={commandReason ?? (nativeReadOnly && !canSubmit ? 'Synced native sessions are read-only. Use gateway operations to continue elsewhere.' : undefined)}
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
                cursor: canSubmit ? 'pointer' : 'default',
                opacity: canSubmit ? 1 : 0.55,
                transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear',
              }}
            >
              Send ↵
            </button>
          </span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
