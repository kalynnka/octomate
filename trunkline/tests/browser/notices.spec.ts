import { test, expect } from '@playwright/test'
import type { LedgerItem } from '../../src/lib/api/types'
import type { ApiThread } from '../../src/lib/api/events'

const timestamp = '2026-11-24T01:00:00Z'
const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Notice arrival order',
  project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
}
const earlier: LedgerItem = { kind: 'user', uid: 'earlier', who: 'operator', t: '', text: 'Check the environment.' }
const reply: LedgerItem = { kind: 'stream', uid: 'reply', text: 'The environment check completed.', streaming: false }
const later: LedgerItem = { kind: 'user', uid: 'later', who: 'operator', t: '', text: 'Continue with the next check.' }
const error = 'the answer did not go through — A turn is still running in this thread.'

for (const width of [1280, 390]) {
  test(`local errors stay where they arrived at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path.endsWith('/threads/thread-a')) return route.fulfill({ json: thread })
      if (path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/project')) return route.fulfill({ json: null })
      if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/operations')) return route.fulfill({ json: { source: null, barred: {}, teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' }, summon: { destinations: [], here: null, routes: {}, reason: 'No agents' } } })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: [] })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto('/threads/thread-a')
    await expect(page).toHaveTitle('Trunkline · Octomate')
    await expect.poll(() => page.evaluate(async () => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      return useConsole.getState().detail?.sendKey
    })).toBe('thread-key')
    await page.evaluate(async ({ earlier, reply, error }) => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.setState({ detail: { ...useConsole.getState().detail, ledger: [earlier] }, live: [reply] })
      useConsole.getState().actions.reportThreadError('thread-a', error)
    }, { earlier, reply, error })
    const log = page.locator('#trk-chatlog')
    await expect(log.getByText(error)).toBeVisible()
    await page.evaluate(async later => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.setState({ live: [...useConsole.getState().live, later] })
    }, later)
    await expect(log.getByText(later.text, { exact: true })).toBeVisible()
    const order = () => log.evaluate(element => {
      const text = element.textContent ?? ''
      return [text.indexOf('The environment check completed.'), text.indexOf('the answer did not go through'), text.indexOf('Continue with the next check.')]
    })
    const positions = await order()
    expect(positions[0]).toBeLessThan(positions[1])
    expect(positions[1]).toBeLessThan(positions[2])
    await page.evaluate(async ledger => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.setState({ detail: { ...useConsole.getState().detail, ledger }, live: [], ledgerN: 1 })
    }, [earlier, { ...reply, uid: 'saved-reply' }, later])
    await expect(log.getByText(error)).toBeVisible()
    await expect(log.getByText(reply.text, { exact: true })).toHaveCount(0)
    await page.evaluate(async () => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.getState().actions.loadOlder()
    })
    await expect(log.getByText(reply.text, { exact: true })).toBeVisible()
    expect(await order()).toEqual(positions)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    expect(await log.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    await page.screenshot({ path: `/private/tmp/trunkline-notice-order-${width}.png` })
  })
}
