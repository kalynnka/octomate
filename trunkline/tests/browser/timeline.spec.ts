import { test, expect } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread, ApiThreadMessage } from '../../src/lib/api/events'

for (const width of [1280, 390]) {
  test(`header controls work by keyboard and timeline previews stay readable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const timestamp = '2026-10-31T02:00:00Z'
    let showThreads!: () => void
    const threadsReady = new Promise<void>(resolve => { showThreads = resolve })
    const thread: ApiThread = {
      id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Timeline previews',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
    }
    const conversation: ApiConversation = {
      id: 'conversation', external_id: null, thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'run', kind: width === 1280 ? 'external' : 'octomate', conversation_id: 'conversation', name: null, model_name: null,
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
        { part_kind: 'builtin-tool-call', tool_name: 'codex_command_execution', tool_call_id: 'read', args: { command: 'cat README.md', commandActions: [{ type: 'read', path: '/work/README.md' }] } },
        { part_kind: 'builtin-tool-return', tool_name: 'codex_command_execution', tool_call_id: 'read', content: 'File contents.' },
        { part_kind: 'builtin-tool-call', tool_name: 'exec', tool_call_id: 'code', args: { code: 'inspect()' } },
        { part_kind: 'builtin-tool-return', tool_name: 'exec', tool_call_id: 'code', content: 'Inspection complete.' },
      ],
    }
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    // Every API request stays in the fixture, away from the developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/agents') return route.fulfill({ json: [{
        id: 'codex', icon_url: '/agent-icons/openai.svg', description: '', gateway: true, default_model: null,
        permission_modes: [], default_permission_mode: null, routes: [], driven_sessions: 0, native_sessions: 1,
      }] })
      if (path === '/api/trunkline/threads') {
        await threadsReady
        return route.fulfill({ json: [thread] })
      }
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/conversations')) return route.fulfill({ json: [conversation] })
      if (path.endsWith('/thread-a/conversations/messages')) return route.fulfill({ json: { items: [response], total: 1, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/messages')) return route.fulfill({ json: { items: [prompt], total: 1, has_more: false, next_cursor: null } })
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: null })
      if (path.endsWith('/thread-a/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects',
        '/api/trunkline/threads/thread-a/batches'].includes(path)) return route.fulfill({ json: [] })
      return route.fulfill({ status: 404, json: { detail: `Unmocked test API: ${path}` } })
    })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/threads/thread-a', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle('Trunkline · Octomate')
    if (width === 390) await page.getByRole('button', { name: 'Show channels', exact: true }).first().press('Enter')
    const sidebar = page.locator('#trk-sb-panel')
    await expect(sidebar).toContainText('Loading threads…')
    await expect(sidebar).not.toContainText('0 thr · 0 ch')
    showThreads()
    await expect(page.locator('[id="pm-reply.2"]')).toBeAttached()
    await expect(sidebar).not.toContainText('Loading threads…')
    const channel = page.getByRole('button', { name: 'Trunkline threads', exact: true })
    await channel.press('Space')
    await expect(channel).toHaveAttribute('aria-expanded', 'false')
    await channel.press('Enter')
    await expect(channel).toHaveAttribute('aria-expanded', 'true')
    const threadButton = sidebar.getByRole('button', { name: /Timeline previews/ })
    await threadButton.press('Enter')
    await expect(threadButton).toHaveAttribute('aria-current', 'page')
    await expect(sidebar.getByRole('button', { name: 'New trunkline thread', exact: true })).toBeEnabled()
    if (width === 390) {
      await page.keyboard.press('Escape')
      await expect(sidebar).toHaveAttribute('data-folded', '')
      await expect(page.getByRole('button', { name: 'Show channels', exact: true }).last()).toBeFocused()
    }
    await page.getByRole('button', { name: 'Choose thread operation', exact: true }).focus()
    await page.keyboard.press('Tab')
    const theme = page.getByRole('button', { name: /^Switch to (light|dark)$/ })
    await expect(theme).toBeFocused()
    await expect(theme).toHaveAttribute('aria-label', 'Switch to dark')
    await expect(theme).toHaveCSS('outline-style', 'solid')
    expect(await theme.evaluate(el => parseFloat(getComputedStyle(el).outlineWidth))).toBeGreaterThan(0)
    await page.keyboard.press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await expect(theme).toHaveAttribute('aria-label', 'Switch to light')
    await page.screenshot({ path: `/private/tmp/inky-header-dark-${width}.png` })
    await page.keyboard.press('Space')
    await expect(page.locator('html')).not.toHaveAttribute('data-theme')
    await expect(theme).toHaveAttribute('aria-label', 'Switch to dark')
    await page.keyboard.press('Tab')
    const toggle = page.getByRole('button', { name: 'Timeline panel', exact: true })
    await expect(toggle).toBeFocused()
    await expect(toggle).toHaveAttribute('aria-controls', 'trk-trace-panel')
    await expect(toggle).toHaveCSS('outline-style', 'solid')
    expect(await toggle.evaluate(el => parseFloat(getComputedStyle(el).outlineWidth))).toBeGreaterThan(0)
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await page.keyboard.press('Enter')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const panel = page.locator('#trk-trace-panel')
    await expect(panel).not.toHaveAttribute('data-folded')
    await page.keyboard.press('Space')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(panel).toHaveAttribute('data-folded')
    await page.keyboard.press('Enter')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(panel).not.toHaveAttribute('data-folded')
    const timeline = page.locator('#trk-timeline')
    const conversationFold = timeline.getByTitle('Fold / unfold conversation', { exact: true })
    if (width === 1280) {
      await expect(conversationFold).toContainText('ingest')
      await expect(conversationFold).not.toContainText('ingested')
    } else {
      await expect(conversationFold).toContainText('● active')
    }
    await conversationFold.press('Space')
    await expect(conversationFold).toHaveAttribute('aria-expanded', 'false')
    await conversationFold.press('Enter')
    await expect(conversationFold).toHaveAttribute('aria-expanded', 'true')
    const runFold = timeline.getByTitle('Fold / unfold run', { exact: true })
    await runFold.press('Enter')
    await expect(runFold).toHaveAttribute('aria-expanded', 'false')
    await runFold.press('Space')
    await expect(runFold).toHaveAttribute('aria-expanded', 'true')
    await expect(timeline.locator('[data-uid="prompt"]')).toContainText('Check the workspace.')
    await expect(timeline.locator('[data-uid="reply.0"]')).toContainText('Checking the workspace.')
    const identity = timeline.locator('[data-uid="reply.0"] [data-agent-label]')
    await expect(identity).toHaveText('Octomate')
    await expect(identity.getByRole('img', { name: 'Octomate(Codex)' })).toBeVisible()
    await expect(identity.locator('.trk-agent-icon')).toHaveCSS('width', '10px')
    await expect(identity.locator('.trk-agent-icon')).toHaveCSS('height', '10px')
    await expect.poll(() => identity.locator('img').evaluate(el => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
    const reasoning = timeline.locator('[data-uid="reply.1"]')
    await expect(reasoning.locator(':scope > div > span').first()).toHaveText('thk')
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
    await expect(timeline.locator('[data-uid="reply.5"]')).toContainText('Read README.md')
    await expect(timeline.locator('[data-uid="reply.7"]')).toContainText('Ran code')
    await expect(timeline.locator('[data-uid] > svg')).toHaveCount(0)
    await expect(timeline.locator('.trk-trace-hover-brackets, .trk-timeline-status')).toHaveCount(0)
    for (const scheme of ['dark', 'light']) {
      await page.getByRole('button', { name: `Switch to ${scheme}`, exact: true }).press('Enter')
      await timeline.screenshot({ animations: 'disabled', path: `/private/tmp/inky-timeline-brief-${width}-${scheme}.png` })
    }
    if (width === 390) {
      await page.getByRole('button', { name: 'Close timeline', exact: true }).click()
      await expect(toggle).toBeFocused()
      await expect(panel).toHaveAttribute('data-folded')
      await page.keyboard.press('Enter')
      await page.keyboard.press('Escape')
      await expect(panel).toHaveAttribute('data-folded')
      await expect(toggle).toBeFocused()
      await page.keyboard.press('Enter')
    }
    await page.locator('#trk-chatlog').evaluate(async log => {
      const ledger = log.querySelector('.trk-ledger-col')!
      for (const position of ['before', 'after']) {
        const space = document.createElement('div')
        space.style.cssText = 'height:1400px;flex-shrink:0'
        if (position === 'before') ledger.prepend(space)
        else ledger.append(space)
      }
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
      // Preserve the fresh thread's bottom-following state for its first jump.
      log.scrollTop = log.scrollHeight
    })
    const locating = summary.evaluate(row => new Promise<{ top: number; height: number; gap: number }[]>(resolve => {
      row.addEventListener('click', () => {
        const log = document.getElementById('trk-chatlog')!
        const card = document.getElementById('pm-reply.2')!
        const fold = card.querySelector(':scope > .lt-fold')!
        const samples: { top: number; height: number; gap: number }[] = []
        const start = performance.now()
        const sample = () => {
          const viewport = log.getBoundingClientRect()
          samples.push({ top: log.scrollTop, height: fold.getBoundingClientRect().height,
            gap: (card.getBoundingClientRect().top - viewport.top) / (viewport.height / log.clientHeight) })
          if (performance.now() - start < 1000) requestAnimationFrame(sample)
          else resolve(samples)
        }
        sample()
      }, { once: true })
    }))
    await summary.click()
    const located = await locating
    expect(located.some((sample, i) => i > 0 && Math.abs(sample.top - located[i - 1]!.top) > 1 && sample.height > located[i - 1]!.height + 0.1)).toBe(true)
    expect(Math.abs(located.at(-1)!.gap - 46)).toBeLessThan(2)
    if (width === 390) {
      await expect(panel).toHaveAttribute('data-folded')
      await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    }
    const card = page.locator('[id="pm-reply.2"]')
    await expect(card).toHaveClass(/trk-flash/)
    await expect(card).toBeInViewport()
    await expect(card).toBeFocused()
    await expect(card.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(card.locator('strong')).toHaveText('Preparing binary-search example')
    await expect(card.locator('strong')).toBeVisible()
    const manual = page.locator('[id="pm-reply.1"]')
    await manual.getByRole('button').click()
    await expect(manual.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await card.locator(':scope > .lt-fold > div').evaluate(el => {
      const content = document.createElement('div')
      content.style.height = '600px'
      el.append(content)
    })
    if (width === 390) await toggle.press('Enter')
    await timeline.locator('[data-uid="reply.5"]').press('Enter')
    const tool = page.locator('[id="pm-reply.5"]')
    await expect(tool).toBeFocused()
    await expect(tool.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(tool.locator('pre').last()).toBeVisible()
    await expect(card.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => card.locator(':scope > .lt-fold').evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(1)
    await expect(card).not.toHaveClass(/trk-flash/)
    await expect(manual.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect.poll(() => tool.evaluate(el => {
      const log = document.getElementById('trk-chatlog')!
      const viewport = log.getBoundingClientRect()
      return Math.abs((el.getBoundingClientRect().top - viewport.top) / (viewport.height / log.clientHeight) - 46)
    })).toBeLessThan(2)
    if (width === 390) await toggle.press('Enter')
    await timeline.locator('[data-uid="reply.5"]').press('Enter')
    await expect(tool).toBeFocused()
    await expect(tool.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect(tool).toHaveClass(/trk-flash/)
    await tool.evaluate(el => {
      for (const animation of el.getAnimations()) {
        if (animation instanceof CSSAnimation && animation.animationName === 'trkFlash') {
          animation.pause()
          animation.currentTime = 100
        }
      }
    })
    await tool.screenshot({ path: `/private/tmp/inky-timeline-highlight-${width}.png` })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/inky-timeline-${width}.png` })
    response.parts = [
      { part_kind: 'text', content: 'Earlier discussion. '.repeat(300) },
      ...response.parts.slice(1),
      { part_kind: 'text', content: 'Later discussion. '.repeat(300) },
    ]
    await page.reload({ waitUntil: 'domcontentloaded' })
    await toggle.click()
    // The first timeline selection after a real refresh must leave the tail,
    // without a preparatory chat scroll or waiting for entry animations.
    await summary.click()
    await expect(card).toBeFocused()
    await expect(card.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    await expect.poll(() => card.evaluate(el => {
      const log = document.getElementById('trk-chatlog')!
      const viewport = log.getBoundingClientRect()
      return Math.abs((el.getBoundingClientRect().top - viewport.top) / (viewport.height / log.clientHeight) - 46)
    })).toBeLessThan(2)
    expect(errors).toEqual([])
  })
}
