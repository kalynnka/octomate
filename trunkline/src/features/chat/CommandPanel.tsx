import { Fragment, useId, type CSSProperties } from 'react'
import type { ApiAgentRoute } from '@/lib/api/events'
import { ellipsis, label, mono } from '@/components/text'
import { EffortSlider } from './GatewayRoute'
import { commandArguments, commandGroup, commandHint, fuzzy, highlight, type ArgumentMatch, type CommandLine, type GatewayOp } from './commands'
import type { EffortLevel } from './gateway'

const accent = (share: number) => `color-mix(in srgb, var(--color-accent) ${share}%, transparent)`
const GATEWAY = 'trunkline gateway'
const heading: CSSProperties = { ...label(7.5), color: 'var(--fg-3)' }

/** What `/summon` hands the agent matched by the input: its model and effort. */
export interface CommandRoute {
  /** every model that agent runs */
  routes: ApiAgentRoute[]
  route: ApiAgentRoute
  efforts: EffortLevel[]
  effort: EffortLevel
  onModel: (model: string) => void
  onEffort: (level: EffortLevel) => void
}

/** The scale `/effort` sets this conversation on, and where it sits. */
export interface CommandEffort {
  supported: EffortLevel[]
  preset: EffortLevel | null
  /** the level the line names */
  level: EffortLevel
  /** the level the conversation runs at now */
  current: EffortLevel
  /** the session it is set on, as the composer names it */
  route: string
  onEffort: (level: EffortLevel) => void
}

/** Text with the letters that were typed picked out. */
function Typed({ text, hits, base }: { text: string; hits: number[]; base: string }) {
  return highlight(text, hits).map((stretch, index) => (
    <span key={index} style={stretch.hit ? { color: 'var(--color-accent)', fontWeight: 700 } : { color: base }}>{stretch.text}</span>
  ))
}

/**
 * The command finder over the composer, grouped by source and sorted by
 * availability. Match indices preserve the typed command's selection.
 * The composer keeps the keyboard, so a press here leaves its focus alone; only
 * an effort range has to take it to drag.
 */
export function CommandPanel({ id, line, matches, offered, total, agent, nativeNote, argumentReason, closed, route, effort, onRun, onDismiss, onSettled }: {
  id?: string
  line: CommandLine
  /** the arguments what was typed could still become, out of `offered` */
  matches: ArgumentMatch[]
  offered: number
  /** every command the line could have named */
  total: number
  /** the thread's agent, whose own commands follow the gateway's */
  agent: string
  /** how discovering the agent's commands went, when there is anything to say */
  nativeNote: string | null
  /** The selected destination's refusal, shared with its operation menu. */
  argumentReason?: string
  /** why a gateway op cannot run; absent when it can */
  closed: Partial<Record<GatewayOp, string>>
  route: CommandRoute | null
  effort: CommandEffort | null
  onRun: (index: number) => void
  onDismiss: () => void
  /** Finish a pointer choice after its click has been handled. */
  onSettled: () => void
}) {
  const generatedId = useId()
  const listId = id ?? generatedId
  const naming = line.phase === 'name'
  const command = naming ? line.matches[0].command : line.command
  const where = command.control ? 'Trunkline control' : command.native ? `${agent} · native` : GATEWAY
  const picked = naming ? undefined : matches[0]?.argument
  const input = naming ? null : commandArguments(line)
  const modelQuery = input?.values.model ?? ''
  const models = route ? [route.route, ...route.routes.filter((one) => one.model !== route.route.model)]
    .filter((one) => fuzzy(modelQuery, one.model) !== null) : []
  const hint = commandHint(command)
  const reason = command.unavailable ?? (command.native ? undefined : closed[command.name])
  const typed = naming ? '' : line.typed.trim()
  const offers = input?.active?.name ?? (offered ? 'option' : '')
  const note = naming ? '' : reason ?? argumentReason ?? (matches.length || effort ? ''
    : !command.parameters ? hint
      : input?.missing.length ? `Add ${input.missing.join(', ')} to continue.`
        : offered && input?.typed ? `no ${offers} matches "${input.typed}"` : '')
  const rows = naming ? line.matches.map((match, index) => ({ ...match, index, group: commandGroup(match.command, closed) }))
    .sort((a, b) => Number(Boolean(a.command.native)) - Number(Boolean(b.command.native))
      || a.group.order - b.group.order) : []
  const groupHead = (text: string, first: boolean) => (
    <span style={{ display: 'block', padding: '8px 24px 4px', ...heading, borderTop: first ? 'none' : '1px solid var(--line-divider)' }}>{text}</span>
  )

  return (
    <div
      className="lt-fade-in"
      onMouseDown={(event) => {
        if (!(event.target instanceof HTMLInputElement)) event.preventDefault()
      }}
      style={{ display: 'flex', flexDirection: 'column', fontFamily: 'var(--font-mono)' }}
    >
      <div style={{
        display: 'flex', alignItems: 'center', gap: 9, padding: '7px 18px 7px 24px', color: 'var(--color-teal)',
        borderBottom: '1px solid var(--trk-vline)', background: 'color-mix(in srgb, var(--color-teal) 7%, transparent)',
      }}>
        <span style={{ ...label(9), whiteSpace: 'nowrap' }}>{naming ? 'Commands' : `/${command.name}`}</span>
        <span style={{ flex: 1, minWidth: 0, ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>
          {naming
            ? `${line.matches.length} of ${total} · ${agent}`
            : `${offered ? `${matches.length} of ${offered} ${offers}s` : offers || 'free text'} · ${where}`}
        </span>
        <button
          type="button"
          aria-label="Dismiss commands"
          title="Dismiss commands"
          onClick={onDismiss}
          className="hov-border-line trk-gateway-close"
          style={{
            width: 22, height: 22, flexShrink: 0, boxSizing: 'border-box', padding: 0, cursor: 'pointer',
            border: '1px solid transparent', borderRadius: 0, background: 'transparent', color: 'var(--fg-2)', fontSize: 13,
          }}
        >
          ×
        </button>
      </div>
      <div style={{ maxHeight: 176, overflowY: 'auto', overscrollBehavior: 'contain', borderBottom: '1px solid var(--trk-vline)' }}>
        {naming && (
          <div id={listId} role="listbox" aria-label="Commands">
            {rows.map(({ command: one, hits, index, group }, row) => {
              const shut = one.unavailable ?? (one.native ? undefined : closed[one.name])
              const opens = row === 0 || Boolean(rows[row - 1].command.native) !== Boolean(one.native)
              return (
                <Fragment key={one.native ? `native:${one.native.id}` : `gateway:${one.name}`}>
                  {opens && groupHead(one.native ? `${agent} · native` : 'Gateway', row === 0)}
                  <button
                    type="button"
                    id={`${listId}-${index}`}
                    role="option"
                    aria-selected={index === 0}
                    aria-disabled={Boolean(shut)}
                    title={shut ?? (one.parameters?.some((parameter) => parameter.required) ? `Add ${commandHint(one)}` : `Run /${one.name}`)}
                    onClick={() => { if (!shut) onRun(index) }}
                    style={{
                      width: '100%', border: 0, borderRadius: 0, textAlign: 'left', color: 'inherit',
                      display: 'flex', alignItems: 'center', gap: 10, padding: '5px 24px', opacity: shut ? 0.5 : 1,
                      borderBottom: '1px solid var(--line-color)', cursor: shut ? 'not-allowed' : 'pointer',
                      background: index === 0 ? 'var(--hover)' : 'transparent',
                    }}
                  >
                    <span style={{ ...mono(10.5, 700), whiteSpace: 'nowrap', flexShrink: 0 }}>
                      <Typed text={`/${one.name}`} hits={hits.map((hit) => hit + 1)} base={shut ? 'var(--fg-3)' : 'var(--fg-1)'} />
                    </span>
                    <span style={{ ...mono(9), color: 'var(--fg-3)', whiteSpace: 'nowrap', flexShrink: 0 }}>{commandHint(one)}</span>
                    <span style={{ flex: 1, minWidth: 0, ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>{one.description}</span>
                    <span style={{ ...label(7.5, '.14em'), color: 'var(--fg-3)', whiteSpace: 'nowrap', flexShrink: 0 }}>
                      {one.control ? 'Trunkline · ' : ''}{group.label}
                    </span>
                    <span aria-hidden="true" style={{ width: 12, flexShrink: 0, textAlign: 'right', ...mono(11), color: 'var(--fg-3)' }}>
                      {shut ? '' : '›'}
                    </span>
                  </button>
                </Fragment>
              )
            })}
            {nativeNote && (
              <>
                {!rows.at(-1)?.command.native && groupHead(`${agent} · native`, rows.length === 0)}
                <span role="status" style={{ display: 'block', padding: '5px 24px 7px', ...mono(8.5), color: 'var(--fg-3)', lineHeight: 1.6 }}>{nativeNote}</span>
              </>
            )}
          </div>
        )}
        {effort && (
          <span onClick={onSettled} style={{ display: 'block', padding: '1px 12px' }}>
            <EffortSlider supported={effort.supported} preset={effort.preset} effort={effort.level} onEffort={effort.onEffort} />
          </span>
        )}
        {!effort && !(route && input?.active?.control) && matches.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '9px 24px' }}>
            <span style={{ width: 64, flexShrink: 0, ...heading }}>{offers}s</span>
            <span id={listId} role="listbox" aria-label={`${offers}s`} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, minWidth: 0 }}>
              {matches.map(({ argument, hits }, index) => {
                const on = index === 0
                return (
                  <button
                    type="button"
                    id={`${listId}-${index}`}
                    key={argument.value}
                    role="option"
                    aria-selected={on}
                    title={argument.about}
                    onClick={() => onRun(index)}
                    style={{
                      ...mono(10), lineHeight: '16px', padding: '1px 7px', cursor: 'pointer', whiteSpace: 'nowrap', borderRadius: 0,
                      border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                      background: on ? accent(10) : 'transparent',
                    }}
                  >
                    <Typed text={argument.value} hits={hits} base={on ? 'var(--fg-1)' : 'var(--fg-2)'} />
                  </button>
                )
              })}
            </span>
          </div>
        )}
        {note && (
          <span role="status" style={{ display: 'block', padding: '10px 24px', ...mono(9), color: 'var(--fg-3)', lineHeight: 1.6 }}>{note}</span>
        )}
      </div>
      {route && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: '7px 24px 10px', borderBottom: '1px solid var(--trk-vline)' }}>
          <span style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
            <span style={{ ...heading, whiteSpace: 'nowrap' }}>
              Model <span style={{ color: 'var(--color-teal)' }}>for {route.route.agent_id}</span>
            </span>
            <span style={{ ...mono(8), color: 'var(--fg-3)' }}>
              {Math.min(6, models.length)} of {models.length} candidates · type --model to filter
            </span>
            <span role="group" aria-label={`${route.route.agent_id} models`} style={{ display: 'flex', flexWrap: 'wrap', gap: 6, minWidth: 0 }}>
              {models.slice(0, 6).map((one) => {
                const on = one.model === route.route.model
                return (
                  <button
                    type="button"
                    key={one.model}
                    role="button"
                    aria-pressed={on}
                    onClick={() => route.onModel(one.model)}
                    className="hov-border-accent"
                    style={{
                      ...mono(10, 700), padding: '5px 10px', cursor: 'pointer', borderRadius: 0, minWidth: 0, overflowWrap: 'anywhere',
                      border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                      color: on ? 'var(--color-accent)' : 'var(--fg-2)', background: on ? accent(10) : 'transparent',
                    }}
                  >
                    {one.model}
                  </button>
                )
              })}
              {models.length > 6 && (
                <button
                  type="button"
                  onClick={() => route.onModel(route.route.model)}
                  className="hov-border-accent"
                  style={{ ...mono(9), padding: '5px 10px', cursor: 'pointer', borderRadius: 0, border: '1px solid var(--line-divider)', color: 'var(--fg-2)', background: 'transparent' }}
                >
                  Show all {route.routes.length} models…
                </button>
              )}
            </span>
          </span>
          <span onClick={onSettled} style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
            <span style={{ ...heading, flexShrink: 0 }}>Effort</span>
            <span style={{ flex: 1, minWidth: 0, paddingTop: 2 }}>
              <EffortSlider bare supported={route.efforts} preset={route.route.claim.default_effort} effort={route.effort} onEffort={route.onEffort} />
            </span>
          </span>
        </div>
      )}
      {!naming && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 1, padding: '5px 24px 6px', borderBottom: '1px solid var(--trk-vline)' }}>
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
            <span style={{ ...mono(10, 700), color: 'var(--color-accent)', whiteSpace: 'nowrap' }}>
              /{command.name} {typed || hint}
            </span>
            <span style={{ ...mono(9), color: 'var(--fg-2)', minWidth: 0, ...ellipsis }}>
              {effort
                ? effort.level === effort.current
                  ? `current effort · ${effort.route}`
                  : `${effort.current} → ${effort.level} · ${effort.route}`
                : argumentReason ?? picked?.about ?? command.description}
            </span>
          </span>
          {reason && <span style={{ ...mono(8), color: 'var(--fg-3)' }}>{reason}</span>}
        </div>
      )}
      {naming && reason && <span role="status" style={{ display: 'block', padding: '7px 24px', ...mono(9), color: 'var(--fg-3)' }}>{reason}</span>}
    </div>
  )
}
