// This document is trusted; the app is confined to the opaque-origin child.
const parentOrigin = document.documentElement.dataset.parentOrigin
const policy = document.documentElement.dataset.csp
const reserved = 'ui/notifications/sandbox-'
let frame

/** Runs inside the opaque child, applying host context without changing app APIs or sizing. */
function applyHostTheme(sandboxOrigin) {
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
    for (const element of [root, document.body]) {
      element.style.backgroundColor = 'var(--color-background-primary)'
      element.style.color = 'var(--color-text-primary)'
    }
  })
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
      theme.textContent = `(${applyHostTheme.toString()})(${JSON.stringify(window.location.origin)})`
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
