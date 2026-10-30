import { test, expect } from '@playwright/test'

for (const width of [1280, 390]) {
  test(`message diagrams and code colors work in both themes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    const diagramRequests: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()) })
    page.on('request', request => { if (request.url().includes('/mermaid')) diagramRequests.push(request.url()) })
    await page.goto('/tests/browser/markdown.html')
    await expect(page).toHaveTitle('Message rendering test')
    await expect(page.locator('.hljs-keyword').first()).toHaveText('import')
    await expect(page.locator('code').first()).toHaveText('demo_worker.py')
    expect(diagramRequests).toEqual([])
    await page.getByRole('button', { name: 'Stream', exact: true }).click()
    await expect(page.locator('.trk-mermaid code')).toContainText('Core -->')
    await expect(page.locator('.trk-print-caret')).toHaveCount(1)
    await expect(page.locator('.trk-mermaid svg')).toHaveCount(0)
    expect(diagramRequests).toEqual([])
    await page.getByRole('button', { name: 'Finish', exact: true }).click()
    await expect(page.locator('.trk-mermaid svg')).toBeVisible()
    await expect(page.locator('.trk-mermaid')).toContainText('Alice worker')
    await expect(page.locator('.trk-print-caret')).toHaveCount(0)
    await page.getByRole('button', { name: 'Invalid', exact: true }).click()
    await expect(page.getByRole('status')).toHaveText('Unable to render this Mermaid diagram.')
    await expect(page.locator('.trk-mermaid code')).toContainText('A --> [')
    await page.getByRole('button', { name: 'Unsafe', exact: true }).click()
    await expect(page.locator('.trk-mermaid svg')).toBeVisible()
    await expect(page.locator('.trk-md script, .trk-md img, .trk-md [onerror], .trk-md [onclick], .trk-md [href^="javascript:"]')).toHaveCount(0)
    await page.getByRole('button', { name: 'Example', exact: true }).click()
    for (const theme of ['Light', 'Dark']) {
      const previous = await page.locator('.trk-mermaid svg').evaluateAll(nodes => nodes.map(node => node.id))
      await page.getByRole('button', { name: theme, exact: true }).click()
      await expect(page.locator('.trk-mermaid svg')).toHaveCount(2)
      if (theme === 'Dark') await expect(page.locator('.trk-mermaid svg').last()).not.toHaveAttribute('id', previous[1])
      await expect(page.locator('.trk-mermaid').first()).toContainText('CLI tools')
      const colors = await page.locator('.trk-md pre code span').evaluateAll(spans => [...new Set(spans.map(span => getComputedStyle(span).color))])
      expect(colors.length).toBeGreaterThanOrEqual(4)
      const contrasts = await page.locator('.trk-md pre code span').evaluateAll(spans => {
        const context = document.createElement('canvas').getContext('2d')!
        const luminance = () => [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).reduce((sum, value, index) => {
          const channel = value / 255
          return sum + (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4) * [0.2126, 0.7152, 0.0722][index]
        }, 0)
        return spans.map(span => {
          context.fillStyle = getComputedStyle(document.body).backgroundColor
          context.fillRect(0, 0, 1, 1)
          context.fillStyle = getComputedStyle(span.closest('pre')!).backgroundColor
          context.fillRect(0, 0, 1, 1)
          const background = luminance()
          context.fillStyle = getComputedStyle(span).color
          context.fillRect(0, 0, 1, 1)
          const foreground = luminance()
          return (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05)
        })
      })
      expect(Math.min(...contrasts)).toBeGreaterThanOrEqual(4.5)
      const ids = await page.locator('.trk-mermaid svg').evaluateAll(nodes => nodes.map(node => node.id))
      expect(new Set(ids).size).toBe(2)
      const clipped = await page.locator('.trk-mermaid > svg').evaluateAll(nodes => nodes.some(node => {
        const view = node.getBoundingClientRect()
        return [...node.querySelectorAll('.node')].some(shape => {
          const bounds = shape.getBoundingClientRect()
          return bounds.left < view.left - 1 || bounds.top < view.top - 1 || bounds.right > view.right + 1 || bounds.bottom > view.bottom + 1
        })
      }))
      expect(clipped).toBe(false)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await expect(page.locator('vite-error-overlay')).toHaveCount(0)
      expect(errors).toEqual([])
      await page.screenshot({ path: `/private/tmp/inky-markdown-${width}-${theme.toLowerCase()}.png`, fullPage: true })
    }
  })

  test(`tall charts stay within the message and viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/tests/browser/markdown.html')
    await page.getByRole('button', { name: 'Dark', exact: true }).click()
    await page.getByRole('button', { name: 'Tall', exact: true }).click()
    const chart = page.locator('.trk-mermaid > svg')
    await expect(chart).toBeVisible()
    for (const height of [900, 500]) {
      await page.setViewportSize({ width, height })
      const bounds = await chart.boundingBox()
      expect(bounds!.height).toBeLessThanOrEqual(Math.min(480, height * 0.6) + 1)
      expect(bounds!.width).toBeLessThanOrEqual(width)
      const clipped = await chart.evaluate(svg => {
        const view = svg.getBoundingClientRect()
        return [...svg.querySelectorAll('.node')].some(node => {
          const bounds = node.getBoundingClientRect()
          return bounds.left < view.left - 1 || bounds.top < view.top - 1 || bounds.right > view.right + 1 || bounds.bottom > view.bottom + 1
        })
      })
      expect(clipped).toBe(false)
      await expect(chart).toContainText('Start')
      await expect(chart).toContainText('End')
      await page.locator('main').screenshot({ path: `/private/tmp/inky-mermaid-size-${width}-${height}.png` })
    }
  })
}
