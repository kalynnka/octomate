import { useEffect } from 'react'
import { create } from 'zustand'
import { ApiError } from '@/lib/api/auth'
import { watchThread } from '@/lib/api/client'
import type { WireEvent } from '@/lib/api/events'

interface Activity {
  text: string
  attention: boolean
}

export const useActivity = create<Record<string, Activity | undefined>>(() => ({}))
const observers = new Map<string, { readers: number; stop: () => void }>()

/** Use the channel status wording without trailing ellipses, tool arguments, or thoughts. */
export function activityText(event: WireEvent): string | undefined {
  switch (event.event_kind) {
    case 'reflex_awake': return 'Preparing'
    case 'reflex_phase_changed':
      return { preparing: 'Preparing', routing: 'Routing', moving: 'Moving', finalizing: 'Finalizing' }[event.phase]
    case 'run_started': return 'Working'
    case 'run_activity_changed': {
      const text = (event.display_text ?? '').replace(/\p{C}/gu, char => /\s/u.test(char) ? ' ' : '').replace(/\s+/gu, ' ').trim()
      return [...text].slice(0, 120).join('').replace(/(?:…+|\.{3,})(?=\s*(?:·|$))/gu, '').trimEnd() || {
        working: 'Working', thinking: 'Thinking', using_tools: 'Using tools',
        writing: 'Writing the response', compacting: 'Compacting context',
      }[event.activity]
    }
    case 'part_start':
      if (event.part.part_kind === 'thinking') return 'Thinking'
      if (event.part.part_kind === 'text') return 'Writing the response'
      if (event.part.part_kind === 'tool-call') return 'Working'
      return
    case 'part_delta':
      if (event.delta.part_delta_kind === 'thinking') return 'Thinking'
      if (event.delta.part_delta_kind === 'text') return 'Writing the response'
      return
    case 'function_tool_call':
    case 'output_tool_call': return 'Using tools'
    case 'action_batch': return event.blocks_run === true ? 'Waiting for input' : 'Working · Input requested'
    case 'run_ended':
    case 'reflex_asleep':
    case 'run_error': return ''
  }
}

/** One shared presentation subscription per displayed thread; never changes run ownership. */
export function observeActivity(id: string, byKey = false): () => void {
  const key = `${byKey ? 'key' : 'id'}:${id}`
  let observer = observers.get(key)
  if (!observer) {
    let stopped = false
    let generation = 0
    let controller: AbortController
    let expiry: ReturnType<typeof setTimeout> | undefined
    let retry: ReturnType<typeof setTimeout> | undefined
    const clear = () => {
      clearTimeout(expiry)
      expiry = undefined
      useActivity.setState(state => {
        const next = { ...state }
        delete next[key]
        return next
      }, true)
    }
    const connect = async () => {
      const current = ++generation
      controller = new AbortController()
      try {
        const socket = await watchThread(byKey ? { key: id } : { id }, event => {
          if (stopped || current !== generation) return
          const text = activityText(event)
          if (text === '') return clear()
          const heartbeat = event.event_kind === 'part_end' || event.event_kind === 'function_tool_result' || event.event_kind === 'output_tool_result'
          if (text === undefined && (!heartbeat || !useActivity.getState()[key])) return
          if (text !== undefined) {
            const attention = event.event_kind === 'action_batch'
            const previous = useActivity.getState()[key]
            if (text !== previous?.text || attention !== previous?.attention) useActivity.setState({ [key]: { text, attention } })
          }
          clearTimeout(expiry)
          const timer = setTimeout(() => {
            if (!stopped && expiry === timer) clear()
          }, 120_000)
          expiry = timer
        }, controller.signal)
        await socket.closed
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          clear()
          return
        }
        // A failed handshake reconnects like a closed socket; labels still expire.
      }
      if (!stopped && current === generation) {
        generation++
        retry = setTimeout(() => void connect(), 2_000)
      }
    }
    observer = {
      readers: 0,
      stop: () => {
        stopped = true
        clearTimeout(retry)
        controller.abort()
        clear()
      },
    }
    observers.set(key, observer)
    void connect()
  }
  observer.readers++
  const active = observer
  let released = false
  return () => {
    if (released) return
    released = true
    if (--active.readers) return
    active.stop()
    observers.delete(key)
  }
}

export function useThreadActivity(id: string, threadKey?: string, enabled = true): Activity | undefined {
  const byKey = threadKey !== undefined
  const target = threadKey ?? id
  useEffect(() => enabled ? observeActivity(target, byKey) : undefined, [target, byKey, enabled])
  return useActivity(state => enabled ? state[`${byKey ? 'key' : 'id'}:${target}`] : undefined)
}

import.meta.hot?.dispose(() => {
  for (const observer of observers.values()) observer.stop()
  observers.clear()
})
