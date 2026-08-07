import { useCallback } from 'react'
import { useConsole } from '@/state/console'
import type { RailKey } from '@/state/console'

/**
 * Drag-to-resize for the console rails. `fromRight` measures the width from
 * the element's right edge (the timeline panel drags on its left edge).
 */
export function useRailDrag(
  key: RailKey,
  elementId: string,
  min: number,
  max: number,
  fromRight = false,
) {
  const { setRailWidth, setRailDrag } = useConsole((s) => s.actions)
  return useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      const el = document.getElementById(elementId)
      if (!el) return
      const r = el.getBoundingClientRect()
      setRailDrag(key)
      const onMove = (ev: MouseEvent) => {
        const raw = fromRight ? r.right - ev.clientX : ev.clientX - r.left
        setRailWidth(
          key,
          Math.round(Math.min(Math.max(raw, min), Math.min(max, window.innerWidth - 380))),
        )
      }
      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        setRailDrag(null)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [key, elementId, min, max, fromRight, setRailWidth, setRailDrag],
  )
}
