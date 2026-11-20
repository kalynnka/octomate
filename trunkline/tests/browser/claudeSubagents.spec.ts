import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread, WireEvent } from '../../src/lib/api/events'

for (const theme of ['light', 'dark']) for (const kind of ['external', 'octomate'] as const) {
  test(`a Claude ${kind} child opens its own thread in ${theme} theme`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.setViewportSize({ width: kind === 'external' ? 1280 : 390, height: 900 })
    await page.addInitScript(theme => {
      localStorage.setItem('trk-theme', theme)
      Object.defineProperty(navigator, 'clipboard', { value: { writeText: async (text: string) => { sessionStorage.setItem('copied', text) } } })
    }, theme)
    const timestamp = '2026-11-18T00:00:00Z'
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const thread: ApiThread = {
      id: 'thread', kind: kind === 'external' ? 'native_thread' : 'thread', chat_type: 'thread',
      channel_tentacle_id: kind === 'external' ? 'claude' : 'trunkline', chat_id: 'session', channel_thread_id: kind === 'external' ? null : 'thread-key',
      title: 'Claude review', status: 'active', project_id: null, handoffs: [], created_at: timestamp, updated_at: timestamp,
    }
    const parent: ApiConversation = {
      id: 'parent', external_id: 'session', thread_id: 'thread', agent_tentacle_id: 'claude', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'turn', kind, conversation_id: 'parent', name: null, model_name: null,
        permission_mode: null, cwd: null, parent_run_id: null, parent_tool_call_id: null, started_at: null }],
    }
    const child = { ...parent, id: 'child', parent_conversation_id: parent.id, subagent_id: 'child-abc123', name: kind === 'external' ? null : 'Review the diff',
      runs: [{ ...parent.runs[0], id: 'child-turn', conversation_id: 'child', parent_run_id: 'turn', parent_tool_call_id: 'spawn' }] }
    const history: ApiModelMessage[] = [
      { id: '2', run_id: 'turn', kind: 'request', timestamp: null, thread_messages: [], parts: [{ part_kind: 'tool-return', tool_name: 'Agent', tool_call_id: 'spawn', content: 'Review complete' }] },
      { id: '1', run_id: 'turn', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'tool-call', tool_name: 'Agent', tool_call_id: 'spawn', args: { subagent_type: 'Explore', description: 'Review the diff', prompt: 'Check the ingestion changes and report any regressions.' } }, { part_kind: 'thinking', content: 'Inspect the changes.', id: null, signature: null }, { part_kind: 'tool-call', tool_name: 'Read', tool_call_id: 'read-parent', args: { file_path: 'ingest.py' } }, { part_kind: 'text', content: 'The review is ready.' }] },
    ]
    const childHistory: ApiModelMessage[] = [
      { id: '5', run_id: 'child-turn', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'text', content: 'Child-only finding: the ingestion changes preserve tool ownership.' }] },
      { id: '4', run_id: 'child-turn', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'tool-call', tool_name: 'Read', tool_call_id: 'read-child', args: { file_path: 'ingest.py' } }] },
      { id: '3', run_id: 'child-turn', kind: 'request', timestamp, thread_messages: [], parts: [{ part_kind: 'user-prompt', content: 'Inspect the ingestion changes.' }] },
    ]
    let saved = false
    const sockets = new Set<WebSocketRoute>()
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => {
      if (new URL(socket.url()).searchParams.get('thread_id') !== thread.id) return
      sockets.add(socket)
      socket.onClose(() => sockets.delete(socket))
    })
    await page.route(url => url.pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      const items = !saved ? [] : path.endsWith('/conversations/child/messages') ? childHistory : path.endsWith('/conversations/messages') ? history : []
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner' } })
      if (path.endsWith('/threads/thread')) return route.fulfill({ json: thread })
      if (path.endsWith('/threads')) return route.fulfill({ json: [thread] })
      if (path.endsWith('/conversations')) return route.fulfill({ json: saved ? [parent, child, { ...child, id: 'grandchild', parent_conversation_id: child.id, name: 'Check tool ownership', runs: [] }, { ...child, id: 'sibling', name: 'Review the tests', runs: [] }] : [parent] })
      if (path.endsWith('/messages')) return route.fulfill({ json: { items, total: items.length, has_more: false, next_cursor: null } })
      if (path.endsWith('/config')) return route.fulfill({ json: { path: '/artifacts' } })
      if (path.endsWith('/project')) return route.fulfill({ json: null })
      if (path.endsWith('/operations')) return route.fulfill({ json: { source: null, barred: {}, teleport: { destinations: [], routes: {} }, summon: { destinations: [], routes: {} } } })
      if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      return route.fulfill({ json: [] })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto('/threads/thread')
    await expect(page).toHaveTitle('Trunkline · Octomate')
    await expect.poll(() => sockets.size).toBeGreaterThan(0)
    const send = (event: WireEvent) => { for (const socket of sockets) socket.send(JSON.stringify(event)) }
    if (kind === 'octomate') send({ event_kind: 'reflex_awake' })
    send({ event_kind: 'run_started', address: { channel_tentacle_id: thread.channel_tentacle_id, chat_type: 'thread', chat_id: thread.chat_id, channel_thread_id: thread.channel_thread_id, user_id: 'owner', shared: false } })
    send({ event_kind: 'subagent_started', invocation_id: child.subagent_id, kind: 'subagent', name: 'Explore' })
    const row = page.locator('#trk-chatlog').locator('[id^="pm-"]').filter({ hasText: 'ABC123' })
    await expect(row).toContainText('Explore')
    await expect(row).toContainText('running')
    send({ event_kind: 'agent_message', message_id: 'handback', agent: { id: child.subagent_id, name: 'Explore' }, text: 'A subagent finding.' })
    const handbackLabel = page.locator('#trk-chatlog [data-agent-label]').filter({ hasText: /^Explore$/ })
    await expect(handbackLabel.locator('svg')).toBeVisible()
    if (kind === 'external') send({ event_kind: 'run_ended', outcome: 'completed' })
    send({ event_kind: 'subagent_settled', invocation_id: child.subagent_id, status: 'completed', response: 'Review complete' })
    await expect(row).toContainText('completed')
    saved = true
    await page.reload()
    await expect(row).toBeVisible()
    await expect(row).toContainText('Explore')
    await expect(row).toContainText('completed')
    await expect(row.locator('[aria-expanded]')).toHaveCount(0)
    const compactBox = await row.boundingBox()
    expect(compactBox!.height).toBeLessThan(kind === 'external' ? 34 : 52)
    const parentLabel = page.locator('#trk-chatlog [data-agent-label]').first()
    await expect(parentLabel).toContainText('Octomate')
    expect((await parentLabel.boundingBox())!.y).toBeLessThan(compactBox!.y)
    await expect(page.locator('#trk-chatlog [data-agent-label]')).toHaveCount(1)
    await page.locator('#trk-chatlog .trk-trace-summary').click()
    for (const selector of ['.trk-thinking-toggle', '.trk-tool-toggle:not(.trk-subagent-toggle)']) {
      const bar = page.locator('#trk-chatlog').locator(selector).first()
      await expect(bar).toBeVisible()
      const centers = await bar.evaluate(element => [...element.children]
        .filter(child => !child.classList.contains('trk-trace-hover-brackets') && child.getBoundingClientRect().height > 0)
        .map(child => { const box = child.getBoundingClientRect(); return box.y + box.height / 2 }))
      expect(Math.max(...centers) - Math.min(...centers)).toBeLessThan(1)
    }
    const taskCenters = await row.locator('.trk-subagent-toggle').evaluate(element => [...element.children]
      .filter(child => child.tagName === 'svg' || child.classList.contains('trk-subagent-title'))
      .map(child => { const box = child.getBoundingClientRect(); return box.y + box.height / 2 }))
    expect(Math.max(...taskCenters) - Math.min(...taskCenters)).toBeLessThan(1)
    await page.screenshot({ animations: 'disabled', path: `/tmp/inky-subagents-${theme}-${kind}.png` })
    const openThread = row.getByRole('button', { name: 'Open thread: Review the diff', exact: true })
    await openThread.focus()
    await expect(openThread).toBeFocused()
    await openThread.press('Enter')
    await expect(page).toHaveURL(/\/threads\/thread\/conversations\/child$/)
    await expect(page.locator('#trk-chatlog')).toContainText('Child-only finding')
    await expect(page.locator('#trk-chatlog')).toContainText('Inspect the ingestion changes.')
    await expect(page.locator('#trk-chatlog')).not.toContainText('Review complete')
    await expect(page.locator('#trk-chathead')).toContainText('Review the diff')
    const header = page.locator('#trk-chathead')
    const headerCenters = await header.evaluate(element => [...element.querySelectorAll(':scope > button, :scope > .trk-head-title, :scope > .trk-head-chip, :scope > span:not(.trk-head-title) > button')]
      .filter(child => child.getBoundingClientRect().height > 0)
      .map(child => { const box = child.getBoundingClientRect(); return box.y + box.height / 2 }))
    expect(Math.max(...headerCenters) - Math.min(...headerCenters)).toBeLessThan(1)
    const back = header.getByRole('button', { name: 'Back to parent thread', exact: true })
    await expect(back).toHaveCSS('border-top-width', '0px')
    const themeControl = header.getByRole('button', { name: /^Switch to/ })
    expect((await back.boundingBox())!.height).toBeCloseTo((await themeControl.boundingBox())!.height, 1)
    if (kind === 'external') {
      const titleCenters = await header.locator('.trk-head-title').evaluate(element => [...element.children]
        .map(child => { const box = child.getBoundingClientRect(); return box.y + box.height / 2 }))
      expect(Math.max(...titleCenters) - Math.min(...titleCenters)).toBeLessThan(1)
    }
    await page.getByRole('button', { name: 'Copy conversation ID', exact: true }).click()
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('copied'))).toBe('child')
    await expect(page.locator('.trk-conversation-footer')).toContainText('parent thread')
    await page.reload()
    await expect(page.locator('#trk-chatlog')).toContainText('Child-only finding')
    await page.screenshot({ animations: 'disabled', path: `/tmp/inky-child-${theme}-${kind}.png` })
    await page.getByRole('button', { name: 'Back to parent thread', exact: true }).click()
    await expect(page).toHaveURL(/\/threads\/thread$/)
    await page.getByRole('button', { name: 'Copy thread ID', exact: true }).click()
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('copied'))).toBe('thread')
    await expect(row).toBeVisible()
    await page.goBack()
    await expect(page.locator('#trk-chatlog')).toContainText('Child-only finding')
    await page.goForward()
    await expect(row).toBeVisible()
    if (kind === 'octomate') await page.getByRole('button', { name: 'Show channels', exact: true }).first().click()
    const nested = page.locator('[data-thread-id="child"]')
    await expect(nested).toContainText('Review the diff')
    await expect(nested).toContainText('ABC123')
    await expect(nested.locator('.trk-thread-tagline')).toContainText('claude')
    const parentBox = await page.locator('[data-thread-id="thread"]').boundingBox()
    const childBox = await nested.boundingBox()
    expect(childBox!.x).toBe(parentBox!.x)
    expect(childBox!.width).toBe(parentBox!.width)
    const parentTitle = await page.locator('[data-thread-id="thread"] .trk-thread-title').boundingBox()
    const childTitle = await nested.locator('.trk-thread-title').boundingBox()
    expect(childTitle!.x).toBeGreaterThan(parentTitle!.x)
    const grandchildTitle = await page.locator('[data-thread-id="grandchild"] .trk-thread-title').boundingBox()
    expect(grandchildTitle!.x - childTitle!.x).toBeCloseTo(childTitle!.x - parentTitle!.x, 3)
    const branches = page.locator('.trk-thread-branch')
    const lineOffset = (await branches.nth(0).locator('.trk-thread-connector').boundingBox())!.x - parentTitle!.x
    expect(lineOffset).toBeGreaterThan(3)
    expect(lineOffset).toBeLessThanOrEqual(4)
    expect((await branches.nth(1).locator('.trk-thread-connector[data-branch]').boundingBox())!.x - childTitle!.x).toBeCloseTo(lineOffset, 1)
    await expect(branches.nth(0).locator('.trk-thread-connector')).not.toHaveAttribute('data-last')
    await expect(branches.nth(1).locator('.trk-thread-connector')).toHaveCount(2)
    await expect(branches.nth(2).locator('.trk-thread-connector')).toHaveAttribute('data-last', '')
    await expect(branches.nth(0).locator('.trk-thread-connector')).toHaveCSS('border-left-style', 'solid')
    expect(await branches.nth(0).locator('.trk-thread-connector').evaluate(element => parseFloat(getComputedStyle(element).borderLeftWidth))).toBeLessThan(2)
    expect(childBox!.height).toBeCloseTo(parentBox!.height, 0)
    await nested.click()
    await expect(nested).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('[data-thread-id="thread"]')).not.toHaveAttribute('aria-current', 'page')
    await expect(page.locator('#trk-chatlog')).toContainText('Child-only finding')
    await page.keyboard.press('Tab')
    await nested.focus()
    await expect(nested).toBeFocused()
    await expect(nested).toHaveCSS('outline-style', 'none')
    await expect(nested.locator('.trk-thread-title')).toHaveCSS('text-decoration-line', 'underline')
    await page.screenshot({ animations: 'disabled', path: `/tmp/inky-sidebar-${theme}-${kind}.png` })
    expect(errors).toEqual([])
  })
}

for (const legacy of [false, true]) test(`sidebar titles load from ${legacy ? 'legacy messages' : 'stored names'} before selection`, async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
  const timestamp = '2026-11-18T00:00:00Z'
  const active: ApiThread = {
    id: 'active', kind: 'native_thread', chat_type: 'thread', channel_tentacle_id: 'claude',
    chat_id: 'active', channel_thread_id: null, title: 'Active review', status: 'active', project_id: null,
    handoffs: [], created_at: timestamp, updated_at: timestamp,
  }
  const unopened = { ...active, id: 'unopened', chat_id: 'unopened', title: 'Unopened review', nested_session_count: 2 }
  const other = { ...active, id: 'other', chat_id: 'other', title: 'Another review' }
  const parent: ApiConversation = {
    id: 'parent', external_id: 'unopened', thread_id: unopened.id, agent_tentacle_id: 'claude', subagent_id: '',
    parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [], runs: [],
  }
  const run = { id: 'child-turn', kind: 'octomate', conversation_id: 'child', name: null, model_name: null,
    permission_mode: null, cwd: null, parent_run_id: 'turn', parent_tool_call_id: 'spawn', started_at: null } as const
  const summoned = { ...parent, id: 'summoned', agent_tentacle_id: 'codex' }
  const child = { ...summoned, id: 'child', subagent_id: 'child-abc123', parent_conversation_id: summoned.id,
    name: legacy ? null : 'Background sleep test', runs: [run] }
  const sibling = { ...child, id: 'sibling', subagent_id: 'child-def456', name: legacy ? null : 'review_task',
    runs: [{ ...run, id: 'sibling-turn', conversation_id: 'sibling', parent_tool_call_id: 'spawn-review' }] }
  const modelPath = '/api/trunkline/threads/unopened/conversations/messages'
  const metadataReads: string[] = []
  let showChildren!: () => void
  const opening = new Promise<void>(resolve => { showChildren = resolve })
  const parentReads: string[] = []
  const usageReads: string[] = []
  const historyReads: string[] = []
  await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/conversations')) metadataReads.push(path)
    if (path.includes('/threads/unopened/')) parentReads.push(path)
    if (path.endsWith('/usage')) usageReads.push(path)
    if (path.endsWith('/messages')) historyReads.push(path)
    if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner' } })
    if (path.endsWith('/threads')) return route.fulfill({ json: [active, unopened, other] })
    if (path.endsWith('/threads/active')) return route.fulfill({ json: active })
    if (path.endsWith('/threads/unopened')) return route.fulfill({ json: unopened })
    if (path.endsWith('/unopened/conversations')) {
      await opening
      return route.fulfill({ json: [parent, summoned, child, sibling] })
    }
    if (legacy && path === modelPath) {
      const older = Boolean(new URL(route.request().url()).searchParams.get('cursor'))
      const items: ApiModelMessage[] = [{ id: older ? '1' : '2', run_id: older ? 'turn' : 'unrelated', kind: 'response', timestamp,
        thread_messages: [], parts: [
          { part_kind: 'tool-call', tool_name: 'Agent', tool_call_id: 'spawn', args: { description: older ? 'Background sleep test' : 'Wrong run' } },
          { part_kind: 'builtin-tool-call', tool_name: 'spawn_agent', tool_call_id: 'spawn-review', args: '{"task_name":"review_task"}' },
        ] }]
      return route.fulfill({ json: { items, total: 2, has_more: !older, next_cursor: older ? null : 'next' } })
    }
    if (path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
    if (path.endsWith('/config')) return route.fulfill({ json: { path: '/artifacts' } })
    if (path.endsWith('/operations')) return route.fulfill({ json: { source: null, barred: {}, teleport: { destinations: [], routes: {} }, summon: { destinations: [], routes: {} } } })
    if (path.endsWith('/project')) return route.fulfill({ json: null })
    if (path.endsWith('/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
    return route.fulfill({ json: [] })
  })
  await page.goto('/threads/active')
  const parentRow = page.locator('.trk-thread-row').filter({ has: page.locator('[data-thread-id="unopened"]') })
  const expand = parentRow.getByRole('button', { name: '2 nested sessions', exact: true })
  await expect(expand).toBeVisible()
  await expect(page.locator('#trk-chathead')).toContainText('Active review')
  expect(metadataReads).toEqual(['/api/trunkline/threads/active/conversations'])
  expect(parentReads).toEqual([])
  metadataReads.length = usageReads.length = historyReads.length = 0
  await page.reload()
  await expect(expand).toBeVisible()
  await expect(page.locator('#trk-chathead')).toContainText('Active review')
  expect(metadataReads).toEqual(['/api/trunkline/threads/active/conversations'])
  expect(parentReads).toEqual([])
  await expand.click()
  const placeholders = page.locator('#trk-nest-unopened .trk-thread-placeholder')
  await expect(placeholders).toHaveCount(2)
  const reserved = (await placeholders.first().boundingBox())!.height
  showChildren()
  await expect(page.locator('[data-thread-id="child"]')).toContainText('Background sleep test')
  await expect(page.locator('[data-thread-id="sibling"]')).toContainText('review_task')
  await expect(page.locator('[data-thread-id="summoned"]')).toHaveCount(0)
  expect(Math.abs((await page.locator('[data-thread-id="child"]').boundingBox())!.height - reserved)).toBeLessThanOrEqual(1)
  await expand.click()
  await expand.click()
  await expect(page).toHaveURL(/\/threads\/active$/)
  const legacyReads = legacy ? [modelPath, modelPath] : []
  expect(parentReads).toEqual(['/api/trunkline/threads/unopened/conversations', ...legacyReads])
  expect(usageReads).toEqual(['/api/trunkline/threads/active/usage'])
  expect(historyReads.sort()).toEqual([
    '/api/trunkline/threads/active/conversations/messages', '/api/trunkline/threads/active/messages', ...legacyReads,
  ])

  await page.locator('[data-thread-id="unopened"]').click()
  await expect(page).toHaveURL(/\/threads\/unopened$/)
  await expect(page.locator('#trk-chathead')).toContainText('Unopened review')
  expect(metadataReads).toEqual(['/api/trunkline/threads/active/conversations', '/api/trunkline/threads/unopened/conversations'])
  expect(usageReads).toEqual(['/api/trunkline/threads/active/usage', '/api/trunkline/threads/unopened/usage'])
  expect(historyReads.sort()).toEqual([
    '/api/trunkline/threads/active/conversations/messages', '/api/trunkline/threads/active/messages',
    ...legacyReads, modelPath, '/api/trunkline/threads/unopened/messages',
  ])
})
