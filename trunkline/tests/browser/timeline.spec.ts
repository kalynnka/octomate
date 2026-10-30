import { test, expect } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread, ApiThreadMessage } from '../../src/lib/api/events'

for (const width of [1280, 390]) {
  test(`timeline previews show readable Markdown and reasoning labels at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const timestamp = '2026-10-31T02:00:00Z'
    const thread: ApiThread = {
      id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Timeline previews',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const conversation: ApiConversation = {
      id: 'conversation', external_id: null, thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'run', kind: 'octomate', conversation_id: 'conversation', name: null, model_name: null,
        permission_mode: null, cwd: '/work/chat/thread-a', parent_run_id: null, parent_tool_call_id: null, started_at: timestamp }],
    }
    const prompt: ApiThreadMessage = {
      kind: 'message', id: 'prompt', thread_id: thread.id, platform_message_id: null, reply_id: '',
      happened_at: timestamp, direction: 'inbound', actor_kind: 'human', agent_tentacle_id: null,
      sender: null, segments: [], created_at: timestamp, message_text: '**Check** the `workspace`.',
    }
    const response: ApiModelMessage = {
      id: 'reply', run_id: 'run', kind: 'response', timestamp, thread_messages: [],
      parts: [
        { part_kind: 'text', content: '**Checking** the [workspace](https://example.com).' },
        { part_kind: 'thinking', content: '**Trace** the boundary.\n\n- Inspect ownership.\n- Check permissions.' },
        { part_kind: 'thinking', content: '**Preparing binary-search example**', provider_details: { reasoning_format: 'summary' } },
        { part_kind: 'thinking', content: '', provider_details: { reasoning_format: 'summary' } },
        { part_kind: 'text', content: 'Use `a ** b` and \\*literal\\*.\n\n```text\nx ** y\n```\n\nRead the [guide](https://example.com) &amp; continue.' },
      ],
    }
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    // Every API request stays in the fixture, away from the developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/conversations')) return route.fulfill({ json: [conversation] })
      if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [response], total: 1, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [prompt], total: 1, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/thread-a/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
        '/api/trunkline/threads/thread-a/batches'].includes(path)) return route.fulfill({ json: [] })
      return route.fulfill({ status: 404, json: { detail: `Unmocked test API: ${path}` } })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/threads/thread-a')
    await expect(page).toHaveTitle('Trunkline · Octomate')
    await expect(page.locator('[id="pm-reply.2"]')).toBeAttached()
    const panel = page.locator('#trk-trace-panel')
    if (await panel.getAttribute('data-folded') !== null) await page.getByTitle('Timeline panel', { exact: true }).click()
    await expect(panel).not.toHaveAttribute('data-folded')
    const timeline = page.locator('#trk-timeline')
    await expect(timeline.locator('[data-uid="prompt"]')).toContainText('Check the workspace.')
    await expect(timeline.locator('[data-uid="reply.0"]')).toContainText('Checking the workspace.')
    const reasoning = timeline.locator('[data-uid="reply.1"]')
    await expect(reasoning).toContainText('Trace the boundary. Inspect ownership. Check permissions.')
    await expect(reasoning).not.toContainText('Summary')
    await expect(reasoning).not.toContainText('Unavailable')
    const summary = timeline.locator('[data-uid="reply.2"]')
    await expect(summary).toContainText('thinking · Summary')
    await expect(summary).toContainText('Preparing binary-search example')
    await expect(summary).not.toContainText('**')
    await expect(summary).not.toContainText('Summary ·')
    const unavailable = timeline.locator('[data-uid="reply.3"]')
    await expect(unavailable).toContainText('thinking · Unavailable')
    await expect(unavailable).not.toContainText('Summary')
    await expect(timeline.locator('[data-uid="reply.4"]')).toContainText('Use a ** b and *literal*. x ** y Read the guide & continue.')
    await summary.click()
    if (width === 390) await page.locator('.trk-scrim').click({ position: { x: 80, y: 400 } })
    const card = page.locator('[id="pm-reply.2"]')
    await expect(card).toHaveClass(/trk-flash/)
    await expect(card).toBeInViewport()
    await card.getByRole('button').click()
    await expect(card.locator('strong')).toHaveText('Preparing binary-search example')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/inky-timeline-${width}.png` })
  })
}
