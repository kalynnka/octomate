import { fetchThread } from '@/lib/api/client'
import type { ApiThread, ThreadEvent, WireEvent } from '@/lib/api/events'
import { api } from '@/lib/api'
import { queryClient } from '@/lib/queryClient'
import { useConsole } from '@/state/console'

/** A row already read can include the change that announced it. */
function changedThread(thread: ApiThread, event: ThreadEvent): ApiThread {
  if (event.event_kind === 'thread_handoff_recorded') {
    if (thread.handoffs.some(handoff => handoff.id === event.handoff.id)) return thread
    const handoffs = [...thread.handoffs, event.handoff].sort((a, b) => a.id.localeCompare(b.id))
    return { ...thread, handoffs, active_agent_tentacle_id: handoffs.at(-1)!.to_agent_tentacle_id }
  }
  if (Date.parse(event.updated_at) < Date.parse(thread.updated_at)) return thread
  const updated = { ...thread, updated_at: event.updated_at }
  if (event.event_kind === 'thread_title_changed') updated.title = event.title
  if (event.event_kind === 'thread_project_bound') updated.project_id = event.project_id
  return updated
}

/** Watch the signed-in user's topic before taking a snapshot. Events received
 *  during a read are applied afterwards; an unseen row is read only by its id. */
export function watchThreads(onError: (message: string) => void): () => void {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  const socket = new WebSocket(`${scheme}://${location.host}/api/trunkline/live/user`)
  let stopped = false
  let updates = Promise.resolve()

  const stop = () => {
    stopped = true
    socket.close()
    void queryClient.cancelQueries({ queryKey: ['threads'] })
  }
  const fail = (error: unknown) => {
    if (stopped) return
    stop()
    onError(error instanceof Error ? error.message : String(error))
  }

  socket.onopen = () => {
    if (stopped) return
    // A mounted list may have started reading before the subscription existed.
    updates = queryClient.cancelQueries({ queryKey: ['threads'] }).then(async () => {
      if (stopped) return
      await queryClient.fetchQuery({ queryKey: ['threads'], queryFn: api.listThreads, staleTime: 0, retry: false })
    }).catch(fail)
  }
  socket.onmessage = (message) => {
    const event = JSON.parse(String(message.data)) as WireEvent
    switch (event.event_kind) {
      case 'thread_title_changed':
      case 'thread_project_bound':
      case 'thread_handoff_recorded':
      case 'thread_message_recorded':
      case 'thread_command_outcome_recorded':
        updates = updates.then(async () => {
          if (stopped) return
          let thread = queryClient.getQueryData<ApiThread[]>(['threads'])?.find(row => row.id === event.thread_id)
          if (!thread) thread = await fetchThread(event.thread_id) ?? undefined
          if (stopped || !thread) return
          const updated = changedThread(thread, event)
          queryClient.setQueryData<ApiThread[]>(['threads'], (rows = []) => [
            ...rows.filter(row => row.id !== updated.id), updated,
          ].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || b.id.localeCompare(a.id)))
          if (event.event_kind === 'thread_project_bound') await useConsole.getState().actions.refreshThreadProject(event.thread_id)
        }).catch(fail)
    }
  }
  socket.onerror = () => fail(new Error('Thread-list updates could not connect.'))
  socket.onclose = () => fail(new Error('Thread-list updates disconnected.'))
  return stop
}
