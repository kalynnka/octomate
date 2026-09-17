import { useRef, type PointerEvent, type RefObject } from 'react'

export function useDialogDrag(dialog: RefObject<HTMLDialogElement | null>) {
  const drag = useRef<{
    pointerId: number
    x: number
    y: number
    left: number
    top: number
    zoom: number
  } | null>(null)

  return {
    onPointerDown(event: PointerEvent<HTMLElement>) {
      const element = dialog.current
      if (!element || !event.isPrimary || event.button !== 0) return
      if (event.target instanceof Element && event.target.closest('form, button, a, input, select, textarea, label')) return
      const rect = element.getBoundingClientRect()
      drag.current = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: rect.left,
        top: rect.top,
        zoom: Number(getComputedStyle(document.documentElement).zoom),
      }
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
    },
    onPointerMove(event: PointerEvent<HTMLElement>) {
      const element = dialog.current
      const start = drag.current
      if (!element || !start || start.pointerId !== event.pointerId) return
      const rect = element.getBoundingClientRect()
      const left = Math.max(8, Math.min(start.left + event.clientX - start.x, window.innerWidth - rect.width - 8))
      const top = Math.max(8, Math.min(start.top + event.clientY - start.y, window.innerHeight - rect.height - 8))
      // Pointer coordinates include Trunkline's CSS zoom; inset values do not.
      element.style.margin = '0'
      element.style.inset = `${top / start.zoom}px auto auto ${left / start.zoom}px`
    },
    onLostPointerCapture(event: PointerEvent<HTMLElement>) {
      if (drag.current?.pointerId === event.pointerId) drag.current = null
    },
  }
}
