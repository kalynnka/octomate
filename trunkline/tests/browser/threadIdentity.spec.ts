import { test, expect } from '@playwright/test'
import type { ApiThread, ThreadOperations } from '../../src/lib/api/events'

for (const width of [1280, 390]) {
  test(`thread keys stay stable while detail loads and switches are interrupted at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const timestamp = '2026-10-31T02:00:00Z'
    const first: ApiThread = {
      id: '01a18bed-47d7-75d0-b346-54d52be38615', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'trunkline-first', title: 'First thread',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const second: ApiThread = { ...first, id: '01a188dd-9bd4-7252-ab06-dfc69e7263a9', channel_thread_id: 'trunkline-second', title: 'Second thread' }
    const operations: ThreadOperations = {
      source: null, barred: {},
      teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
      summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
    }
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    // Every API request stays in the fixture, away from the developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [first, second] })
      if (path === `/api/trunkline/threads/${first.id}`) return route.fulfill({ json: first })
      if (path === `/api/trunkline/threads/${second.id}`) {
        await pending
        return route.fulfill({ json: second })
      }
      if (path.endsWith('/conversations/messages') || path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/project')) return route.fulfill({ json: null })
      if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/operations')) return route.fulfill({ json: operations })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: [] })
    })
    await page.addInitScript(() => {
      const reads: string[] = []
      new MutationObserver(() => {
        const value = document.querySelector('[data-testid="timeline-thread-key"] [data-text-current]')?.textContent ?? ''
        if (reads.at(-1) !== value) reads.push(value)
        document.documentElement.dataset.threadKeyReads = JSON.stringify(reads)
      }).observe(document, { subtree: true, childList: true, characterData: true })
    })
    await page.goto(`/threads/${first.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle('Trunkline · Octomate')
    const key = page.getByTestId('timeline-thread-key').locator('[data-text-current]')
    await expect(key).toHaveText('trunkline-first')
    await expect(page.getByRole('button', { name: 'Copy thread ID', exact: true })).toBeEnabled()
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    const sidebar = page.locator('#trk-sb-panel')
    await sidebar.getByRole('button', { name: /Second thread/ }).click()
    if (width === 390) await page.keyboard.press('Escape')
    await expect(key).toHaveText('trunkline-second')
    await expect(page.getByRole('button', { name: 'Copy thread ID', exact: true })).toBeDisabled()
    await expect(page.locator('#trk-chathead')).not.toContainText(second.id)
    await expect(page.locator('[title="owning route"] [data-text-current]')).toHaveText('trunkline/trunkline-second')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    await sidebar.getByRole('button', { name: /First thread/ }).click()
    if (width === 390) await page.keyboard.press('Escape')
    await expect(key).toHaveText('trunkline-first')
    const late = page.waitForResponse(response => new URL(response.url()).pathname === `/api/trunkline/threads/${second.id}`)
    release()
    await late
    await expect(key).toHaveText('trunkline-first')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    await sidebar.getByRole('button', { name: /Second thread/ }).click()
    if (width === 390) await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: 'Copy thread ID', exact: true })).toBeEnabled()
    await expect(key).toHaveText('trunkline-second')
    await expect(page.getByTestId('timeline-thread-key').locator('.lt-text-old')).toHaveCount(0)
    const reads = JSON.parse(await page.locator('html').getAttribute('data-thread-key-reads') ?? '[]') as string[]
    expect(reads).toContain('trunkline-first')
    expect(reads).toContain('trunkline-second')
    expect(reads).not.toContain(first.id)
    expect(reads).not.toContain(second.id)
    const toggle = page.getByRole('button', { name: 'Timeline panel', exact: true })
    if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click()
    const fullKey = page.locator('#trk-trace-panel > div:nth-child(2) [data-text-current]').first()
    await expect(fullKey).toHaveText('trunkline-second')
    expect(await fullKey.evaluate(el => el.getBoundingClientRect().width)).toBeGreaterThan(20)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/inky-thread-identity-${width}.png` })
  })
}
