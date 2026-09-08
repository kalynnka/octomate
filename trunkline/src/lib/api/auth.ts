/**
 * Local accounts under /api/auth: the browser session and the API keys it
 * manages. The session rides two HttpOnly cookies the console never reads —
 * what it sees is a 401, and `apiFetch` answers one with a single refresh and
 * a retry before letting the failure through. Every write carries the
 * same-origin header the relay demands of cookie-authenticated requests.
 */

export type ApiKeyScope = 'hooks' | 'mcp'

export interface ApiUser {
  id: string
  username: string
  name: string
  nickname: string | null
}

/** One issued key, as the relay lists it — the token itself is never here. */
export interface ApiApiKey {
  id: string
  user_id: string
  name: string
  /** the token's first characters, for telling keys apart */
  key_prefix: string
  scopes: ApiKeyScope[]
  created_at: string
  expires_at: string | null
  revoked_at: string | null
}

/** The one response that carries a token: the reply to issuing it. */
export interface ApiIssuedKey {
  key: ApiApiKey
  token: string
}

export interface LoginBody {
  username: string
  password: string
}

export interface RegistrationBody {
  username: string
  password: string
  name: string
  invitation: string
}

export interface ApiKeyBody {
  name: string
  scopes: ApiKeyScope[]
  /** timezone-aware ISO instant; absent for a key that never expires */
  expires_at?: string
}

/** One field's rejection, as FastAPI reports body validation. */
export interface ValidationIssue {
  loc: (string | number)[]
  msg: string
  type: string
}

/**
 * A refused request, carrying the relay's own account of why: a sentence when
 * a manager refused it, a list of field issues when the body failed validation,
 * nothing when the answer had no JSON body at all.
 */
export class ApiError extends Error {
  status: number
  detail: string | ValidationIssue[] | null

  constructor(status: number, detail: string | ValidationIssue[] | null) {
    super(typeof detail === 'string' ? detail : `request refused (${status})`)
    this.status = status
    this.detail = detail
  }
}

/** The body field an issue is about — "password", "scopes" — or '' when the
 *  issue names no field. */
export function issueField(issue: ValidationIssue): string {
  const field = issue.loc[1]
  return typeof field === 'string' ? field : ''
}

/**
 * An issue worded for the person typing. The password's length checks come
 * back counting "items" — the secret's characters — so they are restated; the
 * complexity rule arrives behind pydantic's "Value error, " and loses it.
 */
export function issueText(issue: ValidationIssue): string {
  if (issueField(issue) === 'password') {
    if (issue.type === 'too_short') return 'Use at least 11 characters.'
    if (issue.type === 'too_long') return 'Use at most 1024 characters.'
  }
  return issue.msg.replace(/^Value error, /, '')
}

/**
 * What a refused form says, in the relay's own words when they are the right
 * ones. Null for a relay problem, which the page reports on its own line — and
 * for anything that was not a refusal at all.
 */
export function refusalText(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status >= 500) return null
  if (Array.isArray(error.detail)) return error.detail.map(issueText).join(' ')
  return error.detail ?? `The relay refused the request (${error.status}).`
}

let sessionLost: (() => void) | null = null

/** What happens when a request is refused even after a refresh: the session is
 *  over, and the console has to say so. */
export function onSessionLost(listener: () => void) {
  sessionLost = listener
}

let refreshing: Promise<boolean> | null = null

/**
 * One refresh at a time. The refresh token is single-use, so two requests that
 * met a 401 together must share the rotation rather than race for it — the
 * loser of that race would be refused, and read as a session that ended.
 */
function refreshSession(): Promise<boolean> {
  refreshing ??= fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'X-Octomate-Request': '1' },
  })
    .then((res) => res.ok)
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

export interface ApiRequest {
  method?: string
  json?: unknown
  /** whether a 401 is a session that lapsed, worth one refresh and a retry.
   *  False on the credential endpoints, whose 401 is the answer itself. */
  retry?: boolean
}

/**
 * A same-origin request to the relay. Writes carry the header cookie
 * authentication requires; a 401 on a read is met with one refresh and one
 * retry, and a 401 that survives that is the session being over.
 */
export async function apiFetch(path: string, request: ApiRequest = {}): Promise<Response> {
  const method = request.method ?? 'GET'
  const headers: Record<string, string> = {}
  if (method !== 'GET' && method !== 'HEAD') headers['X-Octomate-Request'] = '1'
  if (request.json !== undefined) headers['Content-Type'] = 'application/json'
  const init: RequestInit = {
    method,
    headers,
    body: request.json === undefined ? undefined : JSON.stringify(request.json),
  }
  let res = await fetch(path, init)
  if (res.status !== 401 || request.retry === false) return res
  if (await refreshSession()) res = await fetch(path, init)
  if (res.status === 401) sessionLost?.()
  return res
}

/** Turn a refused response into the error the forms render. */
async function refuse(res: Response): Promise<never> {
  let detail: ApiError['detail'] = null
  try {
    detail = ((await res.json()) as { detail?: ApiError['detail'] }).detail ?? null
  } catch {
    // The dev proxy answers a downed relay with a text page; there is no detail.
  }
  throw new ApiError(res.status, detail)
}

export async function fetchMe(): Promise<ApiUser> {
  const res = await apiFetch('/api/auth/me')
  if (!res.ok) return refuse(res)
  return (await res.json()) as ApiUser
}

export async function login(body: LoginBody): Promise<void> {
  const res = await apiFetch('/api/auth/login', { method: 'POST', json: body, retry: false })
  if (!res.ok) return refuse(res)
}

export async function register(body: RegistrationBody): Promise<ApiUser> {
  const res = await apiFetch('/api/auth/register', { method: 'POST', json: body, retry: false })
  if (!res.ok) return refuse(res)
  return (await res.json()) as ApiUser
}

/** End the session. A 401 means it had already ended, which is the outcome asked for. */
export async function logout(): Promise<void> {
  const res = await apiFetch('/api/auth/logout', { method: 'POST', retry: false })
  if (!res.ok && res.status !== 401) return refuse(res)
}

export async function fetchApiKeys(): Promise<ApiApiKey[]> {
  const res = await apiFetch('/api/auth/api-keys')
  if (!res.ok) return refuse(res)
  return (await res.json()) as ApiApiKey[]
}

export async function createApiKey(body: ApiKeyBody): Promise<ApiIssuedKey> {
  const res = await apiFetch('/api/auth/api-keys', { method: 'POST', json: body })
  if (!res.ok) return refuse(res)
  return (await res.json()) as ApiIssuedKey
}

export async function revokeApiKey(id: string): Promise<void> {
  const res = await apiFetch(`/api/auth/api-keys/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) return refuse(res)
}
