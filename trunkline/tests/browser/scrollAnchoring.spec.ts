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
  for (const mode of ['latest', 'history', 'expand', 'expand-fit', 'expand-bottom'] as const) {
    const readingHistory = mode === 'history'
    const expanding = mode.startsWith('expand')
    test(`${expanding ? mode : readingHistory ? 'keeps the reader in history' : 'follows the latest answer'} after delayed layout at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      if (mode === 'expand-bottom') await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
      const current: ApiModelMessage = expanding ? { ...response, parts: [
        { part_kind: 'builtin-tool-call', tool_name: 'anchor_read', tool_call_id: 'anchor-read', args: { path: 'output.log' } },
        { part_kind: 'builtin-tool-return', tool_name: 'anchor_read', tool_call_id: 'anchor-read', content: 'Output line.\n'.repeat(mode === 'expand' ? 80 : 4) },
        ...response.parts,
      ] } : response
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
        if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [current], total: 1, has_more: false, next_cursor: null } })
        if (path.endsWith('/thread-a/messages')) {
          const history = expanding && mode !== 'expand-bottom' ? messages.slice(-1) : messages
          return route.fulfill({ json: { items: [...history].reverse(), total: history.length, has_more: false, next_cursor: null } })
        }
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
      if (expanding) {
        const group = log.locator('details.trk-trace-group')
        const summary = group.locator('summary')
        const summaryBefore = (await summary.boundingBox())!.y
        await summary.click()
        await expect(group).toHaveAttribute('open', '')
        expect((await summary.boundingBox())!.y).toBeCloseTo(summaryBefore, 0)
        const button = log.locator('.trk-tool-toggle')
        if (mode === 'expand-bottom') await button.evaluate(el => {
          const log = document.getElementById('trk-chatlog')!
          const viewport = log.getBoundingClientRect()
          log.scrollTop += (el.getBoundingClientRect().top - viewport.bottom + 50) / (viewport.height / log.clientHeight)
        })
        await expect(button).toBeInViewport()
        const answer = log.getByText('End of latest answer.', { exact: true })
        const answerBefore = (await answer.boundingBox())!.y
        if (mode !== 'expand-bottom') expect(await log.evaluate(el => el.scrollHeight <= el.clientHeight)).toBe(true)
        const column = log.locator('.trk-ledger-col')
        const panelBefore = (await log.boundingBox())!
        const columnBefore = (await column.boundingBox())!
        await log.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-reading-${width}-${mode}-folded.png` })
        const positions = button.evaluate(el => new Promise<{ before: number; samples: number[] }>(resolve => {
          el.addEventListener('pointerdown', () => {
            const before = el.getBoundingClientRect().top
            const samples: number[] = []
            const start = performance.now()
            const sample = () => {
              samples.push(el.getBoundingClientRect().top)
              if (performance.now() - start < 700) requestAnimationFrame(sample)
              else resolve({ before, samples })
            }
            requestAnimationFrame(sample)
          }, { once: true })
        }))
        await button.click()
        await expect(button).toHaveAttribute('aria-expanded', 'true')
        const { before, samples } = await positions
        if (mode === 'expand-fit') expect(Math.max(...samples.map(top => Math.abs(top - before)))).toBeLessThan(2)
        else await expect.poll(() => button.evaluate((el, mode) => {
          const log = document.getElementById('trk-chatlog')!
          const viewport = log.getBoundingClientRect()
          const card = el.closest('.trk-trace-row')!.getBoundingClientRect()
          const margin = 8 * viewport.height / log.clientHeight
          return Math.abs(mode === 'expand' ? card.top - viewport.top - margin : card.bottom - viewport.bottom + margin)
        }, mode)).toBeLessThan(2)
        const columnAfter = (await column.boundingBox())!
        const panelAfter = (await log.boundingBox())!
        expect(columnAfter.x - panelAfter.x).toBeCloseTo(columnBefore.x - panelBefore.x, 0)
        expect(panelAfter.x + panelAfter.width - columnAfter.x - columnAfter.width)
          .toBeCloseTo(panelBefore.x + panelBefore.width - columnBefore.x - columnBefore.width, 0)
        if (mode !== 'expand-bottom') expect((await answer.boundingBox())!.y).toBeGreaterThan(answerBefore + 50)
        await log.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-reading-${width}-${mode}-expanded.png` })
        const top = await log.evaluate(el => el.scrollTop)
        renderDiagrams()
        await expect(log.locator('svg.pele')).toBeVisible()
        expect(Math.abs(await log.evaluate(el => el.scrollTop) - top)).toBeLessThan(2)
        if (mode !== 'expand-fit') {
          await log.evaluate(el => new Promise<void>(resolve => {
            el.addEventListener('scroll', () => requestAnimationFrame(() => resolve()), { once: true })
            el.scrollTop = el.scrollHeight
          }))
          await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
          await page.setViewportSize({ width, height: 700 })
          await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
        }
        await expect(page.locator('vite-error-overlay')).toHaveCount(0)
        expect(errors).toEqual([])
        return
      }
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
