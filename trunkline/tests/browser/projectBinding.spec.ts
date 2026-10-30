import { test, expect, type ElementHandle, type WebSocketRoute } from '@playwright/test'
import type { ApiConversation, ApiProject, ApiThread, ThreadOperations, WireEvent } from '../../src/lib/api/events'

for (const streaming of [false, true]) {
  test(`a project assignment updates the open workspace ${streaming ? 'during a reply' : 'while idle'}`, async ({ page }) => {
    const timestamp = '2026-10-30T06:00:00Z'
    const thread: ApiThread = {
      id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
      channel_tentacle_id: 'trunkline', channel_thread_id: 'thread-key', title: 'Project assignment',
      project_id: null, status: 'active', created_at: timestamp, updated_at: timestamp, handoffs: [],
      active_agent_tentacle_id: 'codex',
    }
    const project: ApiProject = {
      id: 'project', name: 'octomate', origin: '', root: '/work/octomate', extra_roots: [], description: null, enabled: true,
    }
    const conversation: ApiConversation = {
      id: 'conversation', external_id: null, thread_id: thread.id, agent_tentacle_id: 'codex', subagent_id: '',
      parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
      runs: [{ id: 'run', kind: 'octomate', conversation_id: 'conversation', name: null, model_name: null,
        permission_mode: null, cwd: '/work/chat/thread-a', parent_run_id: null, parent_tool_call_id: null, started_at: timestamp }],
    }
    const operations: ThreadOperations = {
      source: null, barred: {},
      teleport: { destinations: [], here: null, routes: {}, reason: 'No destinations' },
      summon: { destinations: [], here: null, routes: {}, reason: 'No agents' },
    }
    let assigned = false
    let notifications: WebSocketRoute | undefined
    let stream: WebSocketRoute | undefined
    let historyReads = 0
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()) })
    await page.routeWebSocket(/\/api\/trunkline\/live/, socket => {
      if (new URL(socket.url()).pathname.endsWith('/user')) notifications = socket
      else if (new URL(socket.url()).searchParams.has('thread_key')) stream = socket
    })
    // Every API request stays in the fixture, away from the developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      if (route.request().method() === 'POST' && path.endsWith('/thread-key/messages')) {
        const events: WireEvent[] = [
          { event_kind: 'reflex_awake' },
          { event_kind: 'run_started', address: { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'thread-key', shared: false } },
          { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'Still answering' } },
        ]
        for (const event of events) stream!.send(JSON.stringify(event))
        return route.fulfill({ status: 202 })
      }
      if (path === '/api/auth/me') return route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
      if (path === '/api/artifacts/config') return route.fulfill({ json: { path: '/artifacts' } })
      if (path === '/api/trunkline/threads') return route.fulfill({ json: [thread] })
      if (path === '/api/trunkline/threads/thread-a') return route.fulfill({ json: thread })
      if (path.endsWith('/thread-a/conversations')) return route.fulfill({ json: [conversation] })
      if (path.endsWith('/thread-a/operations')) return route.fulfill({ json: operations })
      if (path.endsWith('/thread-a/messages') || path.endsWith('/thread-a/conversations/messages')) {
        historyReads++
        return route.fulfill({ json: { items: [], total: 0, has_more: false, next_cursor: null } })
      }
      if (path.endsWith('/thread-a/project')) return route.fulfill({ json: assigned ? project : null })
      if (path.endsWith('/thread-a/usage') || path.endsWith('/permissions')) return route.fulfill({ json: {} })
      if (path.endsWith('/health')) return route.fulfill({ json: { ok: true } })
      if (['/api/trunkline/channels', '/api/trunkline/routes', '/api/trunkline/projects', '/api/trunkline/agents',
        '/api/trunkline/threads/thread-a/batches'].includes(path)) return route.fulfill({ json: [] })
      return route.fulfill({ status: 404, json: { detail: `Unmocked test API: ${path}` } })
    })
    await page.goto('/threads/thread-a')
    await expect(page).toHaveURL(/\/threads\/thread-a$/)
    await expect(page).toHaveTitle('Trunkline · Octomate')
    const composer = page.getByPlaceholder('directive… or / for commands')
    await expect(composer).toBeVisible()
    await expect(page.locator('#trk-projstrip')).toContainText('no project')
    await expect(page.locator('#trk-projstrip')).toContainText('/work/chat/thread-a')
    const log = page.locator('#trk-chatlog')
    let reply: ElementHandle<HTMLElement | SVGElement> | null = null
    if (streaming) {
      await composer.fill('Inspect the project')
      await composer.press('Enter')
      await expect(log.getByText('Still answering', { exact: true })).toBeVisible()
      reply = await log.getByText('Still answering', { exact: true }).elementHandle()
    }
    const reads = historyReads
    assigned = true
    thread.project_id = project.id
    notifications!.send(JSON.stringify({ event_kind: 'thread_project_bound', thread_id: thread.id, project_id: project.id, updated_at: timestamp } satisfies WireEvent))
    const strip = page.locator('#trk-projstrip')
    await expect(strip).toContainText(project.name)
    await expect(strip).toContainText(project.root)
    await expect(strip.getByTitle(`Open ${project.root} in VS Code`)).toBeVisible()
    expect(historyReads).toBe(reads)
    if (streaming) {
      expect(await reply!.evaluate(element => element.isConnected)).toBe(true)
      stream!.send(JSON.stringify({ event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'text', content_delta: ' after assignment' } } satisfies WireEvent))
      await expect(log.getByText('Still answering after assignment', { exact: true })).toBeVisible()
    }
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/private/tmp/inky-project-binding-${streaming ? 'streaming' : 'idle'}.png` })
  })
}
