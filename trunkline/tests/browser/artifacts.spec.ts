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
    console.info('Fixture teardown', document.querySelector('#result').textContent);
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

test('folding preserves app state and hides only that card until it is unfolded', async ({ page }) => {
  const reads = await mockViews(page, { html: appHtml.replace('<body>', '<body><input aria-label="Diagram note">') })
  await page.goto('/tests/browser/harness.html')
  await page.addStyleTag({ content: readFileSync('src/styles/motion.css', 'utf8') })
  const app = await contentFrame(page)
  const other = await contentFrame(page, 'row-b')
  const card = page.locator('#pm-row-a')
  const fold = card.getByRole('button', { name: 'Fold', exact: true })
  const contentId = await fold.getAttribute('aria-controls')
  const buttonWidth = await fold.evaluate(el => el.getBoundingClientRect().width)
  for (const control of [card.getByRole('link', { name: 'Open', exact: true }), card.getByRole('button', { name: 'Reload', exact: true })]) {
    expect(await control.evaluate(el => el.getBoundingClientRect().width)).toBeCloseTo(buttonWidth, 1)
  }
  await app.getByRole('textbox', { name: 'Diagram note' }).fill('Unsaved edit')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 300 })")
  await fold.press('Enter')
  await expect(card.getByRole('button', { name: 'Unfold', exact: true })).toHaveAttribute('aria-expanded', 'false')
  expect(await card.getByRole('button', { name: 'Unfold', exact: true }).evaluate(el => el.getBoundingClientRect().width)).toBeCloseTo(buttonWidth, 1)
  await expect(page.locator(`[id="${contentId}"]`)).toHaveAttribute('inert', '')
  await expect(page.locator(`[id="${contentId}"]`)).toHaveAttribute('aria-hidden', 'true')
  await expect.poll(() => card.evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(60)
  await expect(card.locator('iframe')).toHaveCount(1)
  await expect(other.locator('#result')).toBeVisible()
  await expect(page.locator('#pm-row-b').getByRole('button', { name: 'Fold', exact: true })).toHaveAttribute('aria-expanded', 'true')
  await app.evaluate(`window.send('ui/request-display-mode', { mode: 'fullscreen' }, 'folded-fullscreen');
    window.send('ui/open-link', { url: 'https://external.test/' }, 'folded-link');
    window.send('ui/download-file', { contents: [{ type: 'resource', resource: { uri: 'file:///hidden.txt', text: 'hidden' } }] }, 'folded-download')`)
  await expect.poll(() => app.evaluate("window.replies['folded-fullscreen']?.result.mode")).toBe('inline')
  await expect.poll(() => app.evaluate("window.replies['folded-link']?.result.isError")).toBe(true)
  await expect.poll(() => app.evaluate("window.replies['folded-download']?.result.isError")).toBe(true)
  await card.screenshot({ path: test.info().outputPath('artifact-folded.png') })
  await card.getByRole('button', { name: 'Unfold', exact: true }).click()
  await expect(card.getByRole('button', { name: 'Fold', exact: true })).toHaveAttribute('aria-expanded', 'true')
  await expect(app.getByRole('textbox', { name: 'Diagram note' })).toBeVisible()
  await expect(app.getByRole('textbox', { name: 'Diagram note' })).toHaveValue('Unsaved edit')
  await expect(card.locator('iframe')).toHaveCSS('height', '300px')
  expect(await app.evaluate("window.received.filter(m => m.method === 'ui/notifications/tool-result').length")).toBe(1)
  expect(await app.evaluate('document.body.dataset.teardown')).toBeUndefined()
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
  await expect(frame).toHaveCSS('height', '1000px')
  await expect(page.locator('#pm-row-a')).toHaveCSS('max-width', '100%')
  expect(await app.evaluate('window.host.hostContext.containerDimensions.maxHeight')).toBe(1000)
  await page.setViewportSize({ width: 390, height: 600 })
  await expect(frame).toHaveCSS('height', '1000px')
  expect(await frame.evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(390)
  await expect.poll(() => app.evaluate("window.received.filter(m => m.method === 'ui/notifications/host-context-changed').at(-1).params.containerDimensions.maxHeight")).toBe(1000)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await expect(frame).toHaveCSS('height', '1000px')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 0 })")
  await expect(frame).toHaveCSS('height', '100px')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(app.locator('body')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('button', { name: 'Remove cards' }).click()
  await expect(page.locator('iframe')).toHaveCount(0)
})

test('a tall second diagram grows independently without being limited by the window height', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 740 })
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const first = await contentFrame(page)
  const second = await contentFrame(page, 'row-b')
  await first.evaluate("window.send('ui/notifications/size-changed', { height: 256 })")
  await second.evaluate("window.send('ui/notifications/size-changed', { height: 950 })")
  await expect(page.locator('iframe[title="row-a"]')).toHaveCSS('height', '256px')
  await expect(page.locator('iframe[title="row-b"]')).toHaveCSS('height', '950px')
  await second.evaluate("window.send('ui/notifications/size-changed', { height: 480 })")
  await expect(page.locator('iframe[title="row-b"]')).toHaveCSS('height', '480px')
})

test('artifact resize and folding keep the reader on the current card', async ({ page }) => {
  await mockViews(page, { html: appHtml.replace('</body>', `<button id="resize" onclick="window.expanded = !window.expanded; document.querySelector('#result').style.height = window.expanded ? '850px' : '200px'; window.send('ui/notifications/size-changed', { height: document.body.offsetHeight })">Resize diagram</button>
    <style>body { margin: 0; } #result { height: 200px; }</style></body>`) })
  await page.goto('/tests/browser/harness.html')
  await page.addStyleTag({ content: readFileSync('src/styles/motion.css', 'utf8') + `
    #trk-chatlog { height: 600px; overflow-y: auto; zoom: 0.9; }
    #trk-chatlog::before, #trk-chatlog::after { content: ''; display: block; height: 1000px; }
  ` })
  const app = await contentFrame(page)
  await contentFrame(page, 'row-b')
  const card = page.locator('#pm-row-a')
  const log = page.locator('#trk-chatlog')
  await card.evaluate(async el => { await Promise.all(el.getAnimations().map(animation => animation.finished)) })
  await log.evaluate(el => { el.scrollTop = 940 })
  const top = await card.evaluate(el => el.getBoundingClientRect().top)
  await app.getByRole('button', { name: 'Resize diagram' }).click()
  await expect.poll(() => card.evaluate(el => el.clientHeight)).toBeGreaterThan(850)
  expect(await card.evaluate(el => el.getBoundingClientRect().top)).toBeCloseTo(top, 0)
  await card.getByRole('button', { name: 'Fold', exact: true }).click()
  await expect.poll(() => card.evaluate(el => el.clientHeight)).toBeLessThan(60)
  expect(await card.evaluate(el => el.getBoundingClientRect().top)).toBeCloseTo(top, 0)
  await card.getByRole('button', { name: 'Unfold', exact: true }).click()
  await expect.poll(() => card.evaluate(el => el.clientHeight)).toBeGreaterThan(850)
  expect(await card.evaluate(el => el.getBoundingClientRect().top)).toBeCloseTo(top, 0)
  await app.getByRole('button', { name: 'Resize diagram' }).scrollIntoViewIfNeeded()
  await app.getByRole('button', { name: 'Resize diagram' }).click()
  await expect.poll(() => card.evaluate(el => el.clientHeight)).toBeLessThan(300)
  expect(await card.evaluate(el => el.getBoundingClientRect().bottom)).toBeGreaterThan(await log.evaluate(el => el.getBoundingClientRect().top + 40))
})

test('artifact motion animates the host frame and respects reduced motion', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const card = page.locator('#pm-row-a')
  const frame = card.locator('.trk-artifact-frame')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 300 })")
  await expect(frame).toHaveCSS('height', '300px')
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 800 })")
  await expect(card.locator('iframe')).toHaveCSS('height', '800px')
  const motion = await frame.evaluate(el => ({
    height: el.getBoundingClientRect().height,
    duration: el.getAnimations()[0]?.effect?.getComputedTiming().duration,
  }))
  expect(motion.height).toBeGreaterThan(300)
  expect(motion.height).toBeLessThan(800)
  expect(motion.duration).toBe(380)
  await expect(frame).toHaveCSS('height', '800px')
  await expect(card.locator('.lt-fold')).toHaveCSS('transition-duration', '0.38s')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await app.evaluate("window.send('ui/notifications/size-changed', { height: 300 })")
  await expect(frame).toHaveCSS('height', '300px')
  expect(await frame.evaluate(el => el.getAnimations().length)).toBe(0)
  await card.getByRole('button', { name: 'Fold', exact: true }).click()
  expect(await card.evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(60)
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

test('server, conversation, and sampling operations remain unavailable', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const methods = ['tools/list', 'tools/call', 'resources/list', 'resources/templates/list', 'resources/read', 'prompts/list', 'ui/message', 'ui/update-model-context', 'sampling/createMessage']
  for (const method of methods) await app.evaluate(([method]) => {
    parent.postMessage({ jsonrpc: '2.0', method, params: {}, id: method }, '*')
  }, [method])
  await expect.poll(() => app.evaluate('Object.keys(window.replies).length')).toBe(methods.length)
  for (const method of methods) expect(await app.evaluate(`window.replies[${JSON.stringify(method)}].error.code`)).toBe(-32601)
  expect(await app.evaluate('Object.keys(window.host.hostCapabilities).sort()')).toEqual(['downloadFile', 'logging', 'openLinks', 'sandbox'])
})

test('links open outside the sandbox without an opener or referrer', async ({ page, context }) => {
  await mockViews(page)
  await context.route('https://external.test/**', route => route.fulfill({ body: '<title>External</title>' }))
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const opened = page.waitForEvent('popup')
  await app.evaluate("window.send('ui/open-link', { url: 'https://external.test/diagram#saved' }, 'link')")
  const popup = await opened
  await popup.waitForURL('https://external.test/diagram#saved')
  expect(await popup.evaluate('window.opener')).toBeNull()
  expect(await popup.evaluate('document.referrer')).toBe('')
  await expect.poll(() => app.evaluate('window.replies.link?.result')).toEqual({})
  await popup.close()
  for (const url of ['javascript:alert(1)', 'data:text/html,unsafe', 'file:///tmp/test', 'https://user:secret@external.test/', `${parentOrigin}/api/`, `${sandboxOrigin}/artifacts/sandbox`]) {
    await app.evaluate(url => { parent.postMessage({ jsonrpc: '2.0', method: 'ui/open-link', params: { url }, id: url }, '*') }, url)
    await expect.poll(() => app.evaluate(url => Reflect.get(window, 'replies')[url]?.result?.isError, url)).toBe(true)
  }
  await expect(app.locator('#result')).toHaveText('row-a')
})

test('a blocked popup offers a host link and cancellation stays within its card', async ({ page, context }) => {
  await mockViews(page)
  await context.route('https://external.test/**', route => route.fulfill({ body: '<title>External</title>' }))
  await page.goto('/tests/browser/harness.html')
  const a = await contentFrame(page)
  const b = await contentFrame(page, 'row-b')
  await page.evaluate(() => { window.open = () => null })
  await a.evaluate("window.send('ui/open-link', { url: 'https://external.test/a' }, 'link')")
  await b.evaluate("window.send('ui/open-link', { url: 'https://external.test/b' }, 'link')")
  await page.locator('#pm-row-a').getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect.poll(() => a.evaluate('window.replies.link?.result.isError')).toBe(true)
  const opened = page.waitForEvent('popup')
  await page.locator('#pm-row-b').getByRole('link', { name: 'Open external.test', exact: true }).click()
  const popup = await opened
  await popup.waitForURL('https://external.test/b')
  expect(await popup.evaluate('window.opener')).toBeNull()
  await expect.poll(() => b.evaluate('window.replies.link?.result.isError')).toBe(false)
  await popup.close()
  await expect(page.getByRole('group', { name: 'Artifact browser action' })).toHaveCount(0)
})

test('embedded text and binary downloads require host clicks and preserve their bytes', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  await app.evaluate(`window.send('ui/download-file', { contents: [
    { type: 'resource', resource: { uri: 'file:///exports/hello.txt', mimeType: 'text/plain', text: 'Hello, 世界!' } },
    { type: 'resource', resource: { uri: 'file:///exports/%2E%2E%2Fdiagram.bin', mimeType: 'application/octet-stream', blob: 'AAECA/8=' } }
  ] }, 'download')`)
  expect(await app.evaluate('window.replies.download')).toBeUndefined()
  for (const [name, bytes] of [['hello.txt', Buffer.from('Hello, 世界!')], ['_diagram.bin', Buffer.from([0, 1, 2, 3, 255])]] as const) {
    const downloading = page.waitForEvent('download')
    await page.getByRole('link', { name: `Download ${name}`, exact: true }).click()
    const download = await downloading
    expect(download.suggestedFilename()).toBe(name)
    const saved = await download.path()
    expect(readFileSync(saved!)).toEqual(bytes)
  }
  await expect.poll(() => app.evaluate('window.replies.download?.result.isError')).toBe(false)
  await expect(page.getByRole('group', { name: 'Artifact browser action' })).toHaveCount(0)
})

test('invalid, linked, oversized, and cancelled downloads produce errors without fetching', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const fetched: string[] = []
  page.on('request', request => { if (request.url().includes('external.test')) fetched.push(request.url()) })
  for (const [id, contents] of [
    ['linked', [{ type: 'resource_link', uri: 'https://external.test/file', name: 'file' }]],
    ['invalid', [{ type: 'resource', resource: { uri: 'file:///bad', blob: '!invalid!' } }]],
    ['empty', []],
  ] as const) {
    await app.evaluate(([id, contents]) => parent.postMessage({ jsonrpc: '2.0', method: 'ui/download-file', params: { contents }, id }, '*'), [id, contents])
    await expect.poll(() => app.evaluate(id => {
      const reply = Reflect.get(window, 'replies')[id]
      return reply?.result?.isError === true || reply?.error?.code === -32602
    }, id)).toBe(true)
  }
  await app.evaluate("window.send('ui/download-file', { contents: [{ type: 'resource', resource: { uri: 'file:///large.txt', text: 'x'.repeat(20 * 1024 * 1024 + 1) } }] }, 'large')")
  await expect.poll(() => app.evaluate('window.replies.large?.result.isError')).toBe(true)
  await app.evaluate("window.send('ui/download-file', { contents: [{ type: 'resource', resource: { uri: 'file:///cancel.txt', text: 'cancel' } }] }, 'cancel')")
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect.poll(() => app.evaluate('window.replies.cancel?.result.isError')).toBe(true)
  expect(fetched).toEqual([])
  await expect(app.locator('#result')).toHaveText('row-a')
})

test('fullscreen and Escape preserve the iframe, saved replay, and inline size', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  const reads = await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  await page.addStyleTag({ content: readFileSync('src/styles/console.css', 'utf8') })
  await page.addStyleTag({ content: readFileSync('src/styles/motion.css', 'utf8') })
  const app = await contentFrame(page)
  await app.evaluate("window.kept = 'transient'; window.send('ui/notifications/size-changed', { height: 256 }); window.send('ui/request-display-mode', { mode: 'fullscreen' }, 'full')")
  await expect.poll(() => app.evaluate('window.replies.full?.result.mode')).toBe('fullscreen')
  const dialog = page.getByRole('dialog', { name: 'row-a', exact: true })
  expect(await dialog.evaluate(el => el.matches(':modal'))).toBe(true)
  const frame = page.locator('iframe[title="row-a"]')
  const bounds = (await frame.boundingBox())!
  expect(bounds.height).toBeGreaterThan(600)
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(1281)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(901)
  await dialog.screenshot({ path: test.info().outputPath('artifact-fullscreen.png') })
  await app.evaluate("window.send('ui/request-display-mode', { mode: 'pip' }, 'pip')")
  await expect.poll(() => app.evaluate('window.replies.pip?.result.mode')).toBe('fullscreen')
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveAttribute('data-display-mode', 'inline')
  await expect(frame).toHaveCSS('height', '256px')
  expect(await app.evaluate('window.kept')).toBe('transient')
  expect(await app.evaluate("window.received.filter(m => m.method === 'ui/notifications/tool-result').length")).toBe(1)
  await expect.poll(() => app.evaluate("window.received.filter(m => m.method === 'ui/notifications/host-context-changed').at(-1).params.displayMode")).toBe('inline')
  expect(reads).toHaveLength(2)
  await app.evaluate("window.send('ui/request-display-mode', { mode: 'fullscreen' }, 'again')")
  await page.getByRole('button', { name: 'Back to inline', exact: true }).click()
  await expect(frame).toHaveCSS('height', '256px')
})

test('display modes respect the app declaration', async ({ page }) => {
  await mockViews(page, { html: appHtml.replace('appCapabilities: {}', "appCapabilities: { availableDisplayModes: ['inline'] }") })
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  await app.evaluate("window.send('ui/request-display-mode', { mode: 'fullscreen' }, 'full')")
  await expect.poll(() => app.evaluate('window.replies.full?.result.mode')).toBe('inline')
})

test('logs identify the artifact and app teardown closes only its own card', async ({ page }) => {
  const logs: string[] = []
  const teardowns: string[] = []
  page.on('console', message => { if (message.type() === 'warning') logs.push(message.text()) })
  page.on('console', message => { if (message.text().startsWith('Fixture teardown')) teardowns.push(message.text()) })
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const a = await contentFrame(page)
  const b = await contentFrame(page, 'row-b')
  await a.evaluate("window.send('notifications/message', { level: 'warning', logger: 'canvas', data: 'Sample warning' })")
  await expect.poll(() => logs).toContain('[Artifact row-a / canvas] Sample warning')
  await a.evaluate("window.send('ui/notifications/request-teardown', {})")
  await expect(page.locator('#pm-row-a').getByRole('status')).toHaveText('Artifact closed. Reload to reopen.')
  await expect(page.locator('iframe[title="row-a"]')).toHaveCount(0)
  expect(teardowns).toEqual(['Fixture teardown row-a'])
  await expect(b.locator('#result')).toHaveText('row-b')
  await page.locator('#pm-row-a').getByRole('button', { name: 'Reload', exact: true }).click()
  await contentFrame(page)
})

test('request cancellation and card removal clear pending browser actions', async ({ page }) => {
  await mockViews(page)
  await page.goto('/tests/browser/harness.html')
  const app = await contentFrame(page)
  const download = "window.send('ui/download-file', { contents: [{ type: 'resource', resource: { uri: 'file:///pending.txt', text: 'pending' } }] }, 'pending')"
  await app.evaluate(download)
  await expect(page.getByRole('link', { name: 'Download pending.txt', exact: true })).toBeVisible()
  await app.evaluate("window.send('notifications/cancelled', { requestId: 'pending', reason: 'User cancelled' })")
  await expect(page.getByRole('group', { name: 'Artifact browser action' })).toHaveCount(0)
  await app.evaluate(download)
  await expect(page.getByRole('link', { name: 'Download pending.txt', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Remove cards', exact: true }).click()
  await expect(page.getByRole('group', { name: 'Artifact browser action' })).toHaveCount(0)
  await expect(page.locator('iframe')).toHaveCount(0)
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
