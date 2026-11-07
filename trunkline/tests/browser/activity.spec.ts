import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiThread, ThreadOperations, WireEvent } from '../../src/lib/api/events'
import type { LedgerItem } from '../../src/lib/api/types'

for (const width of [1280, 390]) {
  test(`thread activity survives selection and expires independently of controls at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.clock.install()
    // Font CDN availability is outside this event-stream fixture.
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const timestamp = '2026-11-04T02:00:00Z'
    const first: ApiThread = {
      id: '01a18bed-47d7-75d0-b346-54d52be38615', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'trunkline-first', title: 'Status presentation',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const second: ApiThread = { ...first, id: '01a188dd-9bd4-7252-ab06-dfc69e7263a9', channel_tentacle_id: 'slack', channel_thread_id: 'slack-second', title: 'Background research' }
    const operations: ThreadOperations = {
      source: null, barred: {},
      teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
      summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
    }
    const sockets = new Map<string, Set<WebSocketRoute>>()
    const watched = new Set<string>()
    let activitySocket: WebSocketRoute
    const statuses = new Map<string, WireEvent>()
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => {
      if (new URL(socket.url()).pathname.endsWith('/activity')) {
        activitySocket = socket
        socket.onMessage(message => {
          const request = JSON.parse(String(message)) as { thread_ids: string[]; thread_keys: string[] }
          watched.clear()
          request.thread_ids.forEach(id => watched.add(`id:${id}`))
          request.thread_keys.forEach(key => watched.add(`key:${key}`))
        })
        return
      }
      const query = new URL(socket.url()).searchParams
      const target = query.get('thread_id') ?? query.get('thread_key') ?? 'user'
      const group = sockets.get(target) ?? new Set<WebSocketRoute>()
      sockets.set(target, group)
      group.add(socket)
      socket.onClose(() => group.delete(socket))
    })
    const send = (thread: ApiThread, ...events: WireEvent[]) => {
      for (const target of [thread.id, thread.channel_thread_id!]) {
        for (const socket of sockets.get(target) ?? []) for (const event of events) socket.send(JSON.stringify(event))
      }
      for (const key of [`id:${thread.id}`, `key:${thread.channel_thread_id}`].filter(key => watched.has(key))) {
        for (const event of events) {
          const heartbeat = event.event_kind === 'part_end'
          const status = heartbeat ? statuses.get(key) : event
          if (!status) continue
          statuses.set(key, status)
          activitySocket.send(JSON.stringify({ key, event: status, attention: status.event_kind === 'action_batch' }))
        }
      }
    }
    // Isolate every request from the developer's backend and database.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [first, second] })
      if (path === `/api/trunkline/threads/${first.id}`) return route.fulfill({ json: first })
      if (path === `/api/trunkline/threads/${second.id}`) return route.fulfill({ json: second })
      if (path.endsWith('/conversations/messages') || path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/project')) return route.fulfill({ json: null })
      if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/operations')) return route.fulfill({ json: operations })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: [] })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('requestfailed', request => errors.push(`${request.url()}: ${request.failure()?.errorText}`))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto(`/threads/${first.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle('Trunkline · Octomate')
    await expect(page).toHaveURL(new RegExp(`/threads/${first.id}$`))
    const activity = page.locator('#trk-chatlog .trk-activity')
    const status = activity.locator('[data-text-current]')
    const sidebar = page.locator('#trk-sb-panel')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    await expect(sidebar.getByRole('button', { name: /Background research/ })).toBeVisible()
    await expect.poll(() => watched.has(`id:${second.id}`)).toBe(true)
    send(second, { event_kind: 'run_activity_changed', activity: 'thinking', display_text: 'Pondering octopuses…' })
    const background = sidebar.locator(`[data-thread-activity="${second.id}"]`)
    await expect(background).toHaveText('Pondering octopuses')
    await sidebar.getByRole('button', { name: /Background research/ }).click()
    await expect(status).toHaveText('Pondering octopuses')
    if (width === 390) await page.keyboard.press('Escape')
    await expect(status).toHaveText('Pondering octopuses')
    send(second, { event_kind: 'run_activity_changed', activity: 'thinking', display_text: '<img src=x onerror=alert(1)>' })
    await expect(status).toHaveText('<img src=x onerror=alert(1)>')
    await expect(activity.locator('img')).toHaveCount(0)
    send(second, { event_kind: 'run_activity_changed', activity: 'thinking', display_text: '\u0000\u202e' })
    await expect(status).toHaveText('Thinking')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    await sidebar.getByRole('button', { name: /Status presentation/ }).click()
    if (width === 390) await page.keyboard.press('Escape')
    await expect.poll(() => sockets.get(first.id)?.size).toBe(1)
    await expect.poll(() => page.evaluate(async () => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      return useConsole.getState().detail?.sendKey
    })).toBe(first.channel_thread_id)
    const history: LedgerItem[] = [
      { kind: 'agent', uid: 'previous-reply', label: 'Octomate', blocks: [{ type: 'p', text: 'The previous reply.' }] },
      { kind: 'end', uid: 'previous-run', label: 'run complete · 1 request' },
      { kind: 'system', uid: 'existing-system', text: 'Session ready', tone: 'info' },
    ]
    await page.evaluate(async ledger => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.setState({ detail: { ...useConsole.getState().detail, ledger } })
    }, history)
    send(first, { event_kind: 'run_activity_changed', activity: 'working' })
    await expect(status).toHaveText('Working')
    expect(await activity.evaluate(el => el.parentElement?.previousElementSibling?.textContent)).toBe('Session ready')
    expect(await activity.evaluate(el => el.parentElement === el.parentElement?.parentElement?.lastElementChild)).toBe(true)
    await page.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-status/activity-start-${width}.png` })
    await page.clock.fastForward(120_001)
    await expect(activity).toHaveCount(0)
    const composer = page.getByPlaceholder('directive… or / for commands')
    await composer.fill('Check the status flow')
    await composer.press('Enter')
    await expect(status).toHaveText('relay · dispatching')
    await expect(activity).toHaveCount(1)
    send(first, { event_kind: 'reflex_awake' }, { event_kind: 'run_activity_changed', activity: 'thinking' })
    await expect(status).toHaveText('Thinking')
    await page.clock.fastForward(120_001)
    await expect(activity).toHaveCount(0)
    send(first, { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'The latest reply' } })
    await expect(status).toHaveText('Writing the response')
    send(first, { event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'text', content_delta: ' keeps growing.' } })
    await expect(page.locator('#trk-chatlog')).toContainText('The latest reply keeps growing.')
    expect(await activity.evaluate(el => el.parentElement === el.parentElement?.parentElement?.lastElementChild)).toBe(true)
    send(first, {
      event_kind: 'function_tool_call',
      part: { part_kind: 'tool-call', tool_call_id: 'status-tool', tool_name: 'codex_mcp_tool_call', args: {} },
    })
    await expect(status).toHaveText('Using tools')
    expect(await activity.evaluate(el => el.parentElement?.previousElementSibling?.textContent)).toContain('codex_mcp_tool_call')
    expect(await activity.evaluate(el => el.parentElement === el.parentElement?.parentElement?.lastElementChild)).toBe(true)
    await page.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-status/activity-tool-${width}.png` })
    const question: WireEvent = {
      event_kind: 'action_batch', batch_id: 'batch', blocks_run: true, response_mode: 'live', approvals: [],
      questions: [{ id: 'question', kind: 'question', status: 'pending', tool_name: 'ask', tool_call_id: 'call', position: 0, args: { question: 'Which environment?', choices: ['Staging', 'Production'] } }],
    }
    send(first, { event_kind: 'reflex_awake' }, question)
    await expect(status).toHaveText('Waiting for input')
    const option = page.locator('.lt-entry [aria-pressed]').filter({ hasText: 'Staging' }).first()
    await expect(option).toBeEnabled()
    await page.clock.fastForward(119_000)
    send(first, { event_kind: 'part_end', index: 0, part: { part_kind: 'text', content: '' } })
    await expect(status).toHaveText('Waiting for input')
    await page.clock.fastForward(119_000)
    await expect(status).toHaveText('Waiting for input')
    await page.clock.fastForward(1_001)
    await expect(status).toHaveCount(0)
    await expect(option.first()).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Teleport', exact: true })).toHaveAttribute('aria-disabled', 'true')
    await expect(page.getByRole('button', { name: 'Teleport', exact: true })).toHaveAttribute('aria-description', 'Wait for the current run to finish.')
    send(first, { ...question, batch_id: 'async-batch', blocks_run: false, questions: [
      { ...question.questions[0], id: 'async-question', args: { question: 'Include a progress report?', choices: ['Yes', 'No'] } },
    ] })
    await expect(status).toHaveText('Working · Input requested')
    send(first, { event_kind: 'run_activity_changed', activity: 'compacting' })
    await expect(status).toHaveText('Compacting context')
    await expect(activity).toBeVisible()
    await expect(page.locator('#trk-chathead [data-thread-activity]')).toHaveCount(0)
    await expect(activity).toHaveCount(1)
    await expect(page.locator('.trk-composer-activity')).toHaveCount(0)
    expect(await activity.evaluate(el => el.parentElement === el.parentElement?.parentElement?.lastElementChild)).toBe(true)
    const dots = activity.locator('.lt-dots i')
    const shimmer = activity.locator('.lt-text-swap')
    await expect(dots).toHaveCount(3)
    expect((await activity.locator('.lt-dots').boundingBox())!.x).toBeLessThan((await status.boundingBox())!.x)
    await expect(dots.first()).toHaveCSS('animation-name', 'none')
    await expect(shimmer).toHaveCSS('animation-name', 'none')
    await expect(shimmer).not.toHaveCSS('color', 'rgba(0, 0, 0, 0)')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await expect(dots.first()).toHaveCSS('animation-name', 'lt-dot-pulse')
    await expect(shimmer).toHaveCSS('animation-name', 'trk-text-shimmer')
    const initialPosition = await shimmer.evaluate(el => getComputedStyle(el).backgroundPositionX)
    await expect.poll(() => shimmer.evaluate(el => getComputedStyle(el).backgroundPositionX)).not.toBe(initialPosition)
    await expect.poll(() => dots.first().evaluate(el => el.getAnimations()[0]?.playState)).toBe('running')
    const initialOpacity = await dots.first().evaluate(el => getComputedStyle(el).opacity)
    await expect.poll(() => dots.first().evaluate(el => getComputedStyle(el).opacity)).not.toBe(initialOpacity)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await expect(dots.first()).toHaveCSS('animation-name', 'none')
    await expect(shimmer).toHaveCSS('animation-name', 'none')
    const placement = await activity.evaluate(el => {
      const row = el.parentElement!
      const message = row.previousElementSibling!.getBoundingClientRect()
      const status = row.getBoundingClientRect()
      return { gap: status.top - message.bottom, before: row.previousElementSibling?.textContent, last: row === row.parentElement?.lastElementChild }
    })
    expect(placement.gap).toBeGreaterThanOrEqual(0)
    expect(Math.round(placement.gap)).toBeLessThanOrEqual(14)
    expect(placement.before).toContain('Include a progress report?')
    expect(placement.last).toBe(true)
    await page.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-status/activity-tail-${width}.png` })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const motion = page.locator('.trk-activity-motion')
    await motion.evaluate(el => {
      const observer = new MutationObserver(() => el.getAnimations().forEach(animation => animation.pause()))
      observer.observe(el, { attributes: true, attributeFilter: ['data-exiting'] })
    })
    send(first, { event_kind: 'run_ended', outcome: 'deferred' })
    await expect(motion).toHaveAttribute('data-exiting', '')
    await expect(motion).toHaveAttribute('aria-hidden', 'true')
    expect(await motion.evaluate(el => {
      const animation = el.getAnimations()[0]
      animation.currentTime = 75
      return { name: getComputedStyle(el).animationName, duration: animation.effect!.getTiming().duration, opacity: Number(getComputedStyle(el).opacity) }
    })).toMatchObject({ name: 'lt-fade-out', duration: 150 })
    const opacity = await motion.evaluate(el => Number(getComputedStyle(el).opacity))
    expect(opacity).toBeGreaterThan(0)
    expect(opacity).toBeLessThan(1)
    // A fresh event interrupts the exit instead of being removed by its completion.
    send(first, { event_kind: 'run_activity_changed', activity: 'thinking' })
    await expect(status).toHaveText('Thinking')
    await expect(motion).not.toHaveAttribute('data-exiting')
    expect(await motion.evaluate(el => ({ name: getComputedStyle(el).animationName, duration: el.getAnimations()[0].effect!.getTiming().duration }))).toEqual({ name: 'lt-fade-up', duration: 240 })
    await motion.evaluate(el => el.getAnimations().forEach(animation => animation.finish()))
    await expect(activity).toHaveCount(1)
    send(first, { event_kind: 'reflex_asleep' })
    await expect(motion).toHaveAttribute('data-exiting', '')
    await motion.evaluate(el => el.getAnimations().forEach(animation => animation.finish()))
    await expect(status).toHaveCount(0)
    await expect(option).toBeEnabled()
    const submitted = page.waitForRequest(request => request.url().endsWith('/batches/batch/resolve') && request.method() === 'POST')
    await option.click()
    expect((await submitted).postDataJSON().answers).toEqual({ question: 'Staging' })
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
  })
}
