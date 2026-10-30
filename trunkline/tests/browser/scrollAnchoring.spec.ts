import { test, expect } from '@playwright/test'
import type { ApiModelMessage, ApiThread, ApiThreadMessage } from '../../src/lib/api/events'

const timestamp = '2026-10-31T01:00:00Z'
const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Late diagram layout',
  project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
}
const messages: ApiThreadMessage[] = Array.from({ length: 40 }, (_, index) => ({
  kind: 'message', id: `history-${String(index).padStart(2, '0')}`, thread_id: thread.id, platform_message_id: null,
  reply_id: '', happened_at: timestamp, direction: 'inbound', actor_kind: 'human',
  agent_tentacle_id: null, sender: null, segments: [], created_at: timestamp,
  message_text: `Earlier message ${index}. ${'This message stays in the conversation. '.repeat(6)}`,
}))
const response: ApiModelMessage = {
  id: 'reply', run_id: 'run', kind: 'response', timestamp, thread_messages: [],
  parts: [{ part_kind: 'text', content: 'A diagram that renders after history loads.\n\n```mermaid\nflowchart TD\nStart --> Read --> Think --> Build --> Test --> Finish\n```\n\nEnd of latest answer.' }],
}

for (const width of [1280, 390]) {
  for (const readingHistory of [false, true]) {
    test(`${readingHistory ? 'keeps the reader in history' : 'follows the latest answer'} after delayed diagram layout at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      let renderDiagrams!: () => void
      const diagramsReady = new Promise<void>(resolve => { renderDiagrams = resolve })
      await page.route(url => url.pathname.includes('/pele'), async route => {
        await diagramsReady
        await route.continue()
      })
      await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
      // Every API read stays in the fixture, away from the developer's backend.
      await page.route(url => url.pathname.startsWith('/api/'), async route => {
        const path = new URL(route.request().url()).pathname
        if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
        if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
        if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
        if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
        if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [response], total: 1, has_more: false, next_cursor: null } })
        if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [...messages].reverse(), total: messages.length, has_more: false, next_cursor: null } })
        if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
        if (path.endsWith('/thread-a/usage')) return route.fulfill({ json: {} })
        if (path === '/api/trunkline/health') return route.fulfill({ json: { ok: true } })
        if (path === '/api/trunkline/permissions') return route.fulfill({ json: {} })
        if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
          '/api/trunkline/threads/thread-a/conversations', '/api/trunkline/threads/thread-a/batches'].includes(path)) return route.fulfill({ json: [] })
        return route.fulfill({ status: 404, json: { detail: 'Unmocked test API' } })
      })
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.goto('/threads/thread-a', { waitUntil: 'domcontentloaded' })
      await expect(page).toHaveTitle('Trunkline · Octomate')
      const log = page.locator('#trk-chatlog')
      await expect(log.getByText('End of latest answer.', { exact: true })).toBeVisible()
      await page.evaluate(async () => { await document.fonts.ready })
      await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
      await expect(log.locator('svg.pele')).toHaveCount(0)
      const height = await log.evaluate(el => el.scrollHeight)
      if (readingHistory) {
        await log.evaluate(el => { el.scrollTop -= 500 })
        await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeGreaterThan(450)
      }
      const top = await log.evaluate(el => new Promise<number>(resolve => requestAnimationFrame(() => resolve(el.scrollTop))))
      renderDiagrams()
      await expect(log.locator('svg.pele')).toBeVisible()
      await expect.poll(() => log.evaluate(el => el.scrollHeight)).toBeGreaterThan(height + 100)
      if (readingHistory) {
        expect(await log.evaluate(el => el.scrollTop)).toBeCloseTo(top, 0)
      } else {
        await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
        await expect(log.getByText('End of latest answer.', { exact: true })).toBeInViewport()
      }
      await page.setViewportSize({ width, height: 700 })
      if (readingHistory) {
        expect(await log.evaluate(el => el.scrollTop)).toBeCloseTo(top, 0)
      } else {
        await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
      }
      await expect(page.locator('vite-error-overlay')).toHaveCount(0)
      expect(errors).toEqual([])
    })
  }
}
