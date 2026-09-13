import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiUserProfile } from '../src/lib/api/events.ts'

let server: ViteDevServer
let inspectLinkProfile: typeof import('../src/lib/api/auth.ts').inspectLinkProfile
let confirmLinkProfile: typeof import('../src/lib/api/auth.ts').confirmLinkProfile

const profile: ApiUserProfile = {
  id: 'profile-id', channel_tentacle_id: 'slack', channel_user_id: 'U1',
  user_id: null, name: 'Alice on Slack', nickname: null, gender: null, age: null, title: null,
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ inspectLinkProfile, confirmLinkProfile } = await server.ssrLoadModule('/src/lib/api/auth.ts'))
})
after(async () => { await server?.close() })
afterEach(() => { mock.restoreAll() })

test('inspection sends the ticket only in the same-origin POST body', async () => {
  const pending = { profile, expires_at: '2026-09-12T01:00:00Z' }
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json(pending))
  assert.deepEqual(await inspectLinkProfile('private-ticket'), pending)
  const [path, init] = fetch.mock.calls[0].arguments
  assert.equal(path, '/api/auth/link-profile/inspect')
  assert.equal(init?.method, 'POST')
  assert.equal(new Headers(init?.headers).get('X-Octomate-Request'), '1')
  assert.deepEqual(JSON.parse(String(init?.body)), { token: 'private-ticket' })
})

test('confirmation keeps the displayed account even when the session refreshes', async () => {
  const linked = { ...profile, user_id: 'alice-id' }
  const responses = [
    new Response(null, { status: 401 }),
    new Response(null, { status: 204 }),
    Response.json(linked),
  ]
  const fetch = mock.method(globalThis, 'fetch', async () => {
    const response = responses.shift()
    assert.ok(response, 'Unexpected extra request')
    return response
  })
  assert.deepEqual(await confirmLinkProfile('private-ticket', 'alice-id'), linked)
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), [
    '/api/auth/link-profile/confirm', '/api/auth/refresh', '/api/auth/link-profile/confirm',
  ])
  for (const index of [0, 2]) {
    const init = fetch.mock.calls[index].arguments[1]
    assert.equal(init?.method, 'POST')
    assert.equal(new Headers(init?.headers).get('X-Octomate-Request'), '1')
    assert.deepEqual(JSON.parse(String(init?.body)), {
      token: 'private-ticket', expected_user_id: 'alice-id',
    })
  }
})

test('an account switch refusal reaches the form without retrying confirmation', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json(
    { detail: 'Your signed-in account changed.' }, { status: 409 },
  ))
  await assert.rejects(confirmLinkProfile('private-ticket', 'alice-id'), /account changed/)
  assert.equal(fetch.mock.callCount(), 1)
})
