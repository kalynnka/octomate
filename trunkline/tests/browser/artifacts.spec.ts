import { test, expect, type Page, type Frame } from '@playwright/test'
import { readFileSync } from 'node:fs'

const parentOrigin = 'http://127.0.0.1:5174'
const sandboxOrigin = 'http://127.0.0.1:8765'
const large = 'saved-data-'.repeat(200)
const appHtml = `<!doctype html><html><body><div id="result"></div><script>
window.received = []; window.replies = {};
function send(method, params, id) { parent.postMessage({ jsonrpc: '2.0', method, params, ...(id ? { id } : {}) }, '*') }
window.send = send;
window.addEventListener('message', ({ data }) => {
  if (data.id === 'init' && data.result) {
    window.host = data.result;
    send('ui/notifications/initialized', {});
  } else if (data.method === 'ui/resource-teardown') {
    document.body.dataset.teardown = 'true';
    parent.postMessage({ jsonrpc: '2.0', id: data.id, result: {} }, '*');
  } else if (data.method) {
    window.received.push(data);
    if (data.method === 'ui/notifications/tool-result') document.querySelector('#result').textContent = data.params.structuredContent.label;
    if (data.method === 'ui/notifications/host-context-changed') document.body.dataset.theme = data.params.theme;
  } else if (data.id) { window.replies[data.id] = data; }
});
send('ui/initialize', { protocolVersion: '2026-01-26', appInfo: { name: 'Test', version: '1' }, appCapabilities: {} }, 'init');
</script></body></html>`

async function mockViews(page: Page, options: { html?: string; fail?: boolean; csp?: Record<string, string[]>; path?: string } = {}) {
  const reads: string[] = []
  // Unmocked API calls must never reach a developer's running Octomate server.
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => route.fulfill({ status: 404, json: { detail: 'Unmocked test API' } }))
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } }))
  await page.route(/\/api\/(artifacts\/[^/]+|trunkline\/threads\/thread\/artifacts\/row-[ab])$/, async (route) => {
    reads.push(route.request().url())
    if (options.fail) return route.fulfill({ status: 404, json: { detail: 'Artifact installation unavailable' } })
    const label = route.request().url().split('/').at(-1)!
    const csp = options.csp ?? {}
    await route.fulfill({ json: {
      artifact: { id: label, kind: 'mcp_app', title: label, invocation: {
        tool: { name: 'create_diagram', inputSchema: { type: 'object' } },
        arguments: { diagram: large, label },
        result: { content: [{ type: 'text', text: large }], structuredContent: { label }, isError: false, _meta: { saved: large } },
      } },
      resource: { html: options.html ?? appHtml, ui: { csp } },
      sandbox_origin: sandboxOrigin,
      sandbox_url: `${sandboxOrigin}/artifacts/sandbox?${new URLSearchParams({ policy: JSON.stringify({ parent_origin: parentOrigin, csp }) })}`,
      viewer_path: `${options.path ?? '/work/artifacts'}/${label}`,
    } })
  })
  await page.route('**/api/artifacts/config', (route) => route.fulfill({ json: { path: options.path ?? '/work/artifacts' } }))
  return reads
}

async function contentFrame(page: Page, label = 'row-a'): Promise<Frame> {
  const frame = page.frameLocator(`iframe[title="${label}"]`).frameLocator('iframe')
  await expect(frame.locator('#result')).toHaveText(label)
  // Match the containing frame element rather than relying on load order.
  for (const candidate of page.frames().filter((one) => one.url() === 'about:srcdoc')) {
    if (await candidate.locator('#result').textContent() === label) return candidate
  }
  throw new Error(`Missing app frame: ${label}`)
}

test('two ledger cards initialize independently and receive complete input before result', async ({ page }) => {
  const reads = await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const a = await contentFrame(page)
  const b = await contentFrame(page, 'row-b')
  const messages = await a.evaluate<Array<{ method: string; params: { arguments: { diagram: string }; _meta: { saved: string }; content: { text: string }[]; isError: boolean } }>>("window.received.filter(m => m.method !== 'ui/notifications/host-context-changed')")
  expect(messages.map((message: { method: string }) => message.method)).toEqual(['ui/notifications/tool-input', 'ui/notifications/tool-result'])
  expect(messages[0].params.arguments.diagram).toBe(large)
  expect(messages[1].params._meta.saved).toBe(large)
  expect(messages[1].params.content[0].text).toBe(large)
  expect(messages[1].params.isError).toBe(false)
  expect(await b.evaluate("window.received.find(m => m.method === 'ui/notifications/tool-input').params.arguments.label")).toBe('row-b')
  expect(reads).toHaveLength(2)
})

for (const path of ['/artifacts', '/work/artifacts']) {
  test(`standalone ${path} route reloads the saved artifact and keeps its URL`, async ({ page }) => {
    const reads = await mockViews(page, { path })
    await page.goto(`${path}/app-a`)
    await contentFrame(page, 'app-a')
    await page.getByRole('button', { name: 'Reload', exact: true }).click()
    await contentFrame(page, 'app-a')
    await page.reload()
    await contentFrame(page, 'app-a')
    expect(page.url()).toBe(`${parentOrigin}${path}/app-a`)
    expect(reads.length).toBeGreaterThanOrEqual(3)
    expect(reads.every((url) => url.endsWith('/api/artifacts/app-a'))).toBe(true)
  })
}

test('resize is bounded, theme is updated, and removing cards tears down frames', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const frame = page.locator('iframe[title="row-a"]')
  await expect(frame).toHaveCSS('height', '480px')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 5000 })")
  await expect(frame).toHaveCSS('height', '640px')
  await expect(page.locator('#pm-row-a')).toHaveCSS('width', '960px')
  expect(await app.evaluate('window.host.hostContext.containerDimensions.maxHeight')).toBe(640)
  await page.setViewportSize({ width: 390, height: 600 })
  await expect(frame).toHaveCSS('height', '390px')
  expect(await frame.evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(390)
  await expect.poll(() => app.evaluate("window.received.filter(m => m.method === 'ui/notifications/host-context-changed').at(-1).params.containerDimensions.maxHeight")).toBe(390)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await expect(frame).toHaveCSS('height', '640px')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 0 })")
  await expect(frame).toHaveCSS('height', '100px')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(app.locator('body')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('button', { name: 'Remove cards' }).click()
  await expect(page.locator('iframe')).toHaveCount(0)
})

test('opaque iframe cannot read the console or proxy and CSP blocks undeclared fetches and frames', async ({ page }) => {
  await mockViews(page)
  const blocked: string[] = []
  page.on('request', (request) => { if (/forbidden|blocked\.test/.test(request.url())) blocked.push(request.url()) })
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  expect(await app.evaluate(() => {
    const blocked = []
    try { void parent.document; blocked.push(false) } catch { blocked.push(true) }
    try { void top!.document; blocked.push(false) } catch { blocked.push(true) }
    try { void localStorage; blocked.push(false) } catch { blocked.push(true) }
    return blocked
  })).toEqual([true, true, true])
  expect(await app.evaluate(`Promise.all(['${parentOrigin}/forbidden', 'http://blocked.test/forbidden'].map(url => fetch(url).then(() => false, () => true)))`)).toEqual([true, true])
  await app.evaluate(`const f = document.createElement('iframe'); f.src = 'http://blocked.test/frame'; document.body.append(f)`)
  await expect.poll(() => blocked.length).toBe(0)
  expect(await app.evaluate("window.host.hostCapabilities.sandbox.permissions")).toEqual({})
})

test('declared resources and connections work while reserved sandbox messages are discarded', async ({ page }) => {
  await mockViews(page, { csp: { resourceDomains: ['http://allowed.test'], connectDomains: ['http://allowed.test'] } })
  await page.route('http://allowed.test/**', (route) => route.fulfill({ headers: { 'Access-Control-Allow-Origin': '*' }, body: 'allowed' }))
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  expect(await app.evaluate("fetch('http://allowed.test/data').then(r => r.text())")).toBe('allowed')
  await app.evaluate("window.send('ui/notifications/sandbox-resource-ready', { html: '<p>spoofed</p>' }); window.send('ui/notifications/sandbox-proxy-ready', {})")
  await expect(app.locator('#result')).toHaveText('row-a')
  expect(await app.evaluate("window.received.filter(m => m.method !== 'ui/notifications/host-context-changed').length")).toBe(2)
})

test('unsupported app operations have no handlers and fullscreen remains inline', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const methods = ['tools/call', 'resources/read', 'ui/message', 'ui/update-model-context', 'ui/download-file', 'ui/open-link']
  for (const method of methods) await app.evaluate(([method]) => {
    parent.postMessage({ jsonrpc: '2.0', method, params: {}, id: method }, '*')
  }, [method])
  await expect.poll(() => app.evaluate('Object.keys(window.replies).length')).toBe(methods.length)
  for (const method of methods) expect(await app.evaluate(`window.replies[${JSON.stringify(method)}].error.code`)).toBe(-32601)
  await app.evaluate("window.send('ui/request-display-mode', { mode: 'fullscreen' }, 'full')")
  await expect.poll(() => app.evaluate('window.replies.full?.result.mode')).toBe('inline')
})

test('messages from another window or origin cannot resize a card', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  await contentFrame(page)
  await page.evaluate(() => {
    const source = document.querySelector('iframe')!.contentWindow
    const data = { jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 900 } }
    const spoof = new Event('message')
    Object.defineProperties(spoof, { source: { value: source }, origin: { value: 'https://wrong.test' }, data: { value: data } })
    window.dispatchEvent(spoof)
    window.postMessage(data, window.location.origin)
  })
  await expect(page.locator('iframe[title="row-a"]')).toHaveCSS('height', '480px')
})

test('resource errors stay visible without mounting a sandbox', async ({ page }) => {
  const reads = await mockViews(page, { fail: true })
  await page.goto('/tests/browser/harness.html')
  await expect(page.getByRole('alert')).toHaveCount(2)
  await expect(page.getByRole('alert').first()).toHaveText('Artifact installation unavailable')
  await expect(page.locator('iframe')).toHaveCount(0)
  expect(reads).toHaveLength(2)
})

test('an official SDK app receives design tokens at initialization and on theme changes', async ({ page }) => {
  const sdk = readFileSync(new URL(import.meta.resolve('@modelcontextprotocol/ext-apps/app-with-deps')), 'utf8')
  await mockViews(page, { csp: { resourceDomains: ['http://allowed.test'] }, html: `
    <!doctype html><style>
      body { background: var(--color-background-primary); color: var(--color-text-primary); font-family: var(--font-sans); }
      #result { background: var(--color-background-secondary); border: 1px solid var(--color-border-primary); border-radius: var(--border-radius-lg); }
    </style><div id="result"></div><script type="module">
    import { App, PostMessageTransport, applyHostStyleVariables, applyDocumentTheme } from 'http://allowed.test/sdk.js';
    const app = new App({ name: 'SDK fixture', version: '1' }, {});
    function applyStyles(ctx) {
      if (ctx.theme) applyDocumentTheme(ctx.theme);
      if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
    }
    app.onhostcontextchanged = applyStyles;
    app.addEventListener('toolinput', ({ arguments: args }) => { document.body.dataset.input = args.diagram });
    app.addEventListener('toolresult', (result) => { document.getElementById('result').textContent = result.structuredContent.label });
    app.onerror = error => { document.getElementById('result').textContent = error.message };
    await app.connect(new PostMessageTransport(window.parent, window.parent));
    applyStyles(app.getHostContext());
    </script>` })
  await page.route('http://allowed.test/sdk.js', (route) => route.fulfill({ contentType: 'text/javascript', headers: { 'Access-Control-Allow-Origin': '*' }, body: sdk }))
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  await expect(app.locator('body')).toHaveAttribute('data-input', large)
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await expect(app.locator('body')).toHaveCSS('color', 'rgb(75, 73, 66)')
  await expect(app.locator('#result')).toHaveCSS('border-radius', '0px')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(52, 49, 44)')
  await expect(app.locator('body')).toHaveCSS('color', 'rgb(236, 228, 212)')
  await expect(app.locator('#result')).toHaveCSS('background-color', 'rgb(38, 36, 32)')
  await expect(app.locator('#result')).toHaveCSS('border-color', 'rgba(83, 78, 69, 0.8)')
  await expect(app.locator('html')).toHaveAttribute('data-theme', 'dark')
  expect(await app.locator('body').evaluate(el => getComputedStyle(el).fontFamily)).toContain('Plus Jakarta Sans')
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
})

test('shared document theming preserves app interiors and standard size reports', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await mockViews(page, { html: appHtml.replace('<body>', `<body><style>
    body { margin: 0; padding: 8px; background: #111; }
    #result { height: 200px; background: #1f1f1f; }
    #toolbar { height: 32px; margin-top: 8px; background: var(--color-background-secondary); }
  </style><div id="toolbar">App toolbar</div>`) })
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const frame = page.locator('iframe[title="row-a"]')
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await expect(app.locator('html')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await expect(app.locator('#toolbar')).toHaveCSS('background-color', 'rgb(220, 210, 188)')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(52, 49, 44)')
  await expect(app.locator('body')).toHaveCSS('color', 'rgb(236, 228, 212)')
  await expect(app.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(app.locator('html')).toHaveCSS('color-scheme', 'dark')
  await expect(app.locator('#toolbar')).toHaveCSS('background-color', 'rgb(38, 36, 32)')
  await expect(app.locator('#result')).toHaveCSS('background-color', 'rgb(31, 31, 31)')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 600 })")
  await expect(frame).toHaveCSS('height', '600px')
  // The host uses the app's report, even when it exceeds the visible content.
  expect(await app.locator('body').evaluate(el => el.getBoundingClientRect().height)).toBe(256)
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 256 })")
  await expect(frame).toHaveCSS('height', '256px')
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await expect(frame).toHaveCSS('height', '256px')
})

test('shared theming accepts only context from the sandbox parent', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await app.evaluate(() => {
    const data = { jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: {
      theme: 'dark', styles: { variables: { '--color-background-primary': 'red' } },
    } }
    const spoof = new Event('message')
    Object.defineProperties(spoof, { source: { value: parent }, origin: { value: 'https://wrong.test' }, data: { value: data } })
    window.dispatchEvent(spoof)
    window.postMessage(data, '*')
  })
  // A round trip ensures the queued spoof was processed before checking styles.
  await app.evaluate("window.send('ui/request-display-mode', { mode: 'inline' }, 'roundtrip')")
  await expect.poll(() => app.evaluate('window.replies.roundtrip?.result.mode')).toBe('inline')
  await expect(app.locator('body')).toHaveCSS('background-color', 'rgb(237, 230, 214)')
  await expect(app.locator('html')).toHaveAttribute('data-theme', 'light')
})

test('the card frame stays above the iframe at each interface scale after resizing', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  await page.addStyleTag({ content: readFileSync('src/styles/console.css', 'utf8') })
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  const app = await contentFrame(page)
  const card = page.locator('#pm-row-a .trk-artifact')
  for (const size of ['small', 'medium', 'large']) {
    await page.evaluate(size => { document.documentElement.dataset.interfaceSize = size }, size)
    await app.evaluate("window.send('ui/notifications/size-changed', { height: 256 })")
    await expect(page.locator('iframe[title="row-a"]')).toHaveCSS('height', '256px')
    const frame = await card.evaluate(el => {
      const outline = getComputedStyle(el, '::after')
      const rect = el.getBoundingClientRect()
      const content = el.querySelector('iframe')!.getBoundingClientRect()
      return {
        outline: outline.outlineStyle, color: outline.outlineColor,
        pointerEvents: outline.pointerEvents,
        bottomGap: rect.bottom - content.bottom,
        edge: document.elementFromPoint(rect.x + rect.width / 2, rect.bottom - 0.5) === el,
      }
    })
    expect(frame.outline).toBe('solid')
    expect(frame.color).toBe('rgba(83, 78, 69, 0.8)')
    expect(frame.pointerEvents).toBe('none')
    expect(frame.bottomGap).toBeGreaterThanOrEqual(0.9)
    expect(frame.edge).toBe(true)
    await card.screenshot({ path: test.info().outputPath(`artifact-${size}.png`) })
  }
  await page.locator('#pm-row-a').getByRole('button', { name: 'Reload', exact: true }).click()
  await contentFrame(page)
})

test('a missing handshake becomes a visible loading error', async ({ page }) => {
  await page.clock.install()
  await mockViews(page, { html: '<p>No app handshake</p>' })
  await page.goto('/tests/browser/harness.html')
  await expect(page.locator('iframe')).toHaveCount(2)
  await page.clock.fastForward(16000)
  await expect(page.getByRole('alert')).toHaveCount(2)
  await expect(page.getByRole('alert').first()).toHaveText('The artifact did not finish loading.')
  await expect(page.locator('iframe')).toHaveCount(0)
})
