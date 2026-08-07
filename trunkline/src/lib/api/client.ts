/**
 * Real octomate endpoints under /api/trunkline. The dev server proxies /api
 * and /oauth to the FastAPI instance at 127.0.0.1:8000 (see vite.config.ts) so
 * requests stay same-origin — the backend ships no CORS middleware. In
 * production the console is mounted on that same app, so the paths hold as-is.
 */
import type {
  ApiChannelInfo,
  ApiRoute,
  ApiThreadDetail,
  ApiThreadSummary,
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

export function fetchLiveThreads(): Promise<ApiThreadSummary[]> {
  return getJson<ApiThreadSummary[]>('/api/trunkline/threads')
}

/** The live thread, or null when the backend does not know the id. */
export async function fetchLiveThreadDetail(
  id: string,
): Promise<ApiThreadDetail | null> {
  const res = await fetch(`/api/trunkline/threads/${encodeURIComponent(id)}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`GET /api/trunkline/threads/${id} → ${res.status}`)
  return (await res.json()) as ApiThreadDetail
}

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
