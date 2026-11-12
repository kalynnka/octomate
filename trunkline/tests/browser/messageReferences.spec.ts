import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { test, expect, type WebSocketRoute } from '@playwright/test'
import type { ApiModelMessage, ApiThread, ApiThreadMessage, MessageAnnotations, WireEvent } from '../../src/lib/api/events'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const code = "if True:\n    print('kept')\n\n\n\nprint('done')\n"
const userText = `launch a PR.\n\n\n\nThen review.\n\n> make the backfill script a migration, data one.\n\n\`\`\`python\n${code}\`\`\``
const browserInstructions = 'Use browser tools.\n\n## Loading tools\n\nLoad the tools needed for the task together. Read the page before choosing an action, and check the result after each interaction.\n\n- Inspect the current page.\n- Choose the required browser tools.\n- Verify the result before continuing.\n\n## Recording\n\nKeep the instructions with the assistant activity so the full context can be inspected without expanding the user message.'
const fixture: { ide: MessageAnnotations; claude: MessageAnnotations; citation: MessageAnnotations; bodies: string[]; raw: string[]; events: WireEvent[] } = JSON.parse(execFileSync(`${root}/.venv/bin/python`, ['-c', `
import json, sys
from octomate.capabilities.harness.events import wire_event_adapter
from tests.agent.test_message_annotations import IDE, CLAUDE, CITATION, citation_events, codex_prompt, claude_prompt, answer_part
raw = 'Answer.\\n\\n' + CITATION
ide_raw = IDE.replace('launch a PR.', sys.argv[1]).replace('My file.py', 'Run macOS isolation audit.command')
claude_raw = CLAUDE.replace('Use browser tools.', sys.argv[2])
ide, claude, answer = codex_prompt(ide_raw), claude_prompt(claude_raw), answer_part(raw)
events = [wire_event_adapter.dump_python(event, mode='json') for event in citation_events(raw, chunks=7)]
print(json.dumps(dict(ide=ide.metadata['annotations'], claude=claude.metadata['annotations'], citation=answer.provider_details['annotations'], bodies=[ide.content, claude.content, answer.content], raw=[ide_raw, claude_raw, raw], events=events)))
`, userText, browserInstructions], { cwd: root, env: { ...process.env, OCTOMATE_DB_URL: 'sqlite+aiosqlite:///:memory:', OCTOMATE_HOME: `${root}/tests/config` }, encoding: 'utf8' }))

for (const width of [1280, 390]) {
  test(`native context and file references survive live updates and reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 950 })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: width === 1280 ? 'dark' : 'light' })
    await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async (text: string) => { document.documentElement.setAttribute('data-copied', text) },
    } }))
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const timestamp = '2026-11-12T12:00:00Z'
    const thread: ApiThread = { id: 'thread-a', kind: 'native_thread', chat_type: 'thread', chat_id: 'native', channel_tentacle_id: 'codex', channel_thread_id: null, title: 'Native message references', project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [] }
    const prompts: ApiThreadMessage[] = fixture.bodies.slice(0, 2).map((message_text, index) => ({
      id: `prompt-${index}`, kind: 'message', thread_id: thread.id, platform_message_id: null, reply_id: '', happened_at: timestamp,
      direction: 'inbound', actor_kind: 'human', agent_tentacle_id: null, sender: null, segments: [{ type: 'text', data: { text: message_text, annotations: index === 0 ? fixture.ide : fixture.claude } }], created_at: timestamp,
      message_text,
    }))
    prompts.push({ ...prompts[0], id: 'prompt-memory', message_text: 'Human message with appended context.',
      segments: [{ type: 'text', data: { text: 'Human message with appended context.', annotations: fixture.citation } }],
    })
    const response: ApiModelMessage = { id: 'reply', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'text', content: fixture.bodies[2], provider_details: { annotations: fixture.citation } }] }
    const prefixedResponse: ApiModelMessage = { ...response, id: 'intro', parts: [{ part_kind: 'text', content: 'Assistant message with prepended context.', provider_details: { annotations: fixture.ide } }] }
    const sockets = new Set<WebSocketRoute>()
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => { sockets.add(socket); socket.onClose(() => sockets.delete(socket)) })
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path.endsWith('/threads/thread-a')) return route.fulfill({ json: thread })
      if (path.endsWith('/conversations/messages')) return route.fulfill({ json: { items: [response, prefixedResponse], total: 2, has_more: false, next_cursor: '' } })
      if (path.endsWith('/messages')) return route.fulfill({ json: { items: [...prompts].reverse(), total: prompts.length, has_more: false, next_cursor: '' } })
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
    const log = page.locator('#trk-chatlog')
    const ide = page.locator('#pm-prompt-0')
    const claude = page.locator('#pm-prompt-1')
    const answer = page.locator('[id="pm-reply.0"]')
    const humanMemory = page.locator('#pm-prompt-memory')
    const assistantContext = page.locator('[id="pm-intro.0"]')
    const ideCard = page.locator('[id="pm-prompt-0.context.0.0"]')
    const browserCard = page.locator('[id="pm-prompt-1.context.0.0"]')
    const assistantCard = page.locator('[id="pm-intro.0.context.0.0"]')
    const memoryCard = page.locator('[id="pm-reply.0.context.0.0"]')
    const humanMemoryCard = page.locator('[id="pm-prompt-memory.context.0.0"]')
    await expect(ide).toContainText('launch a PR.')
    for (const message of [ide, claude, assistantContext]) {
      const context = message.locator('.trk-message-references[data-position="before"]')
      await expect(context).toBeVisible()
      expect(await message.evaluate(element => element.querySelector('.trk-message-references[data-position="before"]')!.getBoundingClientRect().bottom
        - element.querySelector('.trk-md')!.getBoundingClientRect().top)).toBeLessThanOrEqual(0)
    }
    for (const message of [answer, humanMemory]) {
      await expect(message.locator('.trk-message-references')).toHaveCount(0)
      await expect(message.locator(':scope > .trk-message-context')).toHaveCount(1)
      expect(await message.evaluate(element => {
        const body = element.querySelector('.trk-md')!.getBoundingClientRect()
        const annotation = element.querySelector('.trk-message-context')!.getBoundingClientRect()
        const actions = element.querySelector(':scope > .trk-message-actions')!.getBoundingClientRect()
        return body.bottom <= annotation.top && annotation.bottom <= actions.top && element.lastElementChild!.classList.contains('trk-message-actions')
      })).toBe(true)
    }
    await expect(ide.locator(':scope > .trk-message-references')).toHaveCount(1)
    await expect(claude.getByRole('button', { name: 'Opened file', exact: true })).toHaveCount(0)
    await expect(claude.getByRole('button', { name: 'Copy reference: /work/main.py', exact: true })).toHaveCount(1)
    const longReference = ide.getByRole('button', { name: 'Copy reference: src/Run macOS isolation audit.command', exact: true })
    expect(await longReference.locator('.trk-reference-name').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
    for (const message of [ide, claude]) {
      const references = message.locator(':scope > .trk-message-references')
      const bounds = (await references.boundingBox())!
      const lastChip = (await references.locator('.trk-reference-tags > :last-child').boundingBox())!
      expect(Math.abs(lastChip.x + lastChip.width - bounds.x - bounds.width)).toBeLessThan(2)
      expect(lastChip.height).toBeLessThanOrEqual(26)
      await expect(message.locator('.trk-message-context')).toHaveCount(0)
      for (const chip of await references.locator('.trk-reference-copy').all()) {
        await expect(chip).toHaveCSS('border-top-width', '0px')
        expect((await chip.boundingBox())!.width).toBeLessThan(200)
      }
    }
    for (const card of [ideCard, browserCard, assistantCard, memoryCard, humanMemoryCard]) {
      await expect(card).toHaveClass(/trk-trace-row/)
      await expect(card.locator(':scope > button')).toHaveAttribute('aria-expanded', 'false')
      expect(await card.evaluate(element => element.parentElement!.matches('.trk-ledger-col, .lt-message-entry'))).toBe(true)
      expect(await card.evaluate(element => element.getBoundingClientRect().left - element.parentElement!.getBoundingClientRect().left)).toBeLessThanOrEqual(1)
    }
    expect(await ideCard.evaluate(element => Boolean(element.compareDocumentPosition(document.getElementById('pm-prompt-0')!) & Node.DOCUMENT_POSITION_PRECEDING))).toBe(true)
    expect(await browserCard.evaluate(element => Boolean(element.compareDocumentPosition(document.getElementById('pm-prompt-1')!) & Node.DOCUMENT_POSITION_PRECEDING))).toBe(true)
    expect(await assistantCard.evaluate(element => Boolean(element.compareDocumentPosition(document.getElementById('pm-intro.0')!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true)
    expect(await memoryCard.evaluate(element => Boolean(element.compareDocumentPosition(document.getElementById('pm-reply.0')!) & Node.DOCUMENT_POSITION_PRECEDING))).toBe(true)
    expect(await humanMemoryCard.evaluate(element => Boolean(element.compareDocumentPosition(document.getElementById('pm-prompt-memory')!) & Node.DOCUMENT_POSITION_PRECEDING))).toBe(true)
    await expect(ide.locator('.trk-md > p')).toHaveText(['launch a PR.', 'Then review.'])
    await expect(ide.locator('blockquote')).toHaveText('make the backfill script a migration, data one.')
    expect(await ide.locator('pre code').textContent()).toBe(code)
    await ide.getByRole('button', { name: 'Copy code', exact: true }).press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-copied', code)
    await ide.screenshot({ path: `/private/tmp/trunkline-user-markdown-${width}.png` })
    await expect(claude).toContainText('My actual request.')
    await expect(answer.locator('.trk-md')).toHaveText('Answer.')
    const context = ideCard.getByRole('button', { name: 'IDE context', exact: true })
    await expect(context).toHaveAttribute('aria-expanded', 'false')
    const reference = ide.getByRole('button', { name: 'Copy reference: src/main.py', exact: true })
    await reference.focus()
    await expect(reference.locator('.trk-reference-tooltip')).toBeVisible()
    await expect(reference.locator('.trk-reference-tooltip')).toContainText('Active file · Selection · Open tab')
    await page.keyboard.press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-copied', 'src/main.py')
    await expect(reference.getByRole('status')).toHaveText('Copied')
    await context.press('Enter')
    await expect(context).toHaveAttribute('aria-expanded', 'true')
    await expect(ideCard.locator('.trk-context-content')).toBeVisible()
    await expect(ideCard.getByRole('heading', { name: 'Active file: src/main.py', exact: true })).toBeVisible()
    await ideCard.screenshot({ path: `/private/tmp/trunkline-ide-card-${width}.png` })
    await context.press('Space')
    const browser = browserCard.getByRole('button', { name: 'Browser instructions', exact: true })
    await browser.press('Enter')
    await expect(browserCard.getByText('Use browser tools.', { exact: true })).toBeVisible()
    await expect(browserCard.getByRole('heading', { name: 'Loading tools', exact: true })).toBeVisible()
    await expect(browserCard.locator('li')).toHaveCount(3)
    await expect(browserCard).toHaveCSS('border-top-style', 'solid')
    expect(await browserCard.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    await browser.evaluate(element => element.blur())
    await browserCard.screenshot({ path: `/private/tmp/trunkline-browser-card-${width}.png` })
    await browser.press('Space')
    const memory = memoryCard.getByRole('button', { name: 'Memory sources', exact: true })
    await memory.press('Enter')
    await expect(memory).toHaveAttribute('aria-expanded', 'true')
    await expect(memoryCard).toHaveCSS('border-top-style', 'solid')
    await expect(memoryCard.locator('.trk-reference-tags')).toHaveCount(0)
    const memoryReference = memoryCard.getByRole('button', { name: 'Copy reference: MEMORY.md:274–275', exact: true })
    await expect(memoryReference).toHaveCount(1)
    await expect(memoryReference).toBeVisible()
    await expect(memoryReference.locator(':scope > svg')).toHaveCount(0)
    await expect(memoryReference.locator('.trk-reference-tooltip')).toHaveCount(0)
    await memoryReference.click()
    await expect(page.locator('html')).toHaveAttribute('data-copied', 'MEMORY.md:274–275')
    await expect(memoryReference.getByRole('status')).toHaveText('Copied')
    await expect(memoryReference.locator(':scope > svg')).toHaveCount(0)
    await expect(memoryCard.getByText('Verified against current code', { exact: true })).toHaveCount(1)
    await expect(memoryCard.getByText('01a1863b-63d4-78d1-9a53-944134fc9f01', { exact: true })).toBeVisible()
    await expect(memoryReference).toHaveCSS('border-top-width', '0px')
    await page.mouse.move(0, 0)
    await memoryReference.evaluate(element => element.blur())
    await memoryCard.screenshot({ path: `/private/tmp/trunkline-memory-card-${width}.png` })
    expect(Math.abs((await memoryCard.boundingBox())!.width - (await browserCard.boundingBox())!.width)).toBeLessThan(2)
    expect(await answer.evaluate(element => element.querySelector(':scope > .trk-message-actions')!.getBoundingClientRect().top
      - element.querySelector('.trk-message-context')!.getBoundingClientRect().bottom)).toBeGreaterThanOrEqual(0)
    const messageCopy = answer.getByRole('button', { name: 'Copy message', exact: true })
    await messageCopy.focus()
    await log.evaluate(element => { element.scrollTop = element.scrollHeight })
    await expect(messageCopy).toBeInViewport({ ratio: 1 })
    await page.screenshot({ path: `/private/tmp/trunkline-annotation-footer-${width}.png` })
    await memory.press('Space')
    await expect(memory).toHaveAttribute('aria-expanded', 'false')
    await expect(memoryCard.locator('.trk-context-content')).toHaveAttribute('inert', '')
    expect((await memoryCard.locator('.lt-fold').boundingBox())!.height).toBe(0)
    await ide.getByRole('button', { name: 'Copy message', exact: true }).press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-copied', fixture.raw[0])
    await claude.getByRole('button', { name: 'Copy message', exact: true }).press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-copied', fixture.raw[1])
    await answer.getByRole('button', { name: 'Copy message', exact: true }).press('Enter')
    await expect(page.locator('html')).toHaveAttribute('data-copied', fixture.raw[2])
    await page.evaluate(async () => { await document.fonts.ready; if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
    await page.mouse.move(0, 0)
    await page.screenshot({ path: `/private/tmp/trunkline-references-${width}.png` })
    expect(await log.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)

    const send = (event: WireEvent) => { for (const socket of sockets) socket.send(JSON.stringify(event)) }
    send({ event_kind: 'run_started', sender: null, address: { channel_tentacle_id: 'codex', chat_type: 'thread', chat_id: 'native', user_id: 'owner', channel_thread_id: null, shared: false } })
    send({ event_kind: 'enqueued_messages', enqueue_id: 'live-prompt', messages: [{ kind: 'request', timestamp, parts: [{ part_kind: 'user-prompt', content: [{ kind: 'text-content', content: fixture.bodies[0], metadata: { annotations: fixture.ide } }] }] }] })
    await expect(page.locator('#pm-live-prompt')).toContainText('launch a PR.')
    await expect(page.locator('#pm-live-prompt blockquote')).toHaveText('make the backfill script a migration, data one.')
    expect(await page.locator('#pm-live-prompt pre code').textContent()).toBe(code)
    await expect(page.locator('[id="pm-live-prompt.context.0.0"]').getByRole('button', { name: 'IDE context', exact: true })).toBeVisible()
    await expect(page.locator('#pm-live-prompt .trk-message-context')).toHaveCount(0)
    for (const event of fixture.events) send(event)
    await expect(log.getByRole('button', { name: 'Memory sources', exact: true })).toHaveCount(3)
    await expect(log.locator('.trk-md').last()).toHaveText('Answer.')
    await page.reload()
    await expect(log.getByRole('button', { name: 'Memory sources', exact: true })).toHaveCount(2)
    await expect(memory).toHaveAttribute('aria-expanded', 'false')
    await expect(context).toHaveAttribute('aria-expanded', 'false')
    await expect(ide.locator('blockquote')).toHaveText('make the backfill script a migration, data one.')
    expect(await ide.locator('pre code').textContent()).toBe(code)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
