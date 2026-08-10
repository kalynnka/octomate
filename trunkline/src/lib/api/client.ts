/**
 * Real octomate endpoints under /api/trunkline. The dev server proxies /api
 * and /oauth to the FastAPI instance at 127.0.0.1:8000 (see vite.config.ts) so
 * requests stay same-origin — the backend ships no CORS middleware. In
 * production the console is mounted on that same app, so the paths hold as-is.
 */
import type {
  ApiChannelInfo,
  ApiConversation,
  ApiDeferredBatch,
  ApiProject,
  ApiRoute,
  ApiThread,
  ApiThreadMessage,
  BatchResponseBody,
  DirectiveBody,
  WireEvent,
} from './events'

export interface HealthState {
  ok: boolean
  /** false when the gateway could not be reached at all */
  reachable: boolean
}

export async function fetchHealth(): Promise<HealthState> {
  try {
    const res = await fetch('/api/trunkline/health')
    // The dev proxy answers 5xx itself when the gateway is down.
    if (res.status >= 500) return { ok: false, reachable: false }
    if (!res.ok) return { ok: false, reachable: true }
    const body = (await res.json()) as { ok?: boolean }
    return { ok: body.ok === true, reachable: true }
  } catch {
    return { ok: false, reachable: false }
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`)
  return (await res.json()) as T
}

export function fetchChannels(): Promise<ApiChannelInfo[]> {
  return getJson<ApiChannelInfo[]>('/api/trunkline/channels')
}

export function fetchRoutes(): Promise<ApiRoute[]> {
  return getJson<ApiRoute[]>('/api/trunkline/routes')
}

/** The projects a new thread can be filed under — enabled ones only. */
export function fetchProjects(): Promise<ApiProject[]> {
  return getJson<ApiProject[]>('/api/trunkline/projects')
}

export function fetchThreads(): Promise<ApiThread[]> {
  return getJson<ApiThread[]>('/api/trunkline/threads')
}

/**
 * One read under a thread. Every /threads/{id} sub-resource 404s together —
 * an id the relay does not know is not an empty ledger — so a missing thread
 * comes back as null once, here, rather than four times at the call sites.
 */
async function threadRead<T>(id: string, suffix: string): Promise<T | null> {
  const path = `/api/trunkline/threads/${encodeURIComponent(id)}${suffix}`
  const res = await fetch(path)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`)
  return (await res.json()) as T
}

export const fetchThread = (id: string) => threadRead<ApiThread>(id, '')

/** The thread's chat ledger, oldest first — its own request, never inlined. */
export const fetchThreadMessages = (id: string) =>
  threadRead<ApiThreadMessage[]>(id, '/messages')

/** The thread's conversations, each carrying its runs. */
export const fetchThreadConversations = (id: string) =>
  threadRead<ApiConversation[]>(id, '/conversations')

/** Null both when the thread is unknown and when no project claims it — the
 *  console renders the same "unattributed" strip either way. */
export const fetchThreadProject = (id: string) =>
  threadRead<ApiProject | null>(id, '/project')

/** Feelers the thread is still blocked on. */
export const fetchThreadBatches = (id: string) =>
  threadRead<ApiDeferredBatch[]>(id, '/batches')

/**
 * Read one SSE response frame-by-frame, invoking onEvent per `data:` payload.
 * Resolves when the stream closes; rejects on transport failure or non-2xx.
 */
async function streamSse(
  path: string,
  body: unknown,
  onEvent: (event: WireEvent) => void,
): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok || res.body == null) {
    throw new Error(`POST ${path} → ${res.status}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue
        onEvent(JSON.parse(line.slice(6)) as WireEvent)
      }
    }
  }
}

export function streamDirective(
  threadId: string,
  body: DirectiveBody,
  onEvent: (event: WireEvent) => void,
): Promise<void> {
  return streamSse(
    `/api/trunkline/threads/${encodeURIComponent(threadId)}/messages`,
    body,
    onEvent,
  )
}

export function resolveBatch(
  batchId: string,
  body: BatchResponseBody,
  onEvent: (event: WireEvent) => void,
): Promise<void> {
  return streamSse(`/api/trunkline/batches/${batchId}/resolve`, body, onEvent)
}
