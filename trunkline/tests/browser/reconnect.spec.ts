import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiThread, ApiThreadMessage, ThreadOperations, WireEvent } from '../../src/lib/api/events'

const timestamp = '2026-10-17T08:00:00Z'
const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Reconnect in place',
  project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
}
const messages: ApiThreadMessage[] = Array.from({ length: 40 }, (_, index) => ({
  kind: 'message', id: `saved-${String(index).padStart(2, '0')}`, thread_id: thread.id, platform_message_id: null,
  reply_id: '', happened_at: timestamp, direction: 'inbound', actor_kind: 'human',
  agent_tentacle_id: null, sender: null, segments: [], created_at: timestamp,
  message_text: `Earlier message ${index}. ${'This message stays in the conversation. '.repeat(10)}`,
}))

for (const width of [1280, 390]) {
  test(`returning to the tab restores live updates without refreshing the conversation (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ colorScheme: width === 390 ? 'dark' : 'light', reducedMotion: width === 390 ? 'reduce' : 'no-preference' })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    const sockets = new Map<string, WebSocketRoute[]>()
    const requests: string[] = []
    let snapshot: Promise<void> | undefined
    let releaseSnapshot!: () => void
    let navigations = 0
    page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++ })
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => {
      const url = new URL(socket.url())
      const key = url.searchParams.has('thread_id') ? 'thread' : url.pathname.split('/').at(-1)!
      sockets.set(key, [...(sockets.get(key) ?? []), socket])
    })
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      requests.push(path)
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') {
        await snapshot
        return route.fulfill({ json: [thread] })
      }
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [...messages].reverse(), total: messages.length, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/thread-a/usage')) return route.fulfill({ json: {} })
      if (path === '/api/trunkline/health') return route.fulfill({ json: { ok: true } })
      if (path === '/api/trunkline/permissions') return route.fulfill({ json: {} })
      if (path.endsWith('/thread-a/operations')) return route.fulfill({ json: {
        source: null, barred: {},
        teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
        summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
      } satisfies ThreadOperations })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
        '/api/trunkline/threads/thread-a/conversations', '/api/trunkline/threads/thread-a/batches'].includes(path)) {
        return route.fulfill({ json: [] })
      }
      return route.fulfill({ status: 404, json: { detail: 'Unmocked test API' } })
    })
    await page.goto('/threads/thread-a')
    await expect(page).toHaveTitle(/Octomate/i)
    const log = page.locator('#trk-chatlog')
    const saved = page.locator('#pm-saved-39')
    await expect(saved).toBeVisible()
    const original = await saved.elementHandle()
    const composer = page.getByPlaceholder('directive… or / for commands')
    await composer.fill('Keep this unsent draft')
    await page.evaluate(() => document.fonts.ready)
    await log.evaluate(async el => {
      el.scrollTop = 400
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    })
    const scrollTop = await log.evaluate(el => el.scrollTop)
    await expect.poll(() => [...sockets.keys()].sort()).toEqual(['activity', 'thread', 'user'])
    const connected = Object.fromEntries([...sockets].map(([key, values]) => [key, values.length]))
    const historyReads = requests.filter(path => path.startsWith('/api/trunkline/threads/thread-a')).length

    // An ordinary activation leaves healthy sockets alone.
    await page.evaluate(() => {
      document.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new Event('focus'))
    })
    expect(Object.fromEntries([...sockets].map(([key, values]) => [key, values.length]))).toEqual(connected)
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    for (const values of sockets.values()) await values.at(-1)!.close({ code: 1012, reason: 'Connection lost while backgrounded' })
    await expect(page.getByRole('button', { name: 'Reconnect', exact: true })).toBeVisible()
    snapshot = new Promise(resolve => { releaseSnapshot = resolve })
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
      document.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new Event('focus'))
    })
    for (const key of ['thread', 'activity', 'user']) await expect.poll(() => sockets.get(key)?.length).toBe(connected[key] + 1)
    const notice = page.locator('.trk-thread-list-alert')
    await expect(notice).toHaveAttribute('role', 'status')
    await expect(notice.locator('strong [data-text-current]')).toHaveText('Reconnecting…')
    await expect(page.getByRole('button', { name: 'Connecting…', exact: true })).toBeDisabled()
    await expect(notice.locator('svg')).toHaveCSS('animation-name', width === 390 ? 'none' : 'lt-spin')
    await expect(notice).toHaveCSS('opacity', '1')
    await expect(notice.locator('strong [data-text-current]')).toHaveCSS('opacity', '1')
    await page.screenshot({ path: `/private/tmp/trunkline-reconnecting-${width}.png` })
    releaseSnapshot()
    await expect(notice).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(composer).toHaveValue('Keep this unsent draft')
    expect(await saved.evaluate((current, previous) => current === previous, original)).toBe(true)
    expect(await log.evaluate(el => el.scrollTop)).toBe(scrollTop)
    expect(requests.filter(path => path.startsWith('/api/trunkline/threads/thread-a')).length).toBe(historyReads)
    expect(navigations).toBe(1)

    const events: WireEvent[] = [
      { event_kind: 'run_started', address: {
        channel_tentacle_id: 'codex', chat_type: 'thread', chat_id: thread.id,
        user_id: 'owner', channel_thread_id: null, shared: false,
      } },
      { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'Live updates are back.' } },
      { event_kind: 'run_ended' },
    ]
    for (const event of events) sockets.get('thread')!.at(-1)!.send(JSON.stringify(event))
    await expect(log.getByText('Live updates are back.', { exact: true })).toHaveCount(1)
    await expect(log.locator('.trk-print-caret')).toHaveCount(0)
    expect(await log.evaluate(el => el.scrollTop)).toBe(scrollTop)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/trunkline-reconnect-${width}.png` })

    await sockets.get('user')!.at(-1)!.close({ code: 1012 })
    snapshot = new Promise(resolve => { releaseSnapshot = resolve })
    await page.getByRole('button', { name: 'Reconnect', exact: true }).click()
    await expect.poll(() => sockets.get('user')?.length).toBe(connected.user + 2)
    await expect(page.getByRole('button', { name: 'Connecting…', exact: true })).toBeDisabled()
    await sockets.get('user')!.at(-1)!.close({ code: 1012 })
    releaseSnapshot()
    await expect(notice).toHaveAttribute('role', 'alert')
    await expect(page.getByRole('button', { name: 'Reconnect', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: 'Reconnect', exact: true }).click()
    await expect.poll(() => sockets.get('user')?.length).toBe(connected.user + 3)
    await expect(notice).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(sockets.get('thread')?.length).toBe(connected.thread + 1)
    expect(navigations).toBe(1)
    await expect(composer).toHaveValue('Keep this unsent draft')
  })
}
