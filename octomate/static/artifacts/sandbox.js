// This document is trusted; the app is confined to the opaque-origin child.
const parentOrigin = document.documentElement.dataset.parentOrigin
const policy = document.documentElement.dataset.csp
const reserved = 'ui/notifications/sandbox-'
let frame

/** Runs inside the opaque child, applying host context without changing app APIs or sizing. */
function applyHostTheme(sandboxOrigin, contentKind) {
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.origin !== sandboxOrigin) return
    const message = event.data
    if (message?.jsonrpc !== '2.0') return
    const context = message.method === 'ui/notifications/host-context-changed'
      ? message.params : message.result?.hostContext
    if (!context) return
    const root = document.documentElement
    if (context.theme === 'light' || context.theme === 'dark') {
      root.dataset.theme = context.theme
      root.style.colorScheme = context.theme
    }
    for (const [name, value] of Object.entries(context.styles?.variables ?? {})) {
      if (name.startsWith('--') && typeof value === 'string') root.style.setProperty(name, value)
    }
    if (contentKind === 'html') return
    for (const element of [root, document.body]) {
      element.style.backgroundColor = 'var(--color-background-primary)'
      element.style.color = 'var(--color-text-primary)'
    }
  })
}

/** Give plain HTML the lifecycle and size reporting normally supplied by an MCP app. */
function initializeHtml(sandboxOrigin) {
  const defaults = document.createElement('style')
  defaults.textContent = ':where(html) { background-color: var(--color-background-primary); color: var(--color-text-primary); }'
  document.head.append(defaults)
  const send = (method, params, id) => window.parent.postMessage({ jsonrpc: '2.0', method, params, ...(id ? { id } : {}) }, sandboxOrigin)
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.origin !== sandboxOrigin) return
    const message = event.data
    if (message?.jsonrpc !== '2.0') return
    if (message.id === 'html-init' && message.result) {
      send('ui/notifications/initialized', {})
      const resize = new ResizeObserver(() => {
        const body = document.body
        const bounds = body.getBoundingClientRect()
        const border = body.offsetHeight - body.clientHeight
        const height = bounds.top + Math.max(bounds.height, body.scrollHeight + border) + parseFloat(getComputedStyle(body).marginBottom)
        send('ui/notifications/size-changed', { height })
      })
      resize.observe(document.body, { box: 'border-box' })
    } else if (message.method === 'ui/resource-teardown') {
      window.parent.postMessage({ jsonrpc: '2.0', id: message.id, result: {} }, sandboxOrigin)
    }
  })
  window.addEventListener('load', () => send('ui/initialize', {
    protocolVersion: '2026-01-26', appInfo: { name: 'HTML visualization', version: '1' }, appCapabilities: {},
  }, 'html-init'), { once: true })
}

window.addEventListener('message', (event) => {
  const message = event.data
  if (!message || message.jsonrpc !== '2.0') return
  const sandboxMessage = typeof message.method === 'string' && message.method.startsWith(reserved)
  if (event.source === window.parent && event.origin === parentOrigin) {
    if (message.method === `${reserved}resource-ready`) {
      if (frame || typeof message.params?.html !== 'string') return
      frame = document.createElement('iframe')
      frame.title = 'Artifact content'
      frame.setAttribute('sandbox', 'allow-scripts')
      frame.setAttribute('allow', "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'; fullscreen 'none'")
      frame.referrerPolicy = 'no-referrer'
      const meta = document.createElement('meta')
      meta.httpEquiv = 'Content-Security-Policy'
      meta.content = policy
      const theme = document.createElement('script')
      theme.textContent = `(${applyHostTheme.toString()})(${JSON.stringify(window.location.origin)}, ${JSON.stringify(document.documentElement.dataset.contentKind)})`
      if (document.documentElement.dataset.contentKind === 'html') {
        theme.textContent += `;(${initializeHtml.toString()})(${JSON.stringify(window.location.origin)})`
      }
      frame.srcdoc = `<!doctype html><html><head>${meta.outerHTML}${theme.outerHTML}</head><body>${message.params.html}</body></html>`
      document.body.append(frame)
    } else if (!sandboxMessage) {
      frame?.contentWindow.postMessage(message, '*')
    }
  } else if (frame && event.source === frame.contentWindow && event.origin === 'null' && !sandboxMessage) {
    window.parent.postMessage(message, parentOrigin)
  }
})

window.parent.postMessage({ jsonrpc: '2.0', method: `${reserved}proxy-ready`, params: {} }, parentOrigin)
