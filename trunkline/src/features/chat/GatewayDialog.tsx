import { useEffect, useId, useRef, useState } from 'react'
import { Icon } from '@/components/Icon'
import { fieldLabel, mono } from '@/components/text'
import { Button } from '@/components/Button'
import { Field } from '@/features/auth/parts'
import type { GatewayRequest, OperationAvailability } from '@/lib/api/events'
import { closeDialog } from '@/lib/dialog'

export function GatewayDialog({ action, availability, disabledReason, onSubmit, onClose }: {
  action: GatewayRequest['action']
  availability?: OperationAvailability
  disabledReason?: string
  onSubmit: (request: GatewayRequest) => void
  onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [targetKey, setTargetKey] = useState('')
  const [routeKey, setRouteKey] = useState('')
  const [effort, setEffort] = useState('')
  const [brief, setBrief] = useState('')
  const hint = action === 'teleport' ? 'Continuing this conversation here.' : 'Continuing with another agent.'
  const destinations = availability?.destinations ?? []
  const destination = targetKey
    ? destinations.find((one) => JSON.stringify(one.target) === targetKey)
    : destinations[0]
  const routes = destination?.routes ?? []
  const route = routeKey ? routes.find((one) => JSON.stringify([one.agent_id, one.model]) === routeKey) : routes[0]
  const agents = [...new Set(routes.map((one) => one.agent_id))]
  const selectedEffort = route?.claim.efforts.find((one) => one === effort)
  const effortIndex = selectedEffort && route ? route.claim.efforts.indexOf(selectedEffort) + 1 : 0
  const title = action === 'teleport' ? 'Teleport' : 'Summon'
  const reason = disabledReason ?? (!destination ? availability?.reason ?? 'No eligible destinations.' : undefined)
  const ready = !reason && (action === 'teleport' || Boolean(route && brief.trim()))
  const close = () => void closeDialog(dialog.current, onClose)

  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])

  return (
    <dialog ref={dialog} className="trk-dialog" aria-labelledby={titleId}
      style={{ width: 'min(440px, calc(100vw / var(--trk-zoom, 1) - 32px))', overflowY: 'auto' }}
      onCancel={(event) => { event.preventDefault(); close() }}>
      <form className="trk-dialog-main" onSubmit={(event) => {
        event.preventDefault()
        if (!ready || !destination) return
        if (action === 'teleport') onSubmit({ action, body: { destination: destination.target, hint: hint.trim() } })
        else if (route) onSubmit({ action, body: {
          destination: destination.target, hint: hint.trim(), brief: brief.trim(),
          agent_id: route.agent_id, model: route.model,
          ...(selectedEffort ? { effort: selectedEffort } : {}),
        } })
      }}>
        <header className="trk-dialog-header">
          <span id={titleId} className="trk-gateway-title"><Icon name={action === 'teleport' ? 'orbit' : 'wandSparkles'} size={18} />{title}</span>
          <button type="button" className="trk-dialog-close hov-wash" aria-label={`Close ${title}`} onClick={close}>×</button>
        </header>
        <p className="trk-dialog-hint">{action === 'teleport'
          ? 'Carry this chat to another destination with the same agent and history.'
          : 'Let another agent take over from a prepared brief.'}</p>
        <Field name="Destination">
          <select className="trk-input" autoFocus required value={destination ? JSON.stringify(destination.target) : ''}
            onChange={(event) => { setTargetKey(event.target.value); setRouteKey(''); setEffort('') }}>
            {!destination && <option value="">Choose a destination</option>}
            {destinations.map((one) => <option key={JSON.stringify(one.target)} value={JSON.stringify(one.target)}>{one.label}</option>)}
          </select>
        </Field>
        {action === 'summon' && <>
          <fieldset className="trk-gateway-routes">
            <legend style={fieldLabel}>Agent / model</legend>
            {agents.map((agent) => {
              const models = routes.filter((one) => one.agent_id === agent)
              const active = route?.agent_id === agent
              return <div key={agent} className="trk-gateway-agent" data-selected={active || undefined}>
                <button type="button" className="trk-gateway-agent-heading hov-wash" aria-pressed={active}
                  onClick={() => { setRouteKey(JSON.stringify([agent, models[0].model])); setEffort('') }}>
                  <span className="trk-gateway-badge">{agent.slice(0, 2).toUpperCase()}</span>
                  <span style={{ ...mono(10, 700), flex: 1, textAlign: 'left', overflowWrap: 'anywhere' }}>{agent}</span>
                  {active && <Icon name="check" size={12} />}
                </button>
                {active && <div className="trk-gateway-models">
                  <div role="group" aria-label={`${agent} models`} className="trk-gateway-chips">
                    {models.map((one) => <button type="button" key={one.model} aria-pressed={route.model === one.model}
                      className="trk-gateway-model hov-border-accent"
                      onClick={() => { setRouteKey(JSON.stringify([agent, one.model])); setEffort('') }}>
                      {one.model}
                    </button>)}
                  </div>
                  <p className="trk-gateway-ability">{route.claim.ability}</p>
                </div>}
              </div>
            })}
            {!routes.length && <p className="trk-dialog-hint">No eligible agents at this destination.</p>}
          </fieldset>
          {route?.claim.efforts.length ? <Field name="Thinking effort" hint={selectedEffort ?? 'Agent default'}>
            <span className="trk-gateway-effort-control">
              <input className="trk-gateway-effort" type="range" min={0} max={route.claim.efforts.length} step={1}
                value={effortIndex} aria-valuetext={selectedEffort ?? 'Agent default'}
                style={{ backgroundImage: `linear-gradient(to right, var(--color-accent) ${effortIndex / route.claim.efforts.length * 100}%, var(--line-divider) 0)` }}
                onChange={(event) => setEffort(route.claim.efforts[Number(event.target.value) - 1] ?? '')} />
              <span className="trk-gateway-effort-stops" aria-hidden="true">
                {['auto', ...route.claim.efforts].map((step, index) => (
                  <span key={step} className="trk-gateway-effort-stop" data-selected={index === effortIndex || undefined}
                    style={{ left: `${index / route.claim.efforts.length * 100}%` }}>{step}</span>
                ))}
              </span>
            </span>
          </Field> : null}
          <Field name="Brief" hint="What the next agent needs to know">
            <textarea className="trk-input" rows={4} required maxLength={8000} value={brief}
              placeholder="Goal, relevant context, decisions, and the next step…"
              style={{ resize: 'vertical' }} onChange={(event) => setBrief(event.target.value)} />
          </Field>
        </>}
        {reason && <p role="status" className="trk-dialog-hint">{reason}</p>}
        <footer className="trk-dialog-actions">
          <Button onClick={close}>Cancel</Button>
          <Button type="submit" disabled={!ready} style={{ background: 'var(--color-teal)', color: 'var(--trk-on-fill)', borderColor: 'var(--color-teal)' }}>{title}</Button>
        </footer>
      </form>
    </dialog>
  )
}
