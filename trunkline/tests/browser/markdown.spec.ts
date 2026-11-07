import { test, expect } from '@playwright/test'

for (const width of [1280, 390]) {
  test(`message diagrams and code colors work in both themes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    const diagramRequests: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()) })
    page.on('request', request => { if (request.url().includes('/pele')) diagramRequests.push(request.url()) })
    await page.goto('/tests/browser/markdown.html')
    await expect(page).toHaveTitle('Message rendering test')
    await expect(page.locator('.hljs-keyword').first()).toHaveText('import')
    await expect(page.locator('code').first()).toHaveText('demo_worker.py')
    const block = page.locator('.trk-code-block').first()
    await expect(block.locator('.trk-code-header')).toHaveText('Python8 lines')
    await expect(block.locator('.trk-code-gutter')).toHaveText('1\n2\n3\n4\n5\n6\n7\n8')
    expect(await page.locator('.trk-md h2').evaluate(el => getComputedStyle(el).fontWeight)).toBe('500')
    expect(await block.locator('.trk-code-header').evaluate(el => getComputedStyle(el).fontWeight)).toBe('400')
    expect(diagramRequests).toEqual([])
    await page.getByRole('button', { name: 'Escaped code', exact: true }).click()
    await expect(page.locator('.trk-code-header').first()).toHaveText('Python3 lines')
    await expect(page.locator('.trk-code-header').last()).toHaveText('JavaScript3 lines')
    await expect(page.locator('.hljs-keyword').first()).toHaveText('import')
    await expect(page.locator('.hljs-number').first()).toHaveText('42')
    await expect(page.locator('.hljs-string')).toHaveText('"<script>literal</script>"')
    await expect(page.locator('.hljs-comment')).toHaveText('/* first\nsecond */')
    await expect(page.locator('.trk-md script')).toHaveCount(0)
    await page.getByRole('button', { name: 'Stream code', exact: true }).click()
    await expect(page.locator('.hljs-keyword')).toHaveText('import')
    await expect(page.locator('.trk-code-block .trk-print-caret')).toHaveCount(1)
    await page.getByRole('button', { name: 'Stream', exact: true }).click()
    await expect(page.locator('.trk-mermaid code')).toContainText('Core -->')
    await expect(page.locator('.trk-print-caret')).toHaveCount(1)
    await expect(page.locator('.trk-mermaid > svg.pele')).toHaveCount(0)
    expect(diagramRequests).toEqual([])
    await page.getByRole('button', { name: 'Finish', exact: true }).click()
    await expect(page.locator('.trk-mermaid > svg.pele')).toBeVisible()
    await expect(page.locator('.trk-mermaid')).toContainText('Alice worker')
    await expect(page.locator('.trk-print-caret')).toHaveCount(0)
    await page.getByRole('button', { name: 'Invalid', exact: true }).click()
    await expect(page.getByRole('status')).toHaveText('Unable to render this Mermaid diagram.')
    await expect(page.locator('.trk-mermaid code')).toContainText('A --> [')
    await page.getByRole('button', { name: 'Unsafe', exact: true }).click()
    await expect(page.locator('.trk-mermaid > svg.pele')).toBeVisible()
    await expect(page.locator('.trk-md script, .trk-md img, .trk-md [onerror], .trk-md [onclick], .trk-md [href^="javascript:"]')).toHaveCount(0)
    await page.getByRole('button', { name: 'Sequence', exact: true }).click()
    await expect(page.locator('.trk-mermaid > svg.pele-sequence')).toBeVisible()
    await expect(page.locator('.trk-mermaid')).toContainText('Run task')
    await page.getByRole('button', { name: 'Example', exact: true }).click()
    await expect(page.locator('.trk-mermaid > svg.pele')).toHaveCount(2)
    await page.evaluate(async () => { await document.fonts.ready })
    for (const theme of ['Light', 'Dark']) {
      const previous = await page.locator('.trk-mermaid > svg.pele').elementHandles()
      const previousColor = await page.locator('.trk-mermaid > svg.pele').first().evaluate(svg => getComputedStyle(svg).color)
      await page.getByRole('button', { name: theme, exact: true }).click()
      await expect(page.locator('.trk-mermaid > svg.pele')).toHaveCount(2)
      for (const svg of previous) expect(await svg.evaluate(node => node.isConnected)).toBe(true)
      if (theme === 'Dark') {
        const color = await page.locator('.trk-mermaid > svg.pele').first().evaluate(svg => getComputedStyle(svg).color)
        expect(color).not.toBe(previousColor)
      }
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
      const ids = await page.locator('.trk-mermaid > svg.pele [id]').evaluateAll(nodes => nodes.map(node => node.id))
      expect(ids.length).toBeGreaterThan(0)
      expect(new Set(ids).size).toBe(ids.length)
      const clipped = await page.locator('.trk-mermaid > svg.pele').evaluateAll(nodes => nodes.some(node => {
        const view = node.getBoundingClientRect()
        return [...node.querySelectorAll('.pele-node')].some(shape => {
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

  test(`long code stays selectable and scrollable with aligned line numbers at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/tests/browser/markdown.html')
    await page.getByRole('button', { name: 'Long code', exact: true }).click()
    const region = page.getByRole('region', { name: 'Python code', exact: true })
    const gutter = region.locator('.trk-code-gutter')
    const code = region.locator('code')
    await expect(page.locator('.trk-code-header')).toHaveText('Python105 lines')
    await expect(gutter).toContainText('104\n105')
    await expect(code.locator('.hljs-string').first()).toHaveText('"""First line\nSecond line\nThird line"""')
    const original = await code.textContent()
    const selected = await code.evaluate(el => {
      const selection = window.getSelection()!
      selection.selectAllChildren(el)
      return selection.toString()
    })
    expect(selected).toBe(original)
    await page.evaluate(() => window.getSelection()?.removeAllRanges())
    expect(await gutter.evaluate(el => getComputedStyle(el).userSelect)).toBe('none')
    expect(await gutter.evaluate(el => getComputedStyle(el).lineHeight)).toBe(await code.evaluate(el => getComputedStyle(el).lineHeight))
    for (const theme of ['Light', 'Dark']) {
      await page.getByRole('button', { name: theme, exact: true }).click()
      await region.evaluate(el => { el.scrollLeft = 0; el.scrollTop = 0 })
      const left = (await gutter.boundingBox())!.x
      await region.focus()
      await page.keyboard.press('ArrowRight')
      await expect.poll(() => region.evaluate(el => el.scrollLeft)).toBeGreaterThan(0)
      expect(Math.abs((await gutter.boundingBox())!.x - left)).toBeLessThan(1)
      expect(Math.round((await region.boundingBox())!.height)).toBeLessThanOrEqual(560)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await region.evaluate(el => { el.scrollLeft = 0 })
      await page.locator('.trk-code-block').screenshot({ path: `/private/tmp/inky-code-block-${width}-${theme.toLowerCase()}.png` })
    }
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })

  test(`tall charts stay within the message and viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/tests/browser/markdown.html')
    await page.getByRole('button', { name: 'Dark', exact: true }).click()
    await page.getByRole('button', { name: 'Tall', exact: true }).click()
    const chart = page.locator('.trk-mermaid > svg.pele')
    await expect(chart).toBeVisible()
    await page.evaluate(async () => { await document.fonts.ready })
    for (const height of [900, 500]) {
      await page.setViewportSize({ width, height })
      const bounds = await chart.boundingBox()
      expect(bounds!.height).toBeLessThanOrEqual(Math.min(480, height * 0.6) + 1)
      expect(bounds!.width).toBeLessThanOrEqual(width)
      const clipped = await chart.evaluate(svg => {
        const view = svg.getBoundingClientRect()
        return [...svg.querySelectorAll('.pele-node')].some(node => {
          const bounds = node.getBoundingClientRect()
          return bounds.left < view.left - 1 || bounds.top < view.top - 1 || bounds.right > view.right + 1 || bounds.bottom > view.bottom + 1
        })
      })
      expect(clipped).toBe(false)
      await expect(chart).toContainText('Start')
      await expect(chart).toContainText('End')
      await page.locator('main').screenshot({ path: `/private/tmp/inky-mermaid-size-${width}-${height}.png` })
    }
    const fitted = await chart.getAttribute('viewBox')
    const diagram = page.locator('.trk-mermaid')
    await diagram.getByRole('button', { name: 'Zoom in', exact: true }).click()
    await expect(chart).not.toHaveAttribute('viewBox', fitted!)
    const zoomed = await chart.getAttribute('viewBox')
    await chart.focus()
    await page.keyboard.press('ArrowDown')
    await expect(chart).not.toHaveAttribute('viewBox', zoomed!)
    await diagram.getByRole('button', { name: 'Reset zoom', exact: true }).click()
    await expect(chart).toHaveAttribute('viewBox', fitted!)
  })
}
