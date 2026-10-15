import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  use: { browserName: 'firefox', headless: true, baseURL: 'http://127.0.0.1:5174' },
  webServer: [
    { command: 'pnpm dev --port 5174 --strictPort', url: 'http://127.0.0.1:5174', reuseExistingServer: false },
    {
      command: '../.venv/bin/python -m uvicorn tests.support.artifact_browser:app --app-dir .. --host 127.0.0.1 --port 8765',
      url: 'http://127.0.0.1:8765/artifacts/sandbox.js', reuseExistingServer: false,
      env: { OCTOMATE_DB_URL: 'sqlite+aiosqlite:///:memory:', OCTOMATE_HOME: '../tests/config' },
    },
  ],
})
