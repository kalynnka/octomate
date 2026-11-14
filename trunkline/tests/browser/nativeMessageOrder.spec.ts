import { test, expect } from '@playwright/test'
import type { ApiConversation, ApiModelMessage, ApiThread, ApiThreadMessage } from '../../src/lib/api/events'

const timestamp = '2026-11-12T17:33:14Z'
const question = 'Render quotes and code blocks, or keep literal markers?'
const reply = 'Render quotes and code blocks'
const replyText = `<send_user_message_question_reply>\n${JSON.stringify([{
  questionItemId: JSON.stringify(['request_user_input_async', 'native-q', 0]), question, answer: reply,
}])}\n</send_user_message_question_reply>`
const thread: ApiThread = {
  id: 'thread-a', kind: 'native_thread', chat_type: 'thread', chat_id: 'native', channel_tentacle_id: 'codex',
  channel_thread_id: null, title: 'Native message order', project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
}
const conversation: ApiConversation = {
  id: 'conversation', external_id: null, thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
  parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
  runs: [{ id: 'run', kind: 'external', conversation_id: 'conversation', name: null, model_name: null,
    permission_mode: null, cwd: null, parent_run_id: null, parent_tool_call_id: null, started_at: timestamp }],
}
const prompts: ApiThreadMessage[] = ['Render my message.', replyText, 'Is this standard Markdown?'].map((text, index) => ({
  id: `01${index}`, kind: 'message', thread_id: thread.id, platform_message_id: null, reply_id: '', happened_at: timestamp,
  direction: 'inbound', actor_kind: 'human', agent_tentacle_id: null, sender: null, segments: [{ type: 'text', data: { text } }],
  created_at: timestamp, message_text: text,
}))
// Native prompts reach the ledger immediately; the model sequence is saved later.
const requests: ApiModelMessage[] = prompts.map((row, index) => ({
  id: ['100', '103', '104'][index], run_id: 'run', kind: 'request', timestamp,
  parts: [{ part_kind: 'user-prompt', content: row.message_text! }], thread_messages: [row],
}))
const messages: ApiModelMessage[] = [
  requests[0],
  { id: '101', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'text', content: 'I’ll check the renderer.' }] },
  { id: '102', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [
    { part_kind: 'builtin-tool-call', tool_name: 'request_user_input_async', tool_call_id: 'native-q', args: { questions: [{ title: question, options: [reply, 'Keep literal markers'] }] } },
  ] },
  requests[1], requests[2],
  { id: '105', run_id: 'run', kind: 'response', timestamp, thread_messages: [], parts: [{ part_kind: 'text', content: 'Yes, both are common Markdown.' }] },
]

for (const width of [1280, 390]) {
  test(`native question answers follow the question after reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 950 })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: width === 1280 ? 'dark' : 'light' })
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    await page.routeWebSocket(/\/api\/trunkline\/live/, () => {})
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path.endsWith('/threads/thread-a')) return route.fulfill({ json: thread })
      if (path.endsWith('/conversations/messages')) return route.fulfill({ json: { items: [...messages].reverse(), total: messages.length, has_more: false, next_cursor: null } })
      if (path.endsWith('/messages')) return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      if (path.endsWith('/conversations')) return route.fulfill({ json: [conversation] })
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
    for (const reload of [false, true]) {
      if (reload) await page.reload()
      await expect(log.getByText('Yes, both are common Markdown.', { exact: true })).toBeVisible()
      expect(await log.locator('[id^="pm-"]').evaluateAll(nodes => nodes.map(node => node.id))).toEqual([
        'pm-010', 'pm-101.0', 'pm-102.0', 'pm-011', 'pm-012', 'pm-105.0',
      ])
      await expect(page.locator('[id="pm-102.0"]')).toContainText(`Answer: ${reply}`)
      await expect(page.locator('#pm-011')).toHaveText(new RegExp(reply))
      expect(await log.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    }
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/trunkline-native-message-order-${width}.png` })
  })
}
