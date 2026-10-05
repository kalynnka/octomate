import { useId, useRef } from 'react'
import type { CSSProperties } from 'react'
import type { ApiAgentRoute } from '@/lib/api/events'
import { ellipsis, fieldLabel, label, mono } from '@/components/text'
import { EFFORT_SCALE, stepEffort, type EffortLevel } from './gateway'

const EFFORT_LABELS: Record<EffortLevel, string> = {
  auto: 'Auto', minimal: 'Min', low: 'Low', medium: 'Med', high: 'High', xhigh: 'X-High',
}
const accent = (share: number) => `color-mix(in srgb, var(--color-accent) ${share}%, transparent)`
const row: CSSProperties = { position: 'absolute', display: 'block' }

/**
 * The comp's effort scale without its hatching: every level is drawn, the ones
 * this model does not take are dimmed, and the band marks the range it runs. A
 * native range sits over the drawing so the keyboard and assistive tech get a
 * real slider. Auto is on the scale only while the route's default is unknown.
 * `bare` leaves the heading off, for a row that names the scale itself.
 */
export function EffortSlider({ supported, preset, effort, onEffort, bare = false }: {
  supported: EffortLevel[]
  preset: EffortLevel | null
  effort: EffortLevel
  onEffort: (level: EffortLevel) => void
  bare?: boolean
}) {
  const scale = supported.includes('auto') ? EFFORT_SCALE : EFFORT_SCALE.slice(1)
  const step = 100 / (scale.length - 1)
  const at = scale.indexOf(effort)
  const real = supported.filter((one) => one !== 'auto').map((one) => scale.indexOf(one))
  const low = Math.min(...real)
  const high = Math.max(...real)
  const read = effort === 'auto' ? 'Auto · agent default'
    : effort === preset ? `${EFFORT_LABELS[effort]} · agent default` : EFFORT_LABELS[effort]
  const drawn = (
    <span style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <span className="trk-gateway-effort">
        <span style={{ ...row, left: 0, right: 0, top: 10, height: 1, background: 'var(--line-divider)' }} />
        {real.length > 0 && (
          <span style={{ ...row, left: `${low * step}%`, width: `${(high - low) * step}%`, top: 9, height: 3, background: accent(22) }} />
        )}
        <span style={{ ...row, left: 0, width: `${at * step}%`, top: 10, height: 1, background: 'var(--color-accent)' }} />
        {scale.map((level, index) => supported.includes(level) && (
          <span key={level} style={{
            ...row, left: `${index * step}%`, top: 14, width: 1, height: 4,
            background: index <= at ? 'var(--color-accent)' : 'var(--fg-3)',
          }} />
        ))}
        <span style={{
          ...row, left: `${at * step}%`, top: 1, width: 10, height: 7, transform: 'translateX(-5px)',
          background: 'var(--color-accent)', clipPath: 'polygon(0 0,100% 0,50% 100%)',
        }} />
        <input
          type="range"
          aria-label="Thinking effort"
          aria-valuetext={read}
          min={0}
          max={scale.length - 1}
          step={1}
          value={at}
          onChange={(event) => onEffort(stepEffort(effort, scale[Number(event.target.value)], supported))}
        />
      </span>
      <span aria-hidden="true" style={{ position: 'relative', display: 'block', height: 11, margin: '0 7px' }}>
        {scale.map((level, index) => {
          const taken = supported.includes(level)
          return (
            <span
              key={level}
              onClick={() => taken && onEffort(level)}
              style={{
                ...label(7.5, '.1em'), position: 'absolute', whiteSpace: 'nowrap',
                left: `${index * step}%`, transform: `translateX(-${index * step}%)`,
                color: level === effort ? 'var(--color-accent)' : taken ? 'var(--fg-2)' : 'var(--fg-3)',
                opacity: taken ? 1 : 0.4, cursor: taken ? 'pointer' : 'not-allowed',
              }}
            >
              {EFFORT_LABELS[level]}
            </span>
          )
        })}
      </span>
    </span>
  )
  if (bare) return drawn
  return (
    <span style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '10px 12px 8px' }}>
      <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Effort</span>
        <span style={{ ...label(8, '.1em'), color: 'var(--color-accent)' }}>{read}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(7.5), color: 'var(--fg-3)' }}>
          {real.length
            ? `model runs ${EFFORT_LABELS[scale[low]].toLowerCase()}–${EFFORT_LABELS[scale[high]].toLowerCase()}`
            : 'agent default only'}
        </span>
      </span>
      {drawn}
    </span>
  )
}

/** Who takes over on Summon: the routes the destination's channel offers. */
export function SummonRoute({ routes, route, efforts, effort, open, onOpen, onRoute, onEffort }: {
  routes: ApiAgentRoute[]
  route: ApiAgentRoute | undefined
  efforts: EffortLevel[]
  effort: EffortLevel
  open: boolean
  onOpen: (open: boolean) => void
  onRoute: (agent: string, model: string) => void
  onEffort: (level: EffortLevel) => void
}) {
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const agents = [...new Set(routes.map((one) => one.agent_id))]
  const shown = open && Boolean(route)
  return (
    <span
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !shown) return
        event.stopPropagation()
        onOpen(false)
        trigger.current?.focus()
      }}
      onBlur={(event) => {
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) onOpen(false)
      }}
      style={{ display: 'inline-flex', marginRight: 2, minWidth: 0 }}
    >
      {shown && <span onClick={() => onOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />}
      <button
        ref={trigger}
        type="button"
        disabled={!route}
        aria-label="Agent, model and effort for the summoned agent"
        aria-expanded={shown}
        aria-controls={shown ? menuId : undefined}
        title={route ? 'agent · model · effort for the summoned agent' : 'Choose a destination that runs another agent.'}
        onClick={() => onOpen(!open)}
        className={route ? 'hov-border' : undefined}
        style={{
          ...label(8, '.06em'), background: 'var(--surface-raised)', border: '1px solid var(--color-teal)',
          borderRadius: 2, height: 20, boxSizing: 'border-box', padding: '0 7px', display: 'inline-flex',
          alignItems: 'center', gap: 5, cursor: route ? 'pointer' : 'default', opacity: route ? 1 : 0.55,
          whiteSpace: 'nowrap', minWidth: 0, position: 'relative', zIndex: 76,
        }}
      >
        <span style={{ color: 'var(--color-accent)' }}>{route?.agent_id ?? 'agent'}</span>
        <span style={{ color: 'var(--fg-3)' }}>·</span>
        <span style={{ color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{route?.model ?? '—'}</span>
        <span style={{ color: 'var(--fg-3)' }}>[{effort}]</span>
        <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
      </button>
      {shown && route && (
        <span
          id={menuId}
          role="group"
          aria-label="Summoned agent"
          className="lt-menu"
          data-open=""
          style={{
            position: 'absolute', bottom: 'calc(100% + 6px)', right: 12, width: 302, maxWidth: 'calc(100% - 24px)',
            maxHeight: '60vh', overflowY: 'auto', zIndex: 80, background: 'var(--surface-raised)',
            border: '1px solid var(--line-color)', boxShadow: 'var(--shadow-soft)', display: 'flex', flexDirection: 'column',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 12px 7px', borderBottom: '1px solid var(--line-divider)' }}>
            <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Agent</span>
            <span style={{ flex: 1 }} />
            <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em' }}>takes over from the brief</span>
          </span>
          {agents.map((agent) => {
            const models = routes.filter((one) => one.agent_id === agent)
            const on = route.agent_id === agent
            const shownRoute = on ? route : models[0]
            return (
              <span key={agent} style={{
                display: 'flex', flexDirection: 'column', borderBottom: '1px solid var(--line-color)',
                background: on ? accent(7) : 'transparent',
              }}>
                <button
                  type="button"
                  aria-pressed={on}
                  onClick={() => on || onRoute(agent, models[0].model)}
                  className="hov-wash"
                  style={{
                    display: 'flex', alignItems: 'flex-start', gap: 9, padding: '8px 12px', cursor: 'pointer',
                    background: 'transparent', border: 0, borderRadius: 0, textAlign: 'left', color: 'inherit',
                  }}
                >
                  <span style={{
                    width: 20, height: 20, flexShrink: 0, boxSizing: 'border-box', marginTop: 1,
                    border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                    color: on ? 'var(--color-accent)' : 'var(--fg-2)', ...mono(8, 700),
                    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    {agent.slice(0, 2).toUpperCase()}
                  </span>
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <span style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
                      <span style={{ ...mono(10, 700), color: 'var(--fg-1)' }}>{agent}</span>
                      <span style={{ ...mono(8), color: 'var(--color-accent)', minWidth: 0, ...ellipsis }}>{shownRoute.model}</span>
                    </span>
                    <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.55, letterSpacing: '.02em', overflowWrap: 'anywhere' }}>
                      {shownRoute.claim.ability}
                    </span>
                  </span>
                  <span aria-hidden="true" style={{ width: 12, flexShrink: 0, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right', marginTop: 1 }}>
                    {on ? '✓' : ''}
                  </span>
                </button>
                {on && (
                  <span role="group" aria-label={`${agent} models`} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '0 12px 8px 41px' }}>
                    {models.map((one) => {
                      const picked = one.model === route.model
                      return (
                        <button
                          key={one.model}
                          type="button"
                          aria-pressed={picked}
                          onClick={() => onRoute(agent, one.model)}
                          className="hov-border-accent"
                          style={{
                            ...mono(7.5, 700), padding: '2px 6px', cursor: 'pointer', borderRadius: 0,
                            border: `1px solid ${picked ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                            color: picked ? 'var(--color-accent)' : 'var(--fg-2)',
                            background: picked ? accent(10) : 'transparent',
                          }}
                        >
                          {one.model}
                        </button>
                      )
                    })}
                  </span>
                )}
              </span>
            )
          })}
          <EffortSlider supported={efforts} preset={route.claim.default_effort} effort={effort} onEffort={onEffort} />
        </span>
      )}
    </span>
  )
}
