import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './Icon'

/**
 * Workspace paths are references until the console can serve their contents. One with an
 * absolute path is a real link to that file, so the browser's own menu can copy its address.
 */
export function FileReference({ path, label = path, chip = false, target, children }: { path: string; label?: string; chip?: boolean; target?: HTMLElement; children?: ReactNode }) {
  const [unavailable, setUnavailable] = useState(false)
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLAnchorElement>(null)
  const href = path.startsWith('/') ? `file://${encodeURI(path)}` : undefined
  const explain = () => { setUnavailable(true); setOpen(true) }
  const tooltip = useRef<HTMLSpanElement>(null)
  const tooltipId = useId()
  // The card is the fallback for a file that could not be opened; until then the reference is just a link.
  const shown = open && unavailable
  // The card outlives the pointer's trip from the reference into it, so its path can be selected or copied there.
  const closing = useRef<number>(undefined)
  const stay = () => window.clearTimeout(closing.current)
  const leave = () => {
    stay()
    closing.current = window.setTimeout(() => { if (document.activeElement !== trigger.current) setOpen(false) }, 200)
  }
  useEffect(() => stay, [])
  useLayoutEffect(() => {
    if (!shown) return
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
  }, [shown, target])
  const control = <>
    <a
      ref={trigger}
      href={href}
      role={href ? undefined : 'button'}
      tabIndex={href ? undefined : 0}
      className={`trk-file-reference ${chip ? 'trk-reference-chip' : 'trk-reference-link'}`}
      aria-label={`${unavailable ? 'Unavailable file' : 'Open file'}: ${path}`}
      aria-describedby={shown ? tooltipId : undefined}
      aria-disabled={unavailable || undefined}
      data-unavailable={unavailable ? '' : undefined}
      onMouseEnter={() => { stay(); setOpen(true) }}
      onMouseLeave={leave}
      onFocus={() => setOpen(true)}
      onBlur={() => { if (!tooltip.current?.matches(':hover')) setOpen(false) }}
      onKeyDown={event => {
        if (event.key === 'Escape') setOpen(false)
        // Without an address it is a button, which the keyboard presses itself.
        if (!href && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault()
          explain()
        }
      }}
      onClick={event => {
        event.preventDefault()
        explain()
      }}
    >
      {chip && !unavailable && <Icon name="file" size={12} />}
      {unavailable ? <code>{label}</code> : <span className="trk-reference-name">{label}</span>}
    </a>
    {shown && createPortal(<span ref={tooltip} id={tooltipId} role="tooltip" className="trk-reference-tooltip" onMouseEnter={stay} onMouseLeave={leave}>
      <strong>{path}</strong>
      <span>This file can’t be opened here yet. Ask the agent to show its contents or check the path.</span>
      {children}
    </span>, document.body)}
  </>
  return target ? createPortal(control, target) : control
}
