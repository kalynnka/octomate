import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Octomate FastAPI server (127.0.0.1:8000) ships no CORS middleware, so the
// dev server proxies same-origin paths straight through to it.
const apiUrl = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  define: { 'import.meta.env.VITE_API_URL': JSON.stringify(apiUrl) },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // Bind the IPv4 loopback explicitly: `localhost` resolves to `::1` first on
    // current Node, and a `tailscale serve` proxy to 127.0.0.1 then gets
    // nothing. Requests that arrive that way carry the tailnet hostname.
    host: '127.0.0.1',
    allowedHosts: ['.ts.net'],
    proxy: {
      '/api': apiUrl,
      '/oauth': apiUrl,
    },
  },
})
