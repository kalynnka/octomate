import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiProfileInfo, ApiUserProfile } from '../src/lib/api/events.ts'

let server: ViteDevServer
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let useAuth: typeof import('../src/state/auth.ts').useAuth
let ControlPage: typeof import('../src/features/control/ControlPage.tsx').ControlPage
let ControlRail: typeof import('../src/features/control/ControlRail.tsx').ControlRail

const linkedProfile: ApiUserProfile = {
  id: 'discord-profile', channel_tentacle_id: 'discord', channel_user_id: '123456789012345678',
  user_id: 'alice-id', name: 'Alice on Discord', nickname: 'Ali', gender: null, age: null, title: null,
}
const profile: ApiProfileInfo = {
  user: { id: 'alice-id', username: 'alice', name: 'Alice Smith', nickname: null },
  profiles: [linkedProfile], mcps: [],
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ useAuth } = await server.ssrLoadModule('/src/state/auth.ts'))
  ;({ ControlPage } = await server.ssrLoadModule('/src/features/control/ControlPage.tsx'))
  ;({ ControlRail } = await server.ssrLoadModule('/src/features/control/ControlRail.tsx'))
})
after(async () => { await server?.close() })
afterEach(() => {
  queryClient.clear()
  queryClient.setQueryDefaults(['profile'], {})
  useConsole.getInitialState().mgmtSec = ''
  useAuth.getInitialState().user = null
})

test('control navigation has one Profile section for the account and linked channels', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  const html = renderToStaticMarkup(createElement(ControlRail))
  const buttons = html.match(/<button\b[^>]*>.*?<\/button>/g) ?? []
  const active = buttons.filter((button) => button.includes('aria-current="page"'))
  assert.equal(active.length, 1)
  assert.ok(active[0].includes('>Profile</span>'))
  assert.equal((html.match(/>Profile<\/span>/g) ?? []).length, 1)
  assert.doesNotMatch(html, />Channels<\/span>/)
})

test('Profile offers native OAuth channels without requiring an installed MCP', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], { ...profile, profiles: [] })
  queryClient.setQueryData(['profile-authorizations'], [{ id: 'discord-dev', type: 'discord' }])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('Link a channel'))
  assert.ok(html.includes('Connect Discord'))
  assert.ok(html.includes('discord-dev'))
  assert.ok(!html.includes('Connect Slack'))
  assert.match(html, /<button[^>]*>[^]*?Connect Discord/)
})

for (const channelId of ['discord', 'discord-development-with-a-long-instance-name']) {
  test(`channel authorization uses the indented wrapping layout for ${channelId}`, () => {
    useConsole.getInitialState().mgmtSec = 'profile'
    useAuth.getInitialState().user = profile.user
    queryClient.setQueryData(['profile'], { ...profile, profiles: [] })
    queryClient.setQueryData(['profile-authorizations'], [{ id: channelId, type: 'discord' }])
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
    const button = (html.match(/<button\b.*?<\/button>/g) ?? []).find((button) => button.includes('Connect Discord'))
    assert.ok(button)
    assert.match(button, /class="trk-profile-picker-connect"/)
    assert.match(html, /<div style="margin-top:16px;margin-left:18px;padding-top:12px;border-top:1px solid var\(--line-divider\)"><p[^>]*margin:0 10px 8px[^>]*>Link a channel<\/p>/)
    assert.match(button, /class="trk-profile-picker-index"[^>]*>\+<\/span>/)
    assert.doesNotMatch(button, /Authorize, then confirm the profile/)
    if (channelId !== 'discord') assert.ok(button.includes(channelId))
  })
}

test('linked channels hide their Connect button and the empty linking section', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], profile)
  queryClient.setQueryData(['profile-authorizations'], [{ id: 'discord', type: 'discord' }])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('Show Discord profile for Alice on Discord'))
  assert.doesNotMatch(html, /Connect Discord|Link a channel|trk-profile-picker-connect/)
})

test('unlinked channels remain available when another channel is linked', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], profile)
  queryClient.setQueryData(['profile-authorizations'], [{ id: 'discord', type: 'discord' }, { id: 'slack', type: 'slack' }])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('Link a channel'))
  assert.ok(html.includes('Connect Slack'))
  assert.doesNotMatch(html, /Connect Discord/)
})

test('link availability matches the channel instance rather than its platform', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], profile)
  queryClient.setQueryData(['profile-authorizations'], [{ id: 'discord', type: 'discord' }, { id: 'discord-dev', type: 'discord' }])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.equal((html.match(/Connect Discord/g) ?? []).length, 1)
  assert.ok(html.includes('discord-dev'))
})

test('removing a linked profile makes its Connect button available again', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], profile)
  queryClient.setQueryData(['profile-authorizations'], [{ id: 'discord', type: 'discord' }])
  const page = createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage))
  assert.doesNotMatch(renderToStaticMarkup(page), /Connect Discord/)
  queryClient.setQueryData(['profile'], { ...profile, profiles: [] })
  const html = renderToStaticMarkup(page)
  assert.ok(html.includes('Connect Discord'))
  assert.ok(html.includes('Link a channel'))
})

test('Profile explains when no channel OAuth is configured', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile-authorizations'], [])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('No channel OAuth is configured.'))
  assert.ok(!html.includes('Connect Discord'))
})

test('Profile starts with the account card in front and its technical ID always visible', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], profile)
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  for (const text of ['Octomate account', 'Alice Smith', '@alice', 'Change password', 'User ID']) {
    assert.ok(html.includes(text), `Missing profile content: ${text}`)
  }
  const accountCard = html.match(/<article\b[^>]*id="trk-profile-card-account".*?<\/article>/)?.[0]
  assert.ok(accountCard)
  assert.match(accountCard, /aria-hidden="false" data-front="true"/)
  assert.match(accountCard, /aria-hidden="true">AS<\/div>/)
  assert.match(accountCard, /<h2 id="trk-profile-card-account-name">Alice Smith<span aria-hidden="true"/)
  assert.ok(accountCard.includes('trk-account-masthead'))
  assert.ok(accountCard.includes('Personal account'))
  assert.ok(accountCard.includes('solid var(--trk-bracket)'))
  assert.doesNotMatch(accountCard, /<details\b|\binert\b|Also known as/)
  assert.ok(accountCard.includes(profile.user.id))
  assert.match(html, /aria-label="Show Octomate profile for Alice Smith" aria-pressed="true"/)
})

test('the profile card uses the username when the display name is missing and shows an optional nickname', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = { ...profile.user, name: '', nickname: 'Ali' }
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.match(html, /<h2 id="trk-profile-card-account-name">alice<span aria-hidden="true"/)
  assert.ok(html.includes('Also known as Ali'))
})

test('the profile card escapes names and omits a duplicate nickname', () => {
  const name = '<script>Alice</script>'
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = { ...profile.user, name, nickname: name }
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('&lt;script&gt;Alice&lt;/script&gt;'))
  assert.doesNotMatch(html, /<script>|Also known as/)
})

test('profiles share a fixed decorative stack, with inactive cards inert and hidden from assistive technology', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], { ...profile, profiles: [{ ...linkedProfile, title: 'Designer', age: 0 }] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('Discord'))
  assert.ok(html.includes('Alice on Discord'))
  assert.match(html, /aria-label="Show Discord profile for Alice on Discord" aria-pressed="false" aria-controls="trk-profile-card-discord-profile"/)
  const cards = html.match(/<article\b.*?<\/article>/g) ?? []
  assert.equal(cards.length, 2)
  assert.match(cards[1], /aria-hidden="true" inert="" data-front="false"/)
  assert.match(html, /class="trk-profile-back" aria-hidden="true" style="z-index:-1;transform:translate\(12px, 12px\)"/)
  for (const detail of ['Designer', 'Gender', 'Age', 'Account ID', 'Profile ID', 'Channel identity', linkedProfile.id, linkedProfile.channel_user_id]) {
    assert.ok(cards[1].includes(detail), `Missing channel detail: ${detail}`)
  }
  assert.match(cards[1], /<dd[^>]*>0<\/dd>/)
  assert.doesNotMatch(cards[1], /Change password|<details\b|User ID|alice-id/)
  assert.doesNotMatch(cards[1].match(/^<article\b[^>]*>/)?.[0] ?? '', /transform:/)
  assert.doesNotMatch(html, /trk-profile-detail|<table\b/)
})

test('multiple profiles on one channel have distinct selectors and cards', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], { ...profile, profiles: [linkedProfile, {
    ...linkedProfile, id: 'second-discord', name: 'Work Alice', channel_user_id: '987654321',
  }] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.match(html, /aria-controls="trk-profile-card-discord-profile"/)
  assert.match(html, /aria-controls="trk-profile-card-second-discord"/)
  assert.ok(html.includes('Show Discord profile for Work Alice'))
  assert.equal((html.match(/<article\b/g) ?? []).length, 3)
  const cards = html.match(/<article\b.*?<\/article>/g) ?? []
  for (const card of cards) assert.doesNotMatch(card.match(/^<article\b[^>]*>/)?.[0] ?? '', /transform:/)
  assert.equal((html.match(/class="trk-profile-back"/g) ?? []).length, 2)
  assert.ok(html.includes('translate(12px, 12px)'))
  assert.ok(html.includes('translate(24px, 24px)'))
  assert.ok(html.includes('margin-right:24px'))
  assert.ok(html.includes('margin-bottom:24px'))
})

test('channel selectors are nested and only external profiles offer disconnect', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], { ...profile, profiles: [linkedProfile, {
    ...linkedProfile, id: 'trunkline-profile', channel_tentacle_id: 'trunkline',
  }] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  const buttons = html.match(/<button\b.*?<\/button>/g) ?? []
  assert.match(html, /trk-profile-picker-heading[^>]*><span[^>]*>Channels<\/span><span[^>]*>02<\/span>/)
  assert.match(buttons.find((button) => button.includes('Show Octomate')) ?? '', /data-channel="false"/)
  assert.match(buttons.find((button) => button.includes('Show Octomate')) ?? '', />U01<\/span>/)
  for (const name of ['Discord', 'Trunkline']) {
    const button = buttons.find((button) => button.includes(`Show ${name}`)) ?? ''
    assert.match(button, /data-channel="true"/)
    assert.match(button, /trk-profile-picker-index/)
    assert.match(button, /trk-profile-picker-status/)
    assert.doesNotMatch(button, /trk-profile-picker-arrow|trk-profile-picker-mark|↗/)
  }
  const discord = html.match(/<article\b[^>]*id="trk-profile-card-discord-profile".*?<\/article>/)?.[0] ?? ''
  const trunkline = html.match(/<article\b[^>]*id="trk-profile-card-trunkline-profile".*?<\/article>/)?.[0] ?? ''
  assert.ok(discord.includes('Disconnect profile'))
  assert.doesNotMatch(trunkline, /Disconnect profile/)
  assert.ok(trunkline.includes('Uses your signed-in Octomate account directly.'))
})

test('channel names blend their brand colors with theme text for a muted palette', () => {
  const channels = [
    { id: 'trunkline', name: 'Trunkline', color: 'var(--color-accent)' },
    { id: 'discord', name: 'Discord', color: '#5865F2' },
    { id: 'slack', name: 'Slack', color: '#746576' },
    { id: 'lark', name: 'Lark', color: '#666D82' },
    { id: 'napcat', name: 'Napcat', color: '#6A828B' },
    { id: 'claude-native', name: 'Claude', color: '#98796A' },
    { id: 'codex-native', name: 'Codex', color: '#677D73' },
    { id: 'deepseek-native', name: 'DeepSeek', color: '#66709B' },
    { id: 'custom', name: 'Custom', color: 'var(--fg-3)' },
  ]
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], {
    ...profile, profiles: channels.map((channel) => ({ ...linkedProfile, id: channel.id, channel_tentacle_id: channel.id })),
  })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  const buttons = html.match(/<button\b.*?<\/button>/g) ?? []
  for (const channel of channels) {
    const button = buttons.find((button) => button.includes(`Show ${channel.name} profile for`))
    assert.ok(button, `Missing channel name: ${channel.name}`)
    assert.ok(button.includes(`color:color-mix(in srgb, ${channel.color} 50%, var(--fg-1))`), `Missing muted text color for ${channel.name}`)
  }
})

test('an account without linked profiles has a single front card and the empty channel notice', () => {
  useConsole.getInitialState().mgmtSec = 'profile'
  useAuth.getInitialState().user = profile.user
  queryClient.setQueryData(['profile'], { ...profile, profiles: [] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.ok(html.includes('No channel identities are linked to your account.'))
  assert.equal((html.match(/<article\b/g) ?? []).length, 1)
  assert.doesNotMatch(html, /class="trk-profile-back"/)
  assert.ok(html.includes('margin-bottom:0'))
})

for (const failed of [false, true]) {
  test(`the account card stays available when channels are ${failed ? 'unavailable' : 'loading'}`, () => {
    useConsole.getInitialState().mgmtSec = 'profile'
    useAuth.getInitialState().user = profile.user
    if (failed) {
      queryClient.setQueryDefaults(['profile'], { retryOnMount: false })
      queryClient.getQueryCache().build(queryClient, { queryKey: ['profile'] }).setState({
        status: 'error', error: new Error('Unavailable'), fetchStatus: 'idle',
      })
    }
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
    assert.ok(html.includes('Change password'))
    assert.ok(html.includes(profile.user.id))
    assert.ok(html.includes(failed ? 'Could not load channels.' : 'Loading channels…'))
  })
}

test('Agents show the supplied permission names and mark the configured default', () => {
  useConsole.getInitialState().mgmtSec = 'agents'
  queryClient.setQueryData(['agents'], [{
    id: 'auditor', description: 'Review agent', gateway: false,
    default_model: null, routes: [], driven_sessions: 0, native_sessions: 0,
    default_permission_mode: 'audit-only',
    permission_modes: [
      { value: 'audit-only', name: 'Read & review', description: 'No edits allowed.' },
      { value: 'workspace-write', name: 'Workspace edits', description: null },
    ],
  }])
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
  assert.match(html, />Permissions</)
  assert.match(html, />Read &amp; review</)
  assert.match(html, />Workspace edits</)
  assert.match(html, /No edits allowed\./)
  assert.equal((html.match(/aria-label="Default permission mode"/g) ?? []).length, 1)
})

test('permission cycling starts from the configured default and sends mode IDs', async () => {
  queryClient.setQueryData(['permission-modes'], {
    auditor: { default: 'audit-only', modes: [
      { value: 'audit-only', name: 'Read & review', description: null },
      { value: 'workspace-write', name: 'Workspace edits', description: null },
    ] },
  })
  useConsole.setState({ ntOn: true, ntAgent: 'auditor', ntPermissionMode: null })
  await useConsole.getState().actions.cyclePermissionMode()
  assert.equal(useConsole.getState().ntPermissionMode, 'workspace-write')
  await useConsole.getState().actions.cyclePermissionMode()
  assert.equal(useConsole.getState().ntPermissionMode, 'audit-only')
  await useConsole.getState().actions.cyclePermissionMode()
  assert.equal(useConsole.getState().ntPermissionMode, 'workspace-write')
  useConsole.setState({ ntOn: false, ntAgent: '', ntPermissionMode: null })
})
