import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiUserProfile } from '../src/lib/api/events.ts'
import type { ApiLinkProfile, ApiUser } from '../src/lib/api/auth.ts'

let server: ViteDevServer
let inspectLinkProfile: typeof import('../src/lib/api/auth.ts').inspectLinkProfile
let confirmLinkProfile: typeof import('../src/lib/api/auth.ts').confirmLinkProfile
let LinkProfileDetails: typeof import('../src/features/auth/LinkProfilePage.tsx').LinkProfileDetails

const profile: ApiUserProfile = {
  id: 'profile-id', channel_tentacle_id: 'slack', channel_user_id: 'U1',
  user_id: null, name: 'Alice on Slack', nickname: null, gender: null, age: null, title: null,
}
const user: ApiUser = { id: 'alice-id', username: 'alice', name: 'Alice Smith', nickname: null }

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ inspectLinkProfile, confirmLinkProfile } = await server.ssrLoadModule('/src/lib/api/auth.ts'))
  ;({ LinkProfileDetails } = await server.ssrLoadModule('/src/features/auth/LinkProfilePage.tsx'))
})
after(async () => { await server?.close() })
afterEach(() => { mock.restoreAll() })

test('profile confirmation shows the available human details and the signed-in account', () => {
  const markup = renderToStaticMarkup(createElement(LinkProfileDetails, {
    profile: { ...profile, nickname: 'Ali', title: 'Designer', gender: 'Female', age: 30 }, user,
  }))
  for (const value of ['Slack', 'Display name', 'Alice on Slack', 'Nickname', 'Ali', 'Designer', 'Female', '30', 'Alice Smith (@alice)']) {
    assert.ok(markup.includes(value), `Missing profile detail: ${value}`)
  }
  assert.ok(!markup.includes(profile.id))
  assert.ok(!markup.includes(user.id))
})

test('Discord user IDs appear only in collapsed technical details', () => {
  const discordProfile = { ...profile, channel_tentacle_id: 'discord', channel_user_id: '123456789012345678', name: 'Alice' }
  const markup = renderToStaticMarkup(createElement(LinkProfileDetails, { profile: discordProfile, user }))
  const technicalStart = markup.indexOf('<details')
  assert.ok(technicalStart >= 0)
  assert.ok(!markup.slice(0, technicalStart).includes(discordProfile.channel_user_id))
  assert.match(markup, /<details\b[^>]*><summary/)
  assert.doesNotMatch(markup, /<details\b[^>]*\bopen/)
  assert.ok(markup.includes('Technical details'))
  assert.ok(markup.includes('Discord user ID'))
  assert.ok(markup.includes(discordProfile.channel_user_id))
  assert.ok(markup.includes('not an Octomate internal profile ID'))
  assert.ok(!markup.includes('Channel ID'))
  for (const label of ['Nickname', 'Title', 'Gender', 'Age']) assert.ok(!markup.includes(`>${label}</dt>`))
})

test('missing names do not turn an ID into a display name', () => {
  const markup = renderToStaticMarkup(createElement(LinkProfileDetails, {
    profile: { ...profile, name: '', age: 0 }, user,
  }))
  assert.ok(markup.includes('Name not provided by the channel'))
  assert.ok(!markup.slice(0, markup.indexOf('<details')).includes(profile.channel_user_id))
  assert.match(markup, />Age<\/dt><dd[^>]*>0<\/dd>/)
})

test('profile details escape channel-provided text and omit duplicate nicknames', () => {
  const name = '<script>untrusted name</script>'
  const markup = renderToStaticMarkup(createElement(LinkProfileDetails, {
    profile: { ...profile, name, nickname: name }, user,
  }))
  assert.ok(markup.includes('&lt;script&gt;untrusted name&lt;/script&gt;'))
  assert.ok(!markup.includes('<script>'))
  assert.ok(!markup.includes('>Nickname</dt>'))
})

test('inspection sends the ticket only in the same-origin POST body', async () => {
  const pending: ApiLinkProfile = {
    profile, expires_at: '2026-09-12T01:00:00Z',
  }
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
