import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Octomate FastAPI server (127.0.0.1:8000) ships no CORS middleware, so the
// dev server proxies same-origin paths straight through to it.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/oauth': 'http://127.0.0.1:8000',
    },
  },
})
