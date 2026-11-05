import { test, expect, type Locator, type WebSocketRoute } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread, ThreadOperations, WireEvent } from '../../src/lib/api/events'
import type { LedgerItem } from '../../src/lib/api/types'

const textRight = (locator: Locator) => locator.evaluate(el => {
  const range = document.createRange()
  range.selectNodeContents(el)
  return range.getBoundingClientRect().right
})

for (const width of [1280, 390]) {
  test(`quiet thinking and tool bars fold independently in history and live work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const timestamp = '2026-11-04T02:00:00Z'
    const thread: ApiThread = {
      id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Inspect OpenUsage',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const conversation: ApiConversation = {
      id: 'conversation', external_id: null, thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'run', kind: 'octomate', conversation_id: 'conversation', name: null, model_name: null,
        permission_mode: null, cwd: '/work/chat', parent_run_id: null, parent_tool_call_id: null, started_at: timestamp }],
    }
    const operations: ThreadOperations = {
      source: null, barred: {},
      teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
      summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
    }
    const messages: ApiModelMessage[] = [
      { id: '050', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [
        { part_kind: 'thinking', content: '**Preparing the inspection**', provider_details: { reasoning_format: 'summary' } },
        { part_kind: 'text', content: 'I’ll read the log and search the documentation.' },
      ] },
      { id: '100', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [
        { part_kind: 'thinking', content: '**Applying surgical UI changes**\n\n**Checking trace border tokens**', provider_details: { reasoning_format: 'summary' } },
        { part_kind: 'tool-call', tool_name: 'Read', tool_call_id: 'read', args: { file_path: 'OpenUsage.log' } },
        { part_kind: 'tool-call', tool_name: 'Search', tool_call_id: 'search', args: { query: 'OpenUsage menu bar visibility' } },
      ] },
      { id: '200', run_id: 'run', kind: 'request', timestamp, thread_messages: [], parts: [
        { part_kind: 'tool-return', tool_name: 'Read', tool_call_id: 'read', content: 'Menu bar item initialized.' },
        { part_kind: 'tool-return', tool_name: 'Search', tool_call_id: 'search', content: 'Search timed out.', outcome: 'failed' },
      ] },
      { id: '300', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [
        { part_kind: 'text', content: 'The log was read. Documentation search failed.' },
        { part_kind: 'thinking', content: '**Checking the result**', provider_details: { reasoning_format: 'summary' } },
      ] },
      { id: '310', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'text', content: 'No files were changed.' }] },
    ]
    const sockets = new Set<WebSocketRoute>()
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => { sockets.add(socket); socket.onClose(() => sockets.delete(socket)) })
    const send = (...events: WireEvent[]) => { for (const socket of sockets) for (const event of events) socket.send(JSON.stringify(event)) }
    // Every request is isolated from the developer's backend and database.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path === '/api/trunkline/agents') return route.fulfill({ json: ['codex', 'claude', 'deepseek', 'custom', 'broken'].map(id => ({
        id, icon_url: id === 'custom' ? null : id === 'broken' ? '/broken-agent.svg' : `/agent-icons/${id === 'codex' ? 'openai' : id}.svg`,
        description: '', gateway: true, default_model: null, permission_modes: [], default_permission_mode: null, routes: [], driven_sessions: 0, native_sessions: 1,
      })) })
      if (path.endsWith('/thread-a/conversations')) return route.fulfill({ json: [conversation] })
      if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: messages, total: messages.length, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/operations')) return route.fulfill({ json: operations })
      if (path.endsWith('/thread-a/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: [] })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto('/threads/thread-a', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle('Trunkline · Octomate')
    const log = page.locator('#trk-chatlog')
    await expect(log.locator('.trk-agent-message')).toHaveCount(3)
    await expect(log.locator('[data-agent-label]')).toHaveCount(1)
    await expect(log.locator('[data-agent-label]')).toHaveText('Octomate')
    await expect(log.getByRole('img', { name: 'Octomate(Codex)' })).toBeVisible()
    expect(await log.locator('[id="pm-050.0"]').evaluate(el => el.previousElementSibling?.hasAttribute('data-agent-label'))).toBe(true)
    for (const id of ['300.0', '310.0']) {
      const message = log.locator(`[id="pm-${id}"]`)
      const bounds = await message.locator('.trk-md').boundingBox()
      const copy = await message.getByRole('button', { name: 'Copy message', exact: true }).boundingBox()
      expect(copy!.x).toBeGreaterThan(bounds!.x + bounds!.width)
      expect(copy!.y).toBeLessThan(bounds!.y + bounds!.height)
    }
    await log.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-assistant-group-${width}.png` })
    const thought = page.locator('[id="pm-100.0"]')
    const read = page.locator('[id="pm-100.1"]')
    const search = page.locator('[id="pm-100.2"]')
    await expect(log.locator('.trk-thinking-toggle')).toHaveCount(3)
    await expect(log.locator('.trk-tool-toggle')).toHaveCount(2)
    const group = log.locator('details.trk-trace-group').first()
    const singleThought = log.locator('.trk-ledger-col > [id="pm-300.1"]')
    await expect(singleThought).toBeVisible()
    await expect(singleThought.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    expect((await singleThought.boundingBox())!.width).toBeCloseTo((await group.boundingBox())!.width, 0)
    expect((await singleThought.boundingBox())!.x).toBeCloseTo((await group.boundingBox())!.x, 0)
    await singleThought.getByRole('button').press('Enter')
    await expect(singleThought.locator('strong')).toBeVisible()
    await singleThought.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-single-thinking-${width}.png` })
    await singleThought.getByRole('button').press('Enter')
    await expect(group.locator('summary')).toHaveText('Read files, searched▸')
    await expect(group.locator('summary > .lt-disclose')).toHaveCSS('transform', 'none')
    await expect(group).not.toHaveAttribute('open')
    await expect(read).not.toBeVisible()
    await expect(group.locator('.trk-thinking-toggle')).toHaveCount(1)
    await expect(thought).not.toBeVisible()
    await group.locator('summary').press('Enter')
    expect((await read.boundingBox())!.x).toBeGreaterThan((await group.boundingBox())!.x + 10)
    for (const row of [thought, read, search]) {
      await expect(row.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
      await expect(row.getByRole('button').locator(':scope > .lt-disclose')).toHaveText('▸')
      await expect(row).toHaveCSS('border-bottom-width', '0px')
      await expect(row).toHaveCSS('border-right-width', '0px')
      await expect(row.locator(':scope > span')).toHaveCount(0)
      await expect(row).toHaveCSS('animation-name', 'none')
    }
    await expect(thought).toHaveCSS('border-top-width', '0px')
    await expect(read).toHaveCSS('border-top-width', '0px')
    await expect(search).toHaveCSS('border-top-width', '0px')
    if (width === 1280) {
      const fraction = await group.evaluate(el => el.getBoundingClientRect().width / el.parentElement!.getBoundingClientRect().width)
      expect(fraction).toBeLessThan(.67)
    }
    const statusEnd = await textRight(read.locator('.trk-tool-status [data-text-current]'))
    const arrowGaps = await log.locator('.trk-thinking-toggle > .lt-disclose, .trk-tool-toggle > .lt-disclose, .trk-trace-summary > .lt-disclose').evaluateAll(elements => elements.map(el => {
      return el.getBoundingClientRect().left - el.previousElementSibling!.getBoundingClientRect().right
    }))
    expect(Math.min(...arrowGaps)).toBeGreaterThan(0)
    expect(Math.max(...arrowGaps)).toBeLessThan(10)
    await expect(group.locator('summary > .lt-disclose')).toHaveCSS('transform', 'matrix(0, 1, -1, 0, 0, 0)')
    expect(await textRight(search.locator('.trk-tool-status [data-text-current]'))).toBeCloseTo(statusEnd, 0)
    const doneColor = await read.locator('.trk-tool-status').evaluate(el => getComputedStyle(el).color)
    expect(doneColor).not.toBe(await read.locator('.trk-tool-toggle').evaluate(el => getComputedStyle(el).color))
    const gap = await search.evaluate(el => el.getBoundingClientRect().top - el.previousElementSibling!.getBoundingClientRect().bottom)
    expect(gap).toBeLessThanOrEqual(3)
    await expect(thought.getByRole('button')).toContainText('Thinking (summary)')
    await expect(read.getByRole('button')).not.toContainText('Tool')
    await expect(read.locator('.trk-tool-name')).toHaveText('Read OpenUsage.log')
    await expect(read.locator('.trk-tool-name')).toHaveAttribute('title', 'Read\nRead OpenUsage.log')
    await expect(read.locator('.trk-tool-name')).toHaveCSS('font-weight', '400')
    await thought.getByRole('button').hover()
    await expect(thought.locator('.trk-trace-hover-brackets')).toHaveCSS('opacity', '1')
    await expect(thought.locator('.trk-trace-hover-brackets > span')).toHaveCount(4)
    await page.mouse.move(0, 0)
    await expect(thought.locator('.trk-trace-hover-brackets')).toHaveCSS('opacity', '0')
    await expect(search.getByRole('button')).toContainText('failed')
    await expect(search.locator('.trk-tool-failure')).toHaveCount(0)
    await expect(search.locator('.lt-fold')).toHaveCSS('height', '0px')
    await search.getByRole('button').press('Enter')
    await expect(search.locator('pre').last()).toBeVisible()
    const failedColor = await search.locator('.trk-tool-status').evaluate(el => getComputedStyle(el).color)
    await expect(search.locator(':scope > span').first()).toHaveCSS('border-top-color', failedColor)
    expect(await search.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(await read.evaluate(el => getComputedStyle(el).backgroundColor))
    await search.getByRole('button').press('Enter')
    await thought.getByRole('button').press('Enter')
    await expect(thought.locator('strong')).toHaveText(['Applying surgical UI changes', 'Checking trace border tokens'])
    await expect(thought).toHaveAttribute('data-expanded', '')
    expect(await thought.evaluate(el => parseFloat(getComputedStyle(el).borderRightWidth))).toBeGreaterThan(0)
    await expect(thought.locator(':scope > span')).toHaveCount(4)
    await expect(read.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    await read.getByRole('button').press('Space')
    await expect(read.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(read).toHaveAttribute('data-expanded', '')
    expect(await read.evaluate(el => parseFloat(getComputedStyle(el).borderRightWidth))).toBeGreaterThan(0)
    await expect(read.locator(':scope > span')).toHaveCount(4)
    await expect(read.locator('pre').last()).toBeVisible()
    await expect(read.locator('pre').last()).toHaveText('Menu bar item initialized.')
    await expect(thought.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await thought.getByRole('button').press('Enter')
    await expect(thought).not.toHaveAttribute('data-expanded')
    await expect(thought.locator(':scope > span')).toHaveCount(0)
    await expect(read.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await read.getByRole('button').press('Enter')
    await group.locator('summary').press('Enter')
    await expect(read).not.toBeVisible()
    await page.getByRole('button', { name: 'Timeline panel', exact: true }).press('Enter')
    await page.locator('#trk-timeline [data-uid="100.0"]').press('Enter')
    await expect(group).toHaveAttribute('open', '')
    await expect(thought).toBeFocused()
    await expect(thought).toBeInViewport()
    await group.locator('summary').press('Enter')
    if (width === 390) await page.getByRole('button', { name: 'Timeline panel', exact: true }).press('Enter')
    await page.locator('#trk-timeline [data-uid="100.1"]').press('Enter')
    await expect(group).toHaveAttribute('open', '')
    await expect(read).toBeFocused()
    await expect(read).toBeInViewport()
    if (width === 1280) await page.getByRole('button', { name: 'Timeline panel', exact: true }).press('Enter')
    await expect(thought).toBeVisible()
    for (const theme of ['dark', 'light']) {
      await page.getByRole('button', { name: `Switch to ${theme}`, exact: true }).press('Enter')
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await log.locator('.trk-ledger-col').screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-bars-${width}-${theme}-folded.png` })
      await thought.getByRole('button').press('Enter')
      await log.locator('.trk-ledger-col').screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-bars-${width}-${theme}-expanded.png` })
      await thought.getByRole('button').press('Enter')
    }
    await expect.poll(() => sockets.size).toBeGreaterThan(0)
    send(
      { event_kind: 'reflex_awake' },
      { event_kind: 'run_started', address: { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'thread-key', shared: false } },
      { event_kind: 'part_start', index: 0, part: { part_kind: 'thinking', content: '**Checking settings**\n\n**Inspecting logs**\n\n**Verifying visibility**', provider_details: { reasoning_format: 'summary' } } },
      { event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'thinking', content_delta: '\n' } },
    )
    const liveThought = log.locator('.trk-trace-row:has(.trk-thinking-toggle)').last()
    const liveGroup = log.locator('details.trk-trace-group').last()
    await expect(liveGroup.locator('summary')).toHaveText('Thinking▸')
    await expect(liveThought.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(liveThought.locator('strong')).toHaveText(['Checking settings', 'Inspecting logs', 'Verifying visibility'])
    const thinkingLabel = liveThought.locator('.trk-trace-label')
    await expect(thinkingLabel).toHaveCSS('animation-name', 'none')
    await expect(thinkingLabel).not.toHaveCSS('color', 'rgba(0, 0, 0, 0)')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await expect(thinkingLabel).toHaveCSS('animation-name', 'trk-text-shimmer')
    const position = await thinkingLabel.evaluate(el => getComputedStyle(el).backgroundPositionX)
    await expect.poll(() => thinkingLabel.evaluate(el => getComputedStyle(el).backgroundPositionX)).not.toBe(position)
    await liveThought.getByRole('button').screenshot({ path: `/private/tmp/trunkline-thinking-shimmer-${width}.png` })
    send(
      { event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'ToolSearch', tool_call_id: 'load-tools', args: { query: 'files' } } },
      { event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: 'ToolSearch', tool_call_id: 'load-tools', content: 'Read is available.' } },
      { event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'codex_command_execution', tool_call_id: 'live-read', args: { command: 'cat OpenUsage.log', commandActions: [{ type: 'read', path: 'OpenUsage.log' }] } } },
      { event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: 'codex_command_execution', tool_call_id: 'live-read', content: 'Menu bar item initialized.' } },
      { event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'exec_command', tool_call_id: 'config', args: { cmd: 'plutil -p /Applications/OpenUsage.app/Contents/Info.plist' } } },
    )
    await expect(liveThought.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    await expect(thinkingLabel).toHaveCSS('animation-name', 'none')
    const liveTool = log.locator('.trk-trace-row:has(.trk-tool-toggle)').last()
    await expect(liveTool.locator('.trk-tool-name')).toHaveText('Running plutil -p /Applications/OpenUsage.app/Contents/Info.plist')
    await expect(liveGroup.locator('.trk-tool-name').nth(1)).toHaveText('Read OpenUsage.log')
    await expect(liveTool.locator('.trk-tool-name')).toHaveCSS('text-overflow', 'ellipsis')
    await expect(liveTool.locator('.trk-tool-status')).toHaveText('run')
    const thinkingTime = liveThought.locator('.trk-tool-status [data-text-current]')
    await expect(thinkingTime).toContainText('s')
    expect(await textRight(liveTool.locator('.trk-tool-status [data-text-current]')))
      .toBeCloseTo(await textRight(thinkingTime), 0)
    await expect(liveGroup.locator('.trk-thinking-toggle')).toHaveCount(1)
    await expect(liveGroup).toHaveAttribute('open', '')
    await expect(liveGroup.locator('summary')).toHaveText('Loaded tools, read files, running a command▸')
    await expect(liveGroup.locator('.trk-tool-toggle')).toHaveCount(3)
    await expect(liveThought.locator('.trk-tool-status > span').first()).toHaveCSS('width', '5px')
    for (const theme of ['dark', 'light']) {
      await page.getByRole('button', { name: `Switch to ${theme}`, exact: true }).press('Enter')
      await expect(liveGroup.locator('.trk-trace-title')).toHaveCSS('animation-name', 'trk-text-shimmer')
      await expect(liveTool.locator('.trk-tool-name > .trk-trace-label')).toHaveCSS('animation-name', 'trk-text-shimmer')
      await expect(liveGroup.locator('.trk-tool-name > .trk-trace-label').first()).toHaveCSS('animation-name', 'none')
      await expect(liveTool.locator('.trk-tool-name')).not.toHaveCSS('color', 'rgba(0, 0, 0, 0)')
      await liveGroup.screenshot({ path: `/private/tmp/trunkline-tools-shimmer-${width}-${theme}.png` })
    }
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const label of [liveGroup.locator('.trk-trace-title'), liveTool.locator('.trk-tool-name > .trk-trace-label')]) {
      await expect(label).toHaveCSS('animation-name', 'none')
      await expect(label).not.toHaveCSS('color', 'rgba(0, 0, 0, 0)')
    }
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await expect(liveTool.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    await liveTool.getByRole('button').press('Enter')
    await liveThought.getByRole('button').press('Enter')
    expect(await textRight(liveTool.locator('.trk-tool-status [data-text-current]')))
      .toBeCloseTo(await textRight(thinkingTime), 0)
    await liveGroup.locator('summary').press('Enter')
    send(
      { event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: 'exec_command', tool_call_id: 'config', content: 'Bundle settings read.' } },
    )
    await expect(liveGroup).not.toHaveAttribute('open')
    await expect(liveGroup.locator('summary')).toHaveText('Loaded tools, read files, ran a command▸')
    await expect(liveGroup.locator('.trk-trace-title')).toHaveCSS('animation-name', 'none')
    await expect(liveTool.locator('.trk-tool-name > .trk-trace-label')).toHaveCSS('animation-name', 'none')
    await expect(liveTool.locator('.trk-tool-name')).toHaveText('Ran plutil -p /Applications/OpenUsage.app/Contents/Info.plist')
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await liveGroup.locator('summary').press('Enter')
    await expect(liveThought.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(liveTool.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(liveTool.locator('pre').last()).toHaveText('Bundle settings read.')
    await liveTool.getByRole('button').press('Enter')
    await liveGroup.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-group-${width}-open.png` })
    send({ event_kind: 'part_start', index: 1, part: { part_kind: 'text', content: 'Now I’ll inspect menu bar visibility.' } })
    await expect(log.getByText('Now I’ll inspect menu bar visibility.', { exact: true })).toBeVisible()
    await expect(log.locator('[data-agent-label]')).toHaveCount(1)
    await expect(liveGroup).not.toHaveAttribute('open')
    await expect(liveGroup.locator('summary')).toBeVisible()
    await expect(liveTool).not.toBeVisible()
    await expect(liveThought).not.toBeVisible()
    await liveGroup.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-group-${width}-folded.png` })
    send(
      { event_kind: 'part_start', index: 2, part: { part_kind: 'thinking', content: '', provider_details: { reasoning_format: 'summary' } } },
      { event_kind: 'part_end', index: 2, part: { part_kind: 'thinking', content: '', provider_details: { reasoning_format: 'summary' } } },
      { event_kind: 'part_start', index: 3, part: { part_kind: 'builtin-tool-call', tool_name: 'inspect_window', tool_call_id: 'inspect', args: { window_id: 42 } } },
    )
    await expect(log.locator('.trk-thinking-toggle').last()).toContainText('Thinking (unavailable)')
    send(
      { event_kind: 'part_start', index: 4, part: { part_kind: 'builtin-tool-return', tool_name: 'inspect_window', tool_call_id: 'inspect', content: 'UI inspection denied.', outcome: 'denied' } },
      { event_kind: 'run_result', output: 'The inspection could not finish.', usage: { requests: 1, tool_calls: 2, input_tokens: 12, output_tokens: 4, cache_read_tokens: 0, cache_write_tokens: 0 } },
    )
    const failedTool = log.locator('.trk-trace-row:has(.trk-tool-toggle)').last()
    const failedGroup = log.locator('details.trk-trace-group').last()
    await expect(failedGroup).not.toHaveAttribute('open')
    await failedGroup.locator('summary').press('Enter')
    await expect(failedTool.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    await expect(failedTool.getByRole('button')).toContainText('failed')
    await expect(failedTool.locator('.trk-tool-failure')).toHaveCount(0)
    await failedTool.getByRole('button').press('Enter')
    await expect(failedTool.locator('pre').last()).toHaveText('UI inspection denied.')
    await failedTool.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-group-${width}-failed-light.png` })
    await page.getByRole('button', { name: 'Switch to dark', exact: true }).press('Enter')
    await failedTool.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-group-${width}-failed-dark.png` })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(log.locator('.trk-activity-group')).toHaveCount(0)
    await expect(log.locator('details.trk-trace-group')).toHaveCount(3)
    // Open an already-running native session: its start event predates this socket.
    sockets.clear()
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(log.getByText('The log was read. Documentation search failed.', { exact: true })).toBeVisible()
    await expect.poll(() => sockets.size).toBeGreaterThan(0)
    send({ event_kind: 'function_tool_call', part: {
      part_kind: 'tool-call', tool_name: 'late_probe', tool_call_id: 'late-probe', args: { cmd: 'sleep 12' },
    } })
    const joinedTool = log.locator('.trk-trace-row').filter({ has: page.locator('.trk-tool-name', { hasText: 'late_probe' }) })
    await expect(joinedTool).toBeVisible()
    await expect(joinedTool.locator('.trk-tool-status')).toHaveText('run')
    const joinedId = await joinedTool.getAttribute('id')
    await expect(log.locator('details.trk-trace-group')).toHaveCount(2)
    await joinedTool.getByRole('button').press('Enter')
    send({ event_kind: 'function_tool_result', part: {
      part_kind: 'tool-return', tool_name: 'late_probe', tool_call_id: 'late-probe', content: 'complete',
    } })
    await expect(joinedTool.locator('.trk-tool-status')).toHaveText('done')
    await expect(log.locator('details.trk-trace-group')).toHaveCount(2)
    send({ event_kind: 'run_result', output: 'Complete.', usage: { requests: 1, tool_calls: 1, input_tokens: 12, output_tokens: 4, cache_read_tokens: 0, cache_write_tokens: 0 } })
    await expect(log.locator('details.trk-trace-group')).toHaveCount(1)
    await expect(log.locator(`.trk-ledger-col > [id="${joinedId}"]`)).toBeVisible()
    await expect(joinedTool.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(joinedTool.locator('pre').last()).toHaveText('complete')
    await joinedTool.getByRole('button').press('Enter')
    await expect(joinedTool.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    const boundaries: LedgerItem[] = [
      { kind: 'user', uid: 'prompt', t: '', who: 'Owner', text: 'Continue.' },
      { kind: 'end', uid: 'ended', label: 'run complete' },
      { kind: 'session-open', uid: 'session', sessionId: 'next-session', tone: 'plain', text: 'New session' },
      { kind: 'agent', uid: 'other', label: 'Octomate(Claude)', blocks: [{ type: 'p', text: 'Another assistant.' }] },
    ]
    for (const boundary of boundaries) {
      await page.evaluate(async boundary => {
        const path = '/src/state/console.ts'
        const { useConsole } = await import(path)
        useConsole.setState({ running: false, live: [], detail: { ...useConsole.getState().detail, ledger: [
          { kind: 'agent', uid: 'before', label: 'Octomate(Codex)', blocks: [{ type: 'p', text: 'Before the boundary.' }] },
          boundary,
          { kind: 'stream', uid: 'after', label: 'Octomate(Codex)', text: 'After the boundary.', streaming: false },
        ] } })
      }, boundary)
      await expect(log.locator('[data-agent-label]')).toHaveCount(boundary.kind === 'agent' ? 3 : 2)
      expect(await log.locator('#pm-after').evaluate(el => el.previousElementSibling?.hasAttribute('data-agent-label'))).toBe(true)
    }
    await page.evaluate(async () => {
      const path = '/src/state/console.ts'
      const { useConsole } = await import(path)
      useConsole.setState({ ledgerN: 1 })
    })
    await expect(log.locator('[data-agent-label]')).toHaveCount(1)
    expect(await log.locator('#pm-after').evaluate(el => el.previousElementSibling?.hasAttribute('data-agent-label'))).toBe(true)
    await page.route('**/broken-agent.svg', route => route.fulfill({ contentType: 'image/svg+xml', body: 'not an image' }))
    for (const agentId of ['codex', 'claude', 'deepseek', 'custom', 'broken']) {
      await page.evaluate(async agentId => {
        const path = '/src/state/console.ts'
        const { useConsole } = await import(path)
        useConsole.setState({ ledgerN: 100, running: false, live: [], detail: { ...useConsole.getState().detail, ledger: [
          { kind: 'think', uid: `think-${agentId}`, agentId, dur: '', text: '**Checking the result**', thinking: false, format: 'summary' },
          { kind: 'agent', uid: `reply-${agentId}`, agentId, label: `Octomate(${agentId[0].toUpperCase() + agentId.slice(1)})`, blocks: [{ type: 'p', text: 'Inspection complete.' }] },
        ] } })
      }, agentId)
      const identity = log.locator('[data-agent-label]')
      await expect(identity).toHaveCount(1)
      expect(await log.locator('.trk-thinking-toggle').evaluate(el => el.closest('.trk-trace-row')!.previousElementSibling?.hasAttribute('data-agent-label'))).toBe(true)
      if (agentId === 'custom' || agentId === 'broken') {
        await expect(identity).toHaveText(`Octomate(${agentId[0].toUpperCase() + agentId.slice(1)})`)
        await expect(identity.locator('img')).toHaveCount(0)
      } else {
        await expect(identity).toHaveText('Octomate')
        await expect.poll(() => identity.locator('img').evaluate(el => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
        await expect(identity.getByRole('img')).not.toHaveCSS('mask-image', 'none')
      }
      if (agentId === 'codex') {
        for (const theme of ['light', 'dark']) {
          await page.getByRole('button', { name: `Switch to ${theme}`, exact: true }).press('Enter')
          await log.locator('.trk-ledger-col').screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-agent-identity-${width}-${theme}.png` })
        }
      }
    }
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
