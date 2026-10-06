import { useEffect, useId, useRef } from 'react'
import type { CSSProperties } from 'react'
import type { ApiAgentRoute } from '@/lib/api/events'
import { ellipsis, fieldLabel, label, mono } from '@/components/text'
import { navigateGatewayMenu, type EffortLevel } from './gateway'

const accent = (share: number) => `color-mix(in srgb, var(--color-accent) ${share}%, transparent)`
const row: CSSProperties = { position: 'absolute', display: 'block' }

/**
 * The harness's advertised effort levels, in their original order. A
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
  const scale = supported
  const step = 100 / Math.max(1, scale.length - 1)
  const at = scale.indexOf(effort)
  const real = supported.filter((one) => one !== 'auto').map((one) => scale.indexOf(one))
  const low = Math.min(...real)
  const high = Math.max(...real)
  const read = effort === 'auto' ? 'Auto · agent default'
    : effort === preset ? `${effort} · agent default` : effort
  const labelWidth = Math.ceil(Math.max(...scale.map((level) => level.length), 4) * 1.2) + 3
  const drawn = (
    <span style={{ display: 'block', ...mono(7.5), width: `min(100%, ${Math.max(42, scale.length * labelWidth)}ch)`, overflowX: 'auto', paddingBottom: 2 }}>
    <span style={{ display: 'flex', flexDirection: 'column', gap: 3, ...mono(7.5), minWidth: `${scale.length * labelWidth}ch` }}>
      <span className="trk-gateway-effort">
        <span style={{ ...row, left: 0, right: 0, top: 10, height: 1, background: 'var(--line-divider)' }} />
        {real.length > 0 && (
          <span style={{ ...row, left: `${low * step}%`, width: `${(high - low) * step}%`, top: 9, height: 3, background: accent(22) }} />
        )}
        <span style={{ ...row, left: 0, width: `${at * step}%`, top: 10, height: 1, background: 'var(--color-accent)' }} />
        {scale.map((level, index) => (
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
          onChange={(event) => onEffort(scale[Number(event.target.value)])}
        />
      </span>
      <span aria-hidden="true" style={{ position: 'relative', display: 'block', height: 11, margin: '0 7px' }}>
        {scale.map((level, index) => {
          return (
            <span
              key={level}
              onClick={() => onEffort(level)}
              style={{
                ...label(7.5, '.1em'), position: 'absolute', whiteSpace: 'nowrap',
                left: `${index * step}%`, transform: `translateX(-${index * step}%)`,
                color: level === effort ? 'var(--color-accent)' : 'var(--fg-2)',
                cursor: 'pointer',
              }}
            >
              {level === 'auto' ? 'Auto' : level}
            </span>
          )
        })}
      </span>
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
            ? `model runs ${scale[low]}–${scale[high]}`
            : 'agent default only'}
        </span>
      </span>
      {drawn}
    </span>
  )
}

/** Who takes over on Summon: the routes the destination's channel offers. */
export function SummonRoute({ routes, route, efforts, effort, open, reason, onOpen, onRoute, onEffort }: {
  routes: ApiAgentRoute[]
  route: ApiAgentRoute | undefined
  efforts: EffortLevel[]
  effort: EffortLevel
  open: boolean
  reason?: string | null
  onOpen: (open: boolean) => void
  onRoute: (agent: string, model: string) => void
  onEffort: (level: EffortLevel) => void
}) {
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLSpanElement>(null)
  const menuId = useId()
  const agents = [...new Set(routes.map((one) => one.agent_id))]
  const shown = open
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLButtonElement>('[data-gateway-option][aria-pressed="true"]')?.focus()
  }, [open])
  return (
    <span
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !shown) return
        event.stopPropagation()
        onOpen(false)
        trigger.current?.focus()
      }}
      className="trk-gateway"
      style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0, padding: '8px 24px', borderBottom: '1px solid var(--trk-vline)' }}
    >
      <button
        ref={trigger}
        type="button"
        disabled={!route && !reason}
        aria-label="Agent, model and effort for the summoned agent"
        aria-expanded={shown}
        aria-controls={shown ? menuId : undefined}
        title={reason ?? (route ? 'agent · model · effort for the summoned agent' : 'Choose a destination that runs another agent.')}
        onClick={() => onOpen(!open)}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
          event.preventDefault()
          onOpen(true)
          menu.current?.querySelector<HTMLButtonElement>('[data-gateway-option][aria-pressed="true"]')?.focus()
        }}
        className={route ? 'hov-border' : undefined}
        style={{
          ...label(8, '.06em'), background: 'var(--surface-raised)', border: '1px solid var(--color-teal)',
          borderRadius: 2, height: 20, boxSizing: 'border-box', padding: '0 7px', display: 'inline-flex',
          alignItems: 'center', gap: 5, cursor: route ? 'pointer' : 'default', opacity: route ? 1 : 0.55,
          whiteSpace: 'nowrap', minWidth: 0, maxWidth: '100%', alignSelf: 'flex-start',
        }}
      >
        <span style={{ color: 'var(--color-accent)' }}>{route?.agent_id ?? 'agent'}</span>
        <span style={{ color: 'var(--fg-3)' }}>·</span>
        <span style={{ color: 'var(--fg-1)', minWidth: 0, ...ellipsis }}>{route?.model ?? '—'}</span>
        <span style={{ color: 'var(--fg-3)' }}>[{effort}]</span>
        <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
      </button>
      {shown && (
        <span
          ref={menu}
          id={menuId}
          role="group"
          aria-label="Summoned agent"
          className="lt-menu trk-gateway-menu"
          data-open=""
          onKeyDown={navigateGatewayMenu}
          style={{
            minWidth: 0, background: 'var(--surface-raised)', border: '1px solid var(--line-color)',
            display: 'flex', flexDirection: 'column',
          }}
        >
          {reason && <span role="status" style={{ display: 'block', padding: '10px 12px', ...mono(9), color: 'var(--fg-3)' }}>{reason}</span>}
          {route && <>
          <span style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, padding: '8px 12px', borderBottom: '1px solid var(--line-divider)' }}>
            <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Agent</span>
            {agents.map((agent) => {
              const on = route.agent_id === agent
              const offered = on ? route : routes.find((one) => one.agent_id === agent)!
              return (
                <button
                  key={agent}
                  type="button"
                  data-gateway-option=""
                  aria-pressed={on}
                  title={offered.claim.ability}
                  onClick={() => onRoute(agent, offered.model)}
                  className="hov-border-accent"
                  style={{
                    ...mono(10, 700), padding: '4px 10px', cursor: 'pointer', borderRadius: 0,
                    border: `1px solid ${on ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                    color: on ? 'var(--color-accent)' : 'var(--fg-2)', background: on ? accent(10) : 'transparent',
                  }}
                >
                  {agent}
                </button>
              )
            })}
          </span>
          <span style={{ ...fieldLabel, color: 'var(--fg-2)', padding: '8px 12px 6px' }}>Model</span>
          <span role="group" aria-label={`${route.agent_id} models`} style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 220px), 1fr))', gap: 6,
            padding: '0 12px 8px', maxHeight: 'min(132px, calc(22dvh / var(--trk-zoom)))', overflowY: 'auto', overscrollBehavior: 'contain',
          }}>
            {routes.filter((one) => one.agent_id === route.agent_id).map((one) => {
              const picked = one.model === route.model
              return (
                <button
                  key={one.model}
                  type="button"
                  data-gateway-option=""
                  aria-pressed={picked}
                  title={one.claim.ability}
                  onClick={() => onRoute(route.agent_id, one.model)}
                  className="hov-border-accent"
                  style={{
                    ...mono(10, 700), padding: '5px 10px', cursor: 'pointer', borderRadius: 0, minWidth: 0, overflowWrap: 'anywhere', textAlign: 'left',
                    border: `1px solid ${picked ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                    color: picked ? 'var(--color-accent)' : 'var(--fg-2)', background: picked ? accent(10) : 'transparent',
                  }}
                >
                  {one.model}
                </button>
              )
            })}
          </span>
          <span style={{ ...mono(8), color: 'var(--fg-3)', padding: '0 12px 8px', ...ellipsis }}>{route.claim.ability}</span>
          <EffortSlider supported={efforts} preset={route.claim.default_effort} effort={effort} onEffort={onEffort} />
          </>}
        </span>
      )}
    </span>
  )
}
