import { test, expect } from '@playwright/test'

for (const width of [1280, 390]) {
  test(`math loads on demand, respects prose and code, and renders safely at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    const mathRequests: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => { if (/\/components\/math|\/katex/.test(request.url())) mathRequests.push(request.url()) })
    await page.goto('/tests/browser/markdown.html')
    await expect(page.getByRole('button', { name: 'Copy code', exact: true })).toBeVisible()
    expect(mathRequests).toEqual([])
    await page.getByRole('button', { name: 'Stream math', exact: true }).click()
    await expect(page.locator('.trk-md')).toContainText('Working $2^')
    await expect(page.locator('.katex')).toHaveCount(0)
    await page.getByRole('button', { name: 'Math', exact: true }).click()
    await expect(page.locator('.katex')).toHaveCount(4)
    await expect(page.locator('math')).toHaveCount(4)
    await expect(page.locator('.katex-display')).toHaveCount(2)
    await expect(page.locator('.trk-md')).toContainText('Prices are $5 and $10.')
    await expect(page.locator('.trk-md p > code')).toHaveText('$x^2$')
    await expect(page.locator('.trk-code-block code')).toHaveText('$x^2$ and \\(x\\)')
    expect(mathRequests.length).toBeGreaterThan(0)
    let light = ''
    for (const theme of ['Light', 'Dark']) {
      await page.getByRole('button', { name: theme, exact: true }).click()
      await page.evaluate(async () => { await document.fonts.ready })
      const color = await page.locator('.katex').first().evaluate(el => getComputedStyle(el).color)
      if (theme === 'Light') light = color
      else expect(color).not.toBe(light)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: `/private/tmp/inky-math-${width}-${theme.toLowerCase()}.png` })
    }
    await page.getByRole('button', { name: 'Unsafe math', exact: true }).click()
    await expect(page.locator('.katex-error')).toBeVisible()
    await expect(page.locator('.trk-md img, .trk-md a, .trk-md script, .trk-md [onerror], .trk-md [onclick]')).toHaveCount(0)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
