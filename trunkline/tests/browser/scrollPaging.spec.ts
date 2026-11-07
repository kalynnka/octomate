import { test, expect } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread } from '../../src/lib/api/events'

for (const width of [1280, 390]) {
  test(`older pages preserve scrolling before the top edge at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const timestamp = '2026-10-31T02:00:00Z'
    const thread: ApiThread = {
      id: 'thread-a', kind: 'native_thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'codex', channel_thread_id: '', title: 'Scrolling through history',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const conversation: ApiConversation = {
      id: 'conversation', external_id: 'native-session', thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'run', kind: 'external', conversation_id: 'conversation', name: null, model_name: null,
        permission_mode: null, cwd: '/work', parent_run_id: null, parent_tool_call_id: null, started_at: timestamp }],
    }
    const messages: ApiModelMessage[] = Array.from({ length: 120 }, (_, i) => ({
      id: `history-${String(i).padStart(3, '0')}`, run_id: 'run', kind: 'response', timestamp, thread_messages: [],
      parts: [{ part_kind: 'text', content: `Message ${i}. ${'Reading earlier history. '.repeat(8)}` }],
    }))
    let releaseOlder!: () => void
    const olderReady = new Promise<void>(resolve => { releaseOlder = resolve })
    const cursors: string[] = []
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/conversations')) return route.fulfill({ json: [conversation] })
      if (path.endsWith('/thread-a/conversations/messages')) {
        const cursor = url.searchParams.get('cursor') ?? '120'
        cursors.push(cursor)
        if (cursor === '40') await olderReady
        const end = Number(cursor)
        return route.fulfill({ json: { items: messages.slice(end - 40, end).reverse(), total: 120, has_more: end > 40, next_cursor: String(end - 40) } })
      }
      if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: '' } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/thread-a/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
        '/api/trunkline/threads/thread-a/batches'].includes(path)) return route.fulfill({ json: [] })
      return route.fulfill({ status: 404, json: { detail: `Unmocked test API: ${path}` } })
    })
    await page.goto('/threads/thread-a')
    await expect(page).toHaveTitle('Trunkline · Octomate')
    const log = page.locator('#trk-chatlog')
    await expect(log.locator('.trk-agent-message')).toHaveCount(40)
    await expect.poll(() => cursors.filter(cursor => cursor !== '120')).toEqual(['80', '40'])
    await page.evaluate(async () => {
      await document.fonts.ready
      await Promise.allSettled(document.getAnimations().filter(a => a.effect?.getComputedTiming().iterations !== Infinity).map(a => a.finished))
    })
    // Still a viewport from the boundary: cached history should be inserted
    // now, with the same message staying under the reader's eye.
    const anchor = await log.evaluate(el => {
      el.scrollTop = el.clientHeight
      const row = [...el.querySelectorAll<HTMLElement>('.trk-agent-message')].find(row => row.getBoundingClientRect().top > el.getBoundingClientRect().top)!
      return { id: row.id, top: row.getBoundingClientRect().top }
    })
    await expect(log.locator('.trk-agent-message')).toHaveCount(80)
    await expect.poll(async () => Math.abs(await page.locator(`[id="${anchor.id}"]`).evaluate(el => el.getBoundingClientRect().top) - anchor.top)).toBeLessThan(2)

    // The next page arrives late while the reader continues to move. Restoring
    // a top saved before the request must not rewind that movement.
    const waiting = await log.evaluate(el => {
      el.scrollTop = el.clientHeight
      const row = [...el.querySelectorAll<HTMLElement>('.trk-agent-message')].find(row => row.getBoundingClientRect().top > el.getBoundingClientRect().top)!
      return { id: row.id, top: row.getBoundingClientRect().top }
    })
    await log.evaluate(el => { el.scrollTop -= 75 })
    const expected = await page.locator(`[id="${waiting.id}"]`).evaluate(el => el.getBoundingClientRect().top)
    const arrival = page.locator(`[id="${waiting.id}"]`).evaluate(el => new Promise<number[]>(resolve => {
      const samples: number[] = []
      const start = performance.now()
      const sample = () => {
        samples.push(el.getBoundingClientRect().top)
        if (performance.now() - start < 600) requestAnimationFrame(sample)
        else resolve(samples)
      }
      sample()
    }))
    releaseOlder()
    await expect(log.locator('.trk-agent-message')).toHaveCount(120)
    expect(Math.max(...(await arrival).map(top => Math.abs(top - expected)))).toBeLessThan(2)
    expect(expected).toBeGreaterThan(waiting.top)
    expect(cursors.filter(cursor => cursor !== '120')).toEqual(['80', '40'])

    // Repeat with the timeline driving an unfinished chat animation. Prepending
    // history must rebase that animation rather than cancel it or reverse it.
    await page.reload()
    await expect(log.locator('.trk-agent-message')).toHaveCount(40)
    const toggle = page.locator('#trk-chathead [aria-controls="trk-trace-panel"]')
    if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click()
    await page.evaluate(async () => {
      await document.fonts.ready
      await Promise.allSettled(document.getAnimations().filter(a => a.effect?.getComputedTiming().iterations !== Infinity).map(a => a.finished))
    })
    const moving = await page.evaluate(async () => {
      const log = document.getElementById('trk-chatlog')!
      const rail = document.getElementById('trk-timeline')!
      const anchor = log.querySelector('.trk-agent-message')!
      const samples = [{ top: anchor.getBoundingClientRect().top, count: log.querySelectorAll('.trk-agent-message').length }]
      rail.scrollTop = 120
      const start = performance.now()
      await new Promise<void>(resolve => {
        const sample = () => {
          samples.push({ top: anchor.getBoundingClientRect().top, count: log.querySelectorAll('.trk-agent-message').length })
          if (performance.now() - start < 1200) requestAnimationFrame(sample)
          else resolve()
        }
        requestAnimationFrame(sample)
      })
      return samples
    })
    expect(moving.at(-1)!.count).toBeGreaterThan(40)
    const distance = moving.at(-1)!.top - moving[0]!.top
    expect(distance).toBeGreaterThan(100)
    expect(new Set(moving.map(sample => Math.round(sample.top))).size).toBeGreaterThan(10)
    for (let i = 1; i < moving.length; i++) {
      const step = moving[i]!.top - moving[i - 1]!.top
      expect(step).toBeGreaterThan(-2)
      expect(step).toBeLessThan(distance / 3)
    }
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/inky-scroll-paging-${width}.png` })
  })
}
