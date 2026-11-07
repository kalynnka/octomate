import { test, expect } from '@playwright/test'

for (const width of [1280, 390]) {
  test(`tool JSON and diffs stay readable without changing plain output at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.goto('/tests/browser/markdown.html')
    await expect(page).toHaveTitle('Message rendering test')
    await page.getByRole('button', { name: 'Tool details', exact: true }).click()
    const tool = page.locator('#pm-formatted-tool')
    await expect(tool.locator('.trk-tool-toggle > svg rect')).toHaveCount(1)
    await expect(tool.locator('.trk-tool-toggle > svg polyline')).toHaveAttribute('points', '7 8 11 12 7 16')
    await expect(page.locator('#pm-partial-tool .trk-tool-toggle > svg polyline')).toHaveCount(2)
    const args = tool.locator('pre').first()
    const result = tool.locator('pre').last()
    await expect(args.locator('.hljs-attr').first()).toHaveText('"command"')
    const source = await args.locator('code').textContent()
    expect(source).toContain('\n  "command":')
    expect(source).toContain('\n    {\n      "type": "unknown"\n    }\n')
    expect(JSON.parse(source!).options).toEqual({ staged: true, limit: 3 })
    await expect(result.locator('.hljs-addition')).toHaveCount(2)
    await expect(result.locator('.hljs-deletion')).toHaveText('-  return raw')
    await expect(result.locator('.hljs-meta')).toHaveText('@@ -10,3 +10,4 @@')
    await expect(result).toContainText('<script>literal</script>')
    await expect(tool.locator('script')).toHaveCount(0)
    const partial = page.locator('#pm-partial-tool pre').first()
    expect(await partial.textContent()).toBe('{"command":"unfinished …')
    await expect(partial.locator('[class^="hljs-"]')).toHaveCount(0)
    for (const theme of ['Light', 'Dark']) {
      await page.getByRole('button', { name: theme, exact: true }).click()
      const textColor = await args.evaluate(el => getComputedStyle(el).color)
      await expect(args.locator('.hljs-attr').first()).not.toHaveCSS('color', textColor)
      const addedColor = await result.locator('.hljs-addition').first().evaluate(el => getComputedStyle(el).color)
      await expect(result.locator('.hljs-deletion')).not.toHaveCSS('color', addedColor)
      const contrast = await tool.locator('[class^="hljs-"]').evaluateAll(spans => {
        const context = document.createElement('canvas').getContext('2d')!
        const luminance = (color: string) => {
          context.fillStyle = color
          context.fillRect(0, 0, 1, 1)
          return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).reduce((sum, value, index) => {
            const channel = value / 255
            return sum + (channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][index]
          }, 0)
        }
        return Math.min(...spans.map(span => {
          const background = luminance(getComputedStyle(span.closest('.trk-trace-row')!).backgroundColor)
          const foreground = luminance(getComputedStyle(span).color)
          return (Math.max(background, foreground) + .05) / (Math.min(background, foreground) + .05)
        }))
      })
      expect(contrast).toBeGreaterThanOrEqual(4.5)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await expect(args).toHaveCSS('white-space', 'pre-wrap')
      await expect(result).toHaveCSS('white-space', 'pre')
      await tool.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-tool-details-${width}-${theme.toLowerCase()}.png` })
    }
    if (width === 390) {
      expect(await result.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
      await result.focus()
      await expect(result).toBeFocused()
      await page.keyboard.press('ArrowRight', { delay: 100 })
      await expect.poll(() => result.evaluate(el => el.scrollLeft)).toBeGreaterThan(0)
    }
    await page.getByRole('button', { name: 'JSON result', exact: true }).click()
    await expect(result).toHaveAttribute('data-language', 'json')
    await expect(result.locator('.hljs-literal')).toHaveText('true')
    expect(await result.textContent()).toContain('\n  "files": [\n    "cards.tsx",\n    "console.css"\n  ]\n')
    await page.getByRole('button', { name: 'Plain result', exact: true }).click()
    await expect(result).not.toHaveAttribute('data-language')
    expect(await result.textContent()).toBe('  <script>literal</script>\n    - a plain log line')
    await expect(result.locator('[class^="hljs-"]')).toHaveCount(0)
    await expect(tool.locator('script')).toHaveCount(0)
    await tool.locator('.trk-tool-toggle').click()
    await expect(tool.locator('.trk-tool-toggle')).toHaveAttribute('aria-expanded', 'false')
    await tool.locator('.trk-tool-toggle').click()
    await expect(args.locator('.hljs-attr').first()).toHaveText('"command"')
    expect(await args.locator('code').textContent()).toBe(source)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
