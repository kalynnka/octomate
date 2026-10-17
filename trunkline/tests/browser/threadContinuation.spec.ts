import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiThread, ApiThreadMessage, WireEvent } from '../../src/lib/api/events'

const timestamp = '2026-10-17T08:00:00Z'
const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Continued conversation',
  project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
}
const messages: ApiThreadMessage[] = Array.from({ length: 40 }, (_, index) => ({
  kind: 'message', id: `saved-${String(index).padStart(2, '0')}`, thread_id: thread.id, platform_message_id: null,
  reply_id: '', happened_at: timestamp, direction: 'inbound', actor_kind: 'human',
  agent_tentacle_id: null, sender: null, segments: [], created_at: timestamp,
  message_text: `Earlier message ${index}. ${'This message stays in the conversation. '.repeat(10)}`,
}))

for (const { label, readingHistory, prompt } of [
  { label: 'at the bottom', readingHistory: false, prompt: 'Continue this conversation' },
  { label: 'while reading history', readingHistory: true, prompt: 'Continue this conversation' },
  { label: 'with a tall prompt', readingHistory: false, prompt: 'Continue this conversation.\n'.repeat(30) },
]) {
  test(`continuing from the composer preserves the view ${label}`, async ({ page }) => {
    let stream: WebSocketRoute | undefined
    let historyReads = 0
    let turns = 0
    await page.routeWebSocket(/\/api\/trunkline\/live/, (socket) => {
      if (new URL(socket.url()).searchParams.has('thread_key')) stream = socket
    })
    // No request from this fixture reaches the developer's backend.
    await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
      const path = new URL(route.request().url()).pathname
      if (route.request().method() === 'POST' && path.endsWith('/thread-key/messages')) {
        const reply = ++turns === 1 ? 'The first completed reply.' : 'The next reply.'
        const events: WireEvent[] = [
          { event_kind: 'reflex_awake' },
          { event_kind: 'run_started', address: {
            channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner',
            user_id: 'owner', channel_thread_id: 'thread-key', shared: false,
          } },
          { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: reply } },
        ]
        for (const event of events) stream!.send(JSON.stringify(event))
        return route.fulfill({ status: 202 })
      }
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/messages')) {
        historyReads++
        return route.fulfill({ json: { items: [...messages].reverse(), total: messages.length, has_more: false, next_cursor: null } })
      }
      if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/thread-a/usage')) return route.fulfill({ json: {} })
      if (path === '/api/trunkline/health') return route.fulfill({ json: { ok: true } })
      if (path === '/api/trunkline/permissions') return route.fulfill({ json: {} })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
        '/api/trunkline/threads/thread-a/conversations', '/api/trunkline/threads/thread-a/batches'].includes(path)) {
        return route.fulfill({ json: [] })
      }
      return route.fulfill({ status: 404, json: { detail: 'Unmocked test API' } })
    })
    await page.goto('/threads/thread-a')
    const log = page.locator('#trk-chatlog')
    const last = page.locator('#pm-saved-39')
    await expect(last).toBeVisible()
    const composer = page.getByPlaceholder('directive… or / for commands')
    await composer.fill('First prompt')
    await composer.press('Enter')
    const previousReply = log.getByText('The first completed reply.', { exact: true })
    await expect(previousReply).toBeVisible()
    stream!.send(JSON.stringify({ event_kind: 'run_result', output: 'The first completed reply.', usage: {
      requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0,
    } } satisfies WireEvent))
    stream!.send(JSON.stringify({ event_kind: 'reflex_asleep' } satisfies WireEvent))
    await expect(log.locator('.trk-print-caret')).toHaveCount(0)
    await expect.poll(() => log.evaluate(el => el.getAnimations().every(animation => animation.playState === 'finished'))).toBe(true)
    const original = await last.elementHandle()
    const previousReplyElement = await previousReply.elementHandle()
    await previousReply.evaluate(el => {
      el.dataset.changed = 'false'
      new MutationObserver(() => { el.dataset.changed = 'true' }).observe(el, { childList: true, characterData: true, subtree: true })
    })
    const reads = historyReads
    await log.evaluate(el => {
      el.dataset.replayed = 'false'
      el.addEventListener('animationstart', event => {
        if (event.target === el) el.dataset.replayed = 'true'
      })
    })
    await composer.fill(prompt)
    const position = await log.evaluate((el, reading) => {
      el.scrollTop = el.scrollHeight - el.clientHeight - (reading ? 400 : 0)
      return el.scrollTop
    }, readingHistory)
    await composer.press('Enter')
    await expect(log).toContainText('The next reply.')
    await expect(log).toContainText(prompt.trim())
    stream!.send(JSON.stringify({ event_kind: 'run_result', output: 'The next reply.', usage: {
      requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0,
    } } satisfies WireEvent))
    stream!.send(JSON.stringify({ event_kind: 'reflex_asleep' } satisfies WireEvent))
    await expect(log.locator('.trk-print-caret')).toHaveCount(0)
    await expect.poll(() => log.evaluate(el => el.getAnimations({ subtree: true })
      .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
      .every(animation => animation.playState === 'finished'))).toBe(true)
    expect(await last.evaluate((current, previous) => current === previous, original)).toBe(true)
    expect(await previousReply.evaluate((current, previous) => current === previous, previousReplyElement)).toBe(true)
    await expect(previousReply).toHaveAttribute('data-changed', 'false')
    await expect(log).toHaveAttribute('data-replayed', 'false')
    expect(historyReads).toBe(reads)
    if (readingHistory) expect(await log.evaluate(el => el.scrollTop)).toBeCloseTo(position, 0)
    else await expect.poll(() => log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2)
  })
}
