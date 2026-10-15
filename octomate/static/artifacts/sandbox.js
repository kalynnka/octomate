// This document is trusted; the app is confined to the opaque-origin child.
const parentOrigin = document.documentElement.dataset.parentOrigin
const policy = document.documentElement.dataset.csp
const reserved = 'ui/notifications/sandbox-'
let frame

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
      frame.srcdoc = `<!doctype html><html><head>${meta.outerHTML}</head><body>${message.params.html}</body></html>`
      document.body.append(frame)
    } else if (!sandboxMessage) {
      frame?.contentWindow.postMessage(message, '*')
    }
  } else if (frame && event.source === frame.contentWindow && event.origin === 'null' && !sandboxMessage) {
    window.parent.postMessage(message, parentOrigin)
  }
})

window.parent.postMessage({ jsonrpc: '2.0', method: `${reserved}proxy-ready`, params: {} }, parentOrigin)
