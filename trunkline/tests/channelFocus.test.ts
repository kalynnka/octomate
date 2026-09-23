import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ChannelMeta, ThreadSummary } from '../src/lib/api/types.ts'

let server: ViteDevServer
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let ThreadsSidebar: typeof import('../src/features/threads/ThreadsSidebar.tsx').ThreadsSidebar

const channels: ChannelMeta[] = [
  { id: 'discord', label: 'Discord', sub: 'gateway', brand: '#5865f2' },
  { id: 'trunkline', label: 'Trunkline', sub: 'web', brand: '#d4621a' },
]
const threads: Record<string, ThreadSummary[]> = {
  discord: [{ id: 'discord-thread', key: 'discord-key', channelId: 'discord', title: 'Discord conversation', tone: 'idle', agentLabel: 'Codex' }],
  trunkline: [{ id: 'web-thread', key: 'web-key', channelId: 'trunkline', title: 'Web conversation', tone: 'idle', agentLabel: 'Codex' }],
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ ThreadsSidebar } = await server.ssrLoadModule('/src/features/threads/ThreadsSidebar.tsx'))
})
after(async () => { await server?.close() })
afterEach(() => {
  queryClient.clear()
  Object.assign(useConsole.getInitialState(), { chFold: {}, sbFold: false })
  useConsole.setState({ chFold: {}, sbFold: false })
})

test('switching channel focus opens the sidebar and immediately folds every other channel', () => {
  useConsole.setState({ sbFold: true, chFold: {} })
  const ids = channels.map((channel) => channel.id)
  useConsole.getState().actions.focusChannel('discord', ids)
  assert.equal(useConsole.getState().sbFold, false)
  assert.deepEqual(useConsole.getState().chFold, { discord: false, trunkline: true })
  useConsole.getState().actions.focusChannel('trunkline', ids)
  assert.deepEqual(useConsole.getState().chFold, { discord: true, trunkline: false })
  useConsole.getState().actions.focusChannel('trunkline', ids)
  assert.deepEqual(useConsole.getState().chFold, { discord: true, trunkline: false })
})

test('switching focus removes the previous channel content and highlights the new shortcut', () => {
  queryClient.setQueryData(['channels'], channels)
  queryClient.setQueryData(['threads'], threads)
  for (const id of ['discord', 'trunkline']) {
    useConsole.getState().actions.focusChannel(id, channels.map((channel) => channel.id))
    Object.assign(useConsole.getInitialState(), { chFold: useConsole.getState().chFold })
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ThreadsSidebar)))
    const active = channels.find((channel) => channel.id === id)!
    assert.ok(html.includes(threads[id][0].title))
    assert.ok(!html.includes(threads[id === 'discord' ? 'trunkline' : 'discord'][0].title))
    assert.ok(html.includes(`aria-label="Focus ${active.label}" aria-pressed="true"`))
    assert.equal((html.match(/aria-label="Focus [^"]+" aria-pressed="true"/g) ?? []).length, 1)
  }
})

test('the collapsed sidebar retains channel shortcuts and Control without hidden thread actions', () => {
  queryClient.setQueryData(['channels'], channels)
  queryClient.setQueryData(['threads'], threads)
  useConsole.getInitialState().sbFold = true
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ThreadsSidebar)))
  assert.ok(html.includes('aria-label="Focus Discord"'))
  assert.ok(html.includes('aria-label="Focus Trunkline"'))
  assert.ok(html.includes('aria-label="Control"'))
  assert.ok(!html.includes('Discord conversation'))
  assert.ok(!html.includes('Web conversation'))
  assert.ok(!html.includes('New trunkline thread'))
})
