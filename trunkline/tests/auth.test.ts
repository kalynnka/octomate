import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiUser } from '../src/lib/api/auth.ts'

const user: ApiUser = { id: 'alice-id', username: 'alice', name: 'Alice', nickname: null }
const browser: {
  window?: { location: Pick<Location, 'hash' | 'pathname' | 'search'> }
  history?: Pick<History, 'replaceState'>
} = globalThis
let server: ViteDevServer
let useAuth: typeof import('../src/state/auth.ts').useAuth

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false },
    appType: 'custom',
  })
  ;({ useAuth } = await server.ssrLoadModule('/src/state/auth.ts'))
})

after(async () => { await server?.close() })
afterEach(() => {
  mock.restoreAll()
  useAuth.setState({ status: 'booting', user: null, page: 'login', invitation: '', notice: null, relay: 'ok' })
  delete browser.window
  delete browser.history
})

test('an invitation link opens registration and removes the code from the address bar', async () => {
  browser.window = { location: { hash: '#invitation=invite-code', pathname: '/', search: '' } }
  const replaceState = mock.fn<History['replaceState']>()
  browser.history = { replaceState }
  mock.method(globalThis, 'fetch', async () => new Response(null, { status: 401 }))
  await useAuth.getState().actions.boot()
  assert.equal(useAuth.getState().status, 'signed-out')
  assert.equal(useAuth.getState().page, 'register')
  assert.equal(useAuth.getState().invitation, 'invite-code')
  assert.deepEqual(replaceState.mock.calls[0].arguments, [null, '', '/'])
})

test('registration signs in and discards the invitation', async () => {
  useAuth.setState({ invitation: 'invite-code', page: 'register' })
  mock.method(globalThis, 'fetch', async () => Response.json(user))
  await useAuth.getState().actions.register({
    username: 'alice', name: 'Alice', password: 'Test password1!', invitation: 'invite-code',
  })
  assert.equal(useAuth.getState().status, 'signed-in')
  assert.deepEqual(useAuth.getState().user, user)
  assert.equal(useAuth.getState().invitation, '')
})

test('reload refreshes an expired access session before restoring the account', async () => {
  browser.window = { location: { hash: '', pathname: '/', search: '' } }
  const responses = [new Response(null, { status: 401 }), new Response(null, { status: 204 }), Response.json(user)]
  const fetch = mock.method(globalThis, 'fetch', async () => {
    const response = responses.shift()
    assert.ok(response, 'Unexpected extra request')
    return response
  })
  await useAuth.getState().actions.boot()
  assert.equal(useAuth.getState().status, 'signed-in')
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), ['/api/auth/me', '/api/auth/refresh', '/api/auth/me'])
})

test('successful logout clears the account', async () => {
  useAuth.setState({ status: 'signed-in', user })
  mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  await useAuth.getState().actions.signOut()
  assert.equal(useAuth.getState().status, 'signed-out')
  assert.equal(useAuth.getState().user, null)
})

for (const status of [401, 500, 'offline'] as const) {
  test(`logout failure (${status}) keeps the account visible for retry`, async () => {
    useAuth.setState({ status: 'signed-in', user })
    mock.method(globalThis, 'fetch', async () => {
      if (status === 'offline') throw new TypeError('Network unavailable')
      return new Response(null, { status })
    })
    await assert.rejects(useAuth.getState().actions.signOut())
    assert.equal(useAuth.getState().status, 'signed-in')
    assert.deepEqual(useAuth.getState().user, user)
  })
}

test('password change returns to login with a confirmation', async () => {
  useAuth.setState({ status: 'signed-in', user })
  const fetch = mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const body = { current_password: 'Old password1!', password: 'New password1!' }
  await useAuth.getState().actions.changePassword(body)
  assert.equal(useAuth.getState().status, 'signed-out')
  assert.equal(useAuth.getState().user, null)
  const notice = useAuth.getState().notice
  assert.ok(notice)
  assert.match(notice, /Password changed/)
  assert.equal(fetch.mock.calls[0].arguments[1]?.body, JSON.stringify(body))
})

test('wrong current password leaves the user signed in', async () => {
  useAuth.setState({ status: 'signed-in', user })
  mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Current password is incorrect' }, { status: 403 }))
  await assert.rejects(useAuth.getState().actions.changePassword({ current_password: 'wrong', password: 'New password1!' }))
  assert.equal(useAuth.getState().status, 'signed-in')
})
