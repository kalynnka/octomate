import type { CSSProperties } from 'react'
import type { ApiAgentRoute } from '@/lib/api/events'
import { ellipsis, label, mono } from '@/components/text'
import { EffortSlider } from './GatewayRoute'
import { COMMANDS, highlight, type ArgumentMatch, type Command, type CommandLine } from './commands'
import type { EffortLevel } from './gateway'

const accent = (share: number) => `color-mix(in srgb, var(--color-accent) ${share}%, transparent)`
const WHERE = 'trunkline gateway'
const heading: CSSProperties = { ...label(7.5), color: 'var(--fg-3)' }

/** What `/summon` hands the agent under the cursor: its model and effort. */
export interface CommandRoute {
  /** every model that agent runs */
  routes: ApiAgentRoute[]
  route: ApiAgentRoute
  efforts: EffortLevel[]
  effort: EffortLevel
  onModel: (model: string) => void
  onEffort: (level: EffortLevel) => void
}

/** Text with the letters that were typed picked out. */
function Typed({ text, hits, base }: { text: string; hits: number[]; base: string }) {
  return highlight(text, hits).map((stretch, index) => (
    <span key={index} style={stretch.hit ? { color: 'var(--color-accent)', fontWeight: 700 } : { color: base }}>{stretch.text}</span>
  ))
}

/**
 * The command finder over the composer: the commands a line could still name,
 * then what its argument can be. The composer keeps the keyboard, so a press
 * here leaves its focus alone; only the effort range has to take it to drag.
 */
export function CommandPanel({ line, matches, offered, cursor, closed, route, onCursor, onRun, onDismiss, onSettled }: {
  line: CommandLine
  /** the arguments what was typed could still become, out of `offered` */
  matches: ArgumentMatch[]
  offered: number
  cursor: number
  /** why a command cannot run; absent when it can */
  closed: Partial<Record<Command['name'], string>>
  route: CommandRoute | null
  onCursor: (index: number) => void
  onRun: (index: number) => void
  onDismiss: () => void
  /** the pointer let go of the effort range, which had taken the focus */
  onSettled: () => void
}) {
  const naming = line.phase === 'name'
  const command = naming ? line.matches[cursor].command : line.command
  const picked = naming ? undefined : matches[cursor]?.argument
  const needed = command.takes.startsWith('<')
  const reason = closed[command.name]
  const typed = naming ? '' : line.typed.trim()
  const note = naming || matches.length ? ''
    : reason ?? (typed ? `no ${command.offers} matches "${typed}"` : `no ${command.offers} to offer`)
  const foot = reason ?? (naming
    ? needed
      ? `needs ${command.takes} · ⇥ to add it · runs in ${WHERE}`
      : `↵ runs /${command.name} in ${WHERE}${command.takes && ` · ⇥ adds ${command.takes}`}`
    : picked ? `↵ runs /${command.name} ${picked.value} · ⇥ fills it in` : note)

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
        <span aria-hidden="true" style={{ ...mono(12, 700), lineHeight: 1 }}>/</span>
        <span style={{ ...label(9), whiteSpace: 'nowrap' }}>{naming ? 'Commands' : `/${command.name}`}</span>
        <span style={{ flex: 1, minWidth: 0, ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>
          {naming
            ? `${line.matches.length} of ${COMMANDS.length} · ${WHERE}`
            : `${matches.length} of ${offered} ${command.offers}s · ${WHERE}`}
        </span>
        <button
          type="button"
          aria-label="Dismiss commands"
          title="Dismiss (Esc)"
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
          <div role="listbox" aria-label="Commands">
            <span style={{ display: 'block', padding: '8px 24px 4px', ...heading }}>Gateway</span>
            {line.matches.map(({ command: one, hits }, index) => {
              const shut = closed[one.name]
              return (
                <div
                  key={one.name}
                  role="option"
                  aria-selected={index === cursor}
                  aria-disabled={Boolean(shut)}
                  title={shut ?? (one.takes.startsWith('<') ? `Add ${one.takes}` : `Run /${one.name}`)}
                  onMouseDown={() => onRun(index)}
                  onMouseEnter={() => onCursor(index)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '5px 24px', opacity: shut ? 0.5 : 1,
                    borderBottom: '1px solid var(--line-color)', cursor: shut ? 'not-allowed' : 'pointer',
                    background: index === cursor ? 'var(--hover)' : 'transparent',
                  }}
                >
                  <span style={{ ...mono(10.5, 700), whiteSpace: 'nowrap', flexShrink: 0 }}>
                    <Typed text={`/${one.name}`} hits={hits.map((hit) => hit + 1)} base={shut ? 'var(--fg-3)' : 'var(--fg-1)'} />
                  </span>
                  <span style={{ ...mono(9), color: 'var(--fg-3)', whiteSpace: 'nowrap', flexShrink: 0 }}>{one.takes}</span>
                  <span style={{ flex: 1, minWidth: 0, ...mono(8.5), color: 'var(--fg-3)', ...ellipsis }}>{one.description}</span>
                  {shut && <span style={{ ...label(7.5, '.14em'), color: 'var(--fg-3)', whiteSpace: 'nowrap', flexShrink: 0 }}>unavailable</span>}
                  <span aria-hidden="true" style={{ width: 12, flexShrink: 0, textAlign: 'right', ...mono(11), color: 'var(--fg-3)' }}>
                    {shut ? '' : one.takes ? '›' : '↵'}
                  </span>
                </div>
              )
            })}
          </div>
        )}
        {matches.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '9px 24px' }}>
            <span style={{ width: 64, flexShrink: 0, ...heading }}>{command.offers}s</span>
            <span role="listbox" aria-label={`${command.offers}s`} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, minWidth: 0 }}>
              {matches.map(({ argument, hits }, index) => {
                const on = index === cursor
                return (
                  <span
                    key={argument.value}
                    role="option"
                    aria-selected={on}
                    title={argument.about}
                    onMouseDown={() => onRun(index)}
                    onMouseEnter={() => onCursor(index)}
                    style={{
                      ...mono(10), lineHeight: '16px', padding: '1px 7px', cursor: 'pointer', whiteSpace: 'nowrap',
                      border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                      background: on ? accent(10) : 'transparent',
                    }}
                  >
                    <Typed text={argument.value} hits={hits} base={on ? 'var(--fg-1)' : 'var(--fg-2)'} />
                  </span>
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '7px 24px 6px', borderBottom: '1px solid var(--trk-vline)' }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <span style={{ ...heading, whiteSpace: 'nowrap' }}>
              Model <span style={{ color: 'var(--color-teal)' }}>for {route.route.agent_id}</span>
            </span>
            <span role="group" aria-label={`${route.route.agent_id} models`} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, minWidth: 0 }}>
              {route.routes.map((one) => {
                const on = one.model === route.route.model
                return (
                  <span
                    key={one.model}
                    role="button"
                    aria-pressed={on}
                    onMouseDown={() => route.onModel(one.model)}
                    className="hov-border-accent"
                    style={{
                      ...mono(8, 700), padding: '2px 6px', cursor: 'pointer', whiteSpace: 'nowrap',
                      border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                      color: on ? 'var(--color-accent)' : 'var(--fg-2)', background: on ? accent(10) : 'transparent',
                    }}
                  >
                    {one.model}
                  </span>
                )
              })}
            </span>
          </span>
          <span onPointerUp={onSettled} style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 180 }}>
            <span style={{ ...heading, flexShrink: 0 }}>Effort</span>
            <span style={{ flex: 1, minWidth: 0, paddingTop: 2 }}>
              <EffortSlider bare supported={route.efforts} preset={route.route.claim.default_effort} effort={route.effort} onEffort={route.onEffort} />
            </span>
          </span>
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, padding: '5px 24px 6px', borderBottom: '1px solid var(--trk-vline)' }}>
        {!naming && (
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
            <span style={{ ...mono(10, 700), color: 'var(--color-accent)', whiteSpace: 'nowrap' }}>
              /{command.name} {picked?.value ?? command.takes}
            </span>
            <span style={{ ...mono(9), color: 'var(--fg-2)', minWidth: 0, ...ellipsis }}>{picked?.about ?? command.description}</span>
          </span>
        )}
        <span style={{ ...mono(8), color: 'var(--fg-3)', letterSpacing: '.03em', ...ellipsis }}>{foot}</span>
      </div>
    </div>
  )
}
