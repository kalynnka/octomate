import { useEffect } from 'react'
import { create } from 'zustand'
import { ApiError, fetchMe } from '@/lib/api/auth'
import type { WireEvent } from '@/lib/api/events'

interface Activity {
  text: string
  attention: boolean
}

export const useActivity = create<Record<string, Activity | undefined>>(() => ({}))
interface ActivityUpdate {
  key: string
  event: Extract<WireEvent, { event_kind: 'run_activity_changed' | 'reflex_phase_changed' | 'run_ended' }>
  attention: boolean
}

const observers = new Map<string, { readers: number; receive: (update: ActivityUpdate) => void; stop: () => void }>()
let socket: WebSocket | undefined
let opened = false
let retry: ReturnType<typeof setTimeout> | undefined
let pending = false

/** Batch effect handoffs and change the watched set without reconnecting. */
function syncActivity() {
  if (pending) return
  pending = true
  queueMicrotask(() => {
    pending = false
    if (!observers.size) {
      clearTimeout(retry)
      retry = undefined
      const closing = socket
      socket = undefined
      opened = false
      closing?.close()
      return
    }
    if (!socket && !retry) {
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      const current = new WebSocket(`${scheme}://${location.host}/api/trunkline/live/activity`)
      socket = current
      let connected = false
      current.onopen = () => {
        if (socket !== current) return
        connected = true
        opened = true
        syncActivity()
      }
      current.onmessage = message => {
        if (socket !== current) return
        const update = JSON.parse(String(message.data)) as ActivityUpdate
        observers.get(update.key)?.receive(update)
      }
      current.onerror = () => current.close()
      current.onclose = async event => {
        if (socket !== current) return
        opened = false
        if (!connected && event.code !== 1008 && observers.size) {
          try {
            await fetchMe()
          } catch (error) {
            if (socket !== current) return
            if (error instanceof ApiError && error.status === 401) {
              socket = undefined
              useActivity.setState({}, true)
              return
            }
          }
        }
        if (socket !== current) return
        socket = undefined
        if (observers.size && event.code !== 1008) retry = setTimeout(() => {
          retry = undefined
          syncActivity()
        }, 2_000)
      }
    }
    if (opened) socket?.send(JSON.stringify({
      thread_ids: [...observers.keys()].filter(key => key.startsWith('id:')).map(key => key.slice(3)),
      thread_keys: [...observers.keys()].filter(key => key.startsWith('key:')).map(key => key.slice(4)),
    }))
  })
}

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

/** All displayed threads share one status-only socket; never changes run ownership. */
export function observeActivity(id: string, byKey = false): () => void {
  const key = `${byKey ? 'key' : 'id'}:${id}`
  let observer = observers.get(key)
  if (!observer) {
    let stopped = false
    let expiry: ReturnType<typeof setTimeout> | undefined
    const clear = () => {
      clearTimeout(expiry)
      expiry = undefined
      useActivity.setState(state => {
        const next = { ...state }
        delete next[key]
        return next
      }, true)
    }
    observer = {
      readers: 0,
      receive: ({ event, attention }) => {
        if (stopped) return
        const text = activityText(event)
        if (text === '') return clear()
        if (text === undefined) return
        const previous = useActivity.getState()[key]
        if (text !== previous?.text || attention !== previous?.attention) useActivity.setState({ [key]: { text, attention } })
        clearTimeout(expiry)
        const timer = setTimeout(() => {
          if (!stopped && expiry === timer) clear()
        }, 120_000)
        expiry = timer
      },
      stop: () => {
        stopped = true
        clear()
      },
    }
    observers.set(key, observer)
    syncActivity()
  }
  observer.readers++
  const active = observer
  let released = false
  return () => {
    if (released) return
    released = true
    if (--active.readers) return
    // React can replay effects when a keyed row moves; keep its live status through that handoff.
    queueMicrotask(() => {
      if (active.readers) return
      active.stop()
      observers.delete(key)
      syncActivity()
    })
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
  syncActivity()
})
