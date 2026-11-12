import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiThread, ThreadOperations, WireEvent } from '../../src/lib/api/events'

for (const width of [1280, 390]) {
  test(`running threads stand out and sort first within their sections at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const timestamp = '2026-11-04T02:00:00Z'
    const native: ApiThread[] = Array.from({ length: 14 }, (_, index) => ({
      id: `native-${String(index).padStart(2, '0')}`, kind: index % 2 === 0 ? 'native_thread' : 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'codex', channel_thread_id: `native-key-${index}`,
      active_agent_tentacle_id: 'codex',
      title: index === 3 ? 'Design channel tool call UIs' : index === 10 ? 'Revisit compaction design' : `Saved conversation ${index}`,
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }))
    const slack = native.slice(0, 2).map((thread, index) => ({ ...thread, kind: 'thread' as const, id: `slack-${index}`, channel_tentacle_id: 'slack', title: index ? 'Research background tasks' : 'Slack conversation' }))
    const threads = [...native, ...slack]
    const operations: ThreadOperations = {
      source: null, barred: {},
      teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
      summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
    }
    const sockets = new Map<string, Set<WebSocketRoute>>()
    const watched = new Set<string>()
    const active = new Set<string>()
    let activitySocket: WebSocketRoute
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => {
      if (new URL(socket.url()).pathname.endsWith('/activity')) {
        activitySocket = socket
        sockets.set('activity', new Set([socket]))
        let previous = new Set<string>()
        socket.onMessage(message => {
          const request = JSON.parse(String(message)) as { thread_ids: string[]; thread_keys: string[] }
          watched.clear()
          request.thread_ids.forEach(id => watched.add(id))
          request.thread_keys.forEach(key => watched.add(key))
          for (const id of request.thread_ids) if (!previous.has(id)) socket.send(JSON.stringify({
            key: `id:${id}`, attention: false,
            event: active.has(id) ? { event_kind: 'run_activity_changed', activity: 'working' } : { event_kind: 'run_ended' },
          }))
          previous = new Set(request.thread_ids)
        })
        socket.onClose(() => sockets.delete('activity'))
        return
      }
      const query = new URL(socket.url()).searchParams
      const target = query.get('thread_id') ?? query.get('thread_key') ?? 'user'
      const group = sockets.get(target) ?? new Set<WebSocketRoute>()
      sockets.set(target, group)
      group.add(socket)
      socket.onClose(() => group.delete(socket))
    })
    const send = (id: string, event: WireEvent) => {
      if (event.event_kind === 'run_ended') active.delete(id)
      else active.add(id)
      activitySocket.send(JSON.stringify({ key: `id:${id}`, event, attention: false }))
    }
    // Every request stays in the fixture, away from the developer's backend and database.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: threads })
      const thread = threads.find(thread => path === `/api/trunkline/threads/${thread.id}`)
      if (thread) return route.fulfill({ json: thread })
      if (path.endsWith('/conversations/messages') || path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/project')) return route.fulfill({ json: null })
      if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/operations')) return route.fulfill({ json: operations })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: [] })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto('/threads/native-00', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle('Trunkline · Octomate')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    const sidebar = page.locator('#trk-sb-panel')
    const nativeRows = sidebar.locator('#trk-channel-codex [data-thread-id]')
    const slackRows = sidebar.locator('#trk-channel-slack [data-thread-id]')
    await expect(nativeRows).toHaveCount(12)
    await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-00')
    await expect(slackRows.first()).toHaveAttribute('data-thread-id', 'slack-0')
    await expect(sidebar.locator('[data-thread-origin]')).toHaveCount(0)
    await expect.poll(() => watched.has('native-10')).toBe(true)
    expect([...sockets.values()].reduce((total, group) => total + group.size, 0)).toBe(3)
    const running = sidebar.locator('[data-thread-id="native-10"]')
    await expect(running.locator('.trk-thread-dot')).toBeHidden()
    const titleLeft = await running.locator('.trk-thread-title').evaluate(element => element.getBoundingClientRect().left)
    const original = await running.elementHandle()
    send('native-10', { event_kind: 'run_activity_changed', activity: 'using_tools' })
    await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-10')
    await expect(running).toHaveAttribute('data-running', '')
    await expect(running.locator('.trk-thread-dot')).toBeVisible()
    expect(await running.locator('.trk-thread-title').evaluate(element => element.getBoundingClientRect().left)).toBe(titleLeft)
    await expect(running.locator('.trk-thread-title')).toHaveCSS('font-weight', '600')
    expect(await running.evaluate((element, previous) => element === previous, original)).toBe(true)
    await expect.poll(() => watched.has('native-03')).toBe(true)
    send('native-03', { event_kind: 'run_activity_changed', activity: 'thinking' })
    await expect(nativeRows.nth(0)).toHaveAttribute('data-thread-id', 'native-03')
    await expect(nativeRows.nth(1)).toHaveAttribute('data-thread-id', 'native-10')
    await expect(nativeRows.nth(2)).toHaveAttribute('data-thread-id', 'native-00')
    await expect.poll(() => watched.has('slack-1')).toBe(true)
    send('slack-1', { event_kind: 'run_activity_changed', activity: 'working' })
    await expect(slackRows.first()).toHaveAttribute('data-thread-id', 'slack-1')
    await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-03')
    send('native-03', { event_kind: 'run_ended', outcome: 'completed' })
    await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-10')
    await expect(sidebar.locator('[data-thread-id="native-03"]')).not.toHaveAttribute('data-running')
    await expect(sidebar.locator('[data-thread-id="native-03"] .trk-thread-dot')).toBeHidden()
    await expect(nativeRows.nth(1)).toHaveAttribute('data-thread-id', 'native-00')
    await page.reload({ waitUntil: 'domcontentloaded' })
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    // The snapshot restores both sections without sending another live event.
    await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-10')
    await expect(running).toHaveAttribute('data-running', '')
    await expect(running.locator('[data-thread-activity]')).toHaveText('Working')
    await expect(sidebar.locator('[data-thread-origin]')).toHaveCount(0)
    await expect(sidebar.locator('[data-thread-id="native-03"]')).not.toHaveAttribute('data-running')
    await expect(slackRows.first()).toHaveAttribute('data-thread-id', 'slack-1')
    await expect.poll(() => [...sockets.values()].reduce((total, group) => total + group.size, 0)).toBe(3)
    await running.click()
    await expect(running).toHaveAttribute('aria-current', 'page')
    await expect.poll(() => [...sockets.values()].reduce((total, group) => total + group.size, 0)).toBe(3)
    if (width === 390) {
      await page.keyboard.press('Escape')
      await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    }
    send('native-03', { event_kind: 'run_activity_changed', activity: 'thinking' })
    const dot = running.locator('.trk-thread-dot')
    await expect(dot).toHaveCSS('animation-name', 'none')
    await expect(dot).not.toHaveCSS('box-shadow', 'none')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await expect(dot).toHaveCSS('animation-name', 'trk-thread-shine')
    const glow = await dot.evaluate(element => getComputedStyle(element).boxShadow)
    await expect.poll(() => dot.evaluate(element => getComputedStyle(element).boxShadow)).not.toBe(glow)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await expect(dot).toHaveCSS('animation-name', 'none')
    for (const theme of ['dark', 'light']) {
      await page.evaluate(async theme => {
        const path = '/src/state/console.ts'
        const { useConsole } = await import(path)
        useConsole.getState().actions.setTheme(theme)
      }, theme)
      await expect(running).toHaveAttribute('aria-current', 'page')
      await expect(nativeRows.first()).toHaveAttribute('data-thread-id', 'native-03')
      const idleTitle = sidebar.locator('[data-thread-id="native-00"] .trk-thread-title')
      await expect(running.locator('.trk-thread-title')).not.toHaveCSS('color', await idleTitle.evaluate(element => getComputedStyle(element).color))
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await sidebar.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-sidebar-running-${width}-${theme}.png` })
    }
    // Known active rows are promoted before pagination, including those beyond its first page.
    native.forEach(thread => active.add(thread.id))
    await page.evaluate(async ids => {
      const path = '/src/state/activity.ts'
      const { useActivity } = await import(path)
      useActivity.setState(Object.fromEntries(ids.map(id => [`id:${id}`, { text: 'Working', attention: false }])))
    }, native.map(thread => thread.id))
    await expect(nativeRows).toHaveCount(14)
    await expect(nativeRows.last()).toHaveAttribute('data-thread-id', 'native-13')
    await expect(sidebar.locator('#trk-channel-codex [data-running]')).toHaveCount(14)
    expect(await page.evaluate(async () => {
      const path = '/src/lib/queryClient.ts'
      const { queryClient } = await import(path)
      const rows: ApiThread[] | undefined = queryClient.getQueryData(['threads'])
      return rows?.map(thread => thread.id)
    })).toEqual(threads.map(thread => thread.id))
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
