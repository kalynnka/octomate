import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './Icon'

/** Workspace paths are references until the console can serve their contents. */
export function FileReference({ path, label = path, chip = false, target, children }: { path: string; label?: string; chip?: boolean; target?: HTMLElement; children?: ReactNode }) {
  const [unavailable, setUnavailable] = useState(false)
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const tooltip = useRef<HTMLSpanElement>(null)
  const tooltipId = useId()
  useLayoutEffect(() => {
    if (!open) return
    const button = trigger.current!
    const popup = tooltip.current!
    const bounds = button.getBoundingClientRect()
    const size = popup.getBoundingClientRect()
    const left = Math.max(8, Math.min(bounds.left, innerWidth - size.width - 8))
    const top = bounds.top >= size.height + 8 ? bounds.top - size.height - 6 : bounds.bottom + 6
    popup.style.left = `calc(${left}px / var(--trk-zoom, 1))`
    popup.style.top = `calc(${top}px / var(--trk-zoom, 1))`
    const close = () => setOpen(false)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [open, unavailable, target])
  const control = <>
    <button
      ref={trigger}
      type="button"
      className={`trk-file-reference ${chip ? 'trk-reference-chip' : 'trk-reference-link'}`}
      aria-label={`${unavailable ? 'Unavailable file' : 'Open file'}: ${path}`}
      aria-describedby={open ? tooltipId : undefined}
      aria-disabled={unavailable || undefined}
      data-unavailable={unavailable ? '' : undefined}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => { if (document.activeElement !== trigger.current) setOpen(false) }}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onKeyDown={event => { if (event.key === 'Escape') setOpen(false) }}
      onClick={() => { setUnavailable(true); setOpen(true) }}
    >
      {chip && !unavailable && <Icon name="file" size={12} />}
      {unavailable ? <code>{label}</code> : <span className="trk-reference-name">{label}</span>}
    </button>
    {open && createPortal(<span ref={tooltip} id={tooltipId} role="tooltip" className="trk-reference-tooltip">
      <strong>{path}</strong>
      {unavailable && <span>This file can’t be opened here yet. Ask the agent to show its contents or check the path.</span>}
      {children}
    </span>, document.body)}
  </>
  return target ? createPortal(control, target) : control
}
