import { test, expect } from '@playwright/test'

for (const width of [1280, 390]) {
  test.describe(`copy controls at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: width === 390 })
    test.beforeEach(async ({ page }) => {
      await page.addInitScript(() => {
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
          writeText: async (text: string) => {
            if (document.documentElement.hasAttribute('data-reject-copy')) throw new DOMException('Clipboard denied', 'NotAllowedError')
            document.documentElement.setAttribute('data-copied', text)
          },
        } })
      })
    })

    test('code copies without language labels or line numbers and reports clipboard failures', async ({ page }) => {
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.goto('/tests/browser/markdown.html')
      const block = page.locator('.trk-code-block')
      const copy = block.getByRole('button', { name: 'Copy code', exact: true })
      await expect(copy).toBeVisible()
      const buttonBounds = (await copy.boundingBox())!
      const iconBounds = (await copy.locator('svg').boundingBox())!
      expect(Math.abs(buttonBounds.width - buttonBounds.height)).toBeLessThan(1)
      expect(Math.abs(iconBounds.x + iconBounds.width / 2 - buttonBounds.x - buttonBounds.width / 2)).toBeLessThan(1)
      expect(Math.abs(iconBounds.y + iconBounds.height / 2 - buttonBounds.y - buttonBounds.height / 2)).toBeLessThan(1)
      const bounds = await block.boundingBox()
      await copy.click()
      await expect(copy.getByRole('status')).toHaveText('Copied')
      expect((await copy.boundingBox())!.width).toBeCloseTo(buttonBounds.width, 0)
      await expect(page.locator('html')).toHaveAttribute('data-copied', 'import json\nfrom pathlib import Path\n\n# A private workspace for this worker.\nworkspace = Path("/work/alice")\ncount = 42\nif count > 0:\n    print("Ready", workspace)\n')
      expect((await block.boundingBox())!.height).toBeCloseTo(bounds!.height, 0)
      await expect(copy.getByRole('status')).toHaveText('')
      await page.getByRole('button', { name: 'Long code', exact: true }).click()
      const original = await block.locator('code').textContent()
      await copy.focus()
      await page.keyboard.press('Enter')
      await expect(page.locator('html')).toHaveAttribute('data-copied', original!)
      await expect(copy.getByRole('status')).toHaveText('Copied')
      await page.evaluate(() => document.documentElement.setAttribute('data-reject-copy', ''))
      await copy.click()
      await expect(copy.getByRole('status')).toHaveText('Copy failed')
      await page.evaluate(() => document.documentElement.removeAttribute('data-reject-copy'))
      await copy.click()
      await expect(copy.getByRole('status')).toHaveText('Copied')
      await page.getByRole('button', { name: 'Example', exact: true }).click()
      await expect(page.locator('svg.pele')).toHaveCount(2)
      await page.evaluate(async () => {
        await document.fonts.ready
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      })
      const diagrams = await page.locator('svg.pele').elementHandles()
      await copy.click()
      for (const diagram of diagrams) expect(await diagram.evaluate(el => el.isConnected)).toBe(true)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: `/private/tmp/inky-copy-code-${width}.png` })
      expect(errors).toEqual([])
    })

    test('device code and its adjacent button copy with press feedback', async ({ page }) => {
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
      await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ contentType: 'text/css', body: '' }))
      await page.goto('/tests/browser/markdown.html')
      await expect(page).toHaveTitle('Message rendering test')
      await page.getByRole('button', { name: 'Reasoning', exact: true }).click()
      await page.getByRole('button', { name: 'Dark', exact: true }).click()
      const card = page.locator('#pm-device-code')
      const button = card.getByRole('button', { name: 'Copy device code', exact: true })
      const code = button.locator('.trk-oauth-code')
      const icon = button.locator(':scope > svg')
      await expect(code).toHaveText('ABCD-1234')
      await expect(icon).toHaveCount(1)
      for (const target of [code, icon]) {
        await target.hover()
        await expect(button).toHaveCSS('cursor', 'default')
        await page.mouse.down()
        await expect(icon).not.toHaveCSS('transform', 'none')
        await expect(code).not.toHaveCSS('transform', 'none')
        await page.mouse.up()
        await expect(button.getByRole('status')).toHaveText('Copied')
        await expect(page.locator('html')).toHaveAttribute('data-copied', 'ABCD-1234')
        expect(await page.evaluate(() => getSelection()?.toString())).toBe('')
        await expect(icon).toHaveCSS('transform', 'none')
        await expect(code).toHaveCSS('transform', 'none')
        await page.evaluate(() => document.documentElement.removeAttribute('data-copied'))
      }
      await code.dblclick()
      expect(await page.evaluate(() => getSelection()?.toString())).toBe('')
      await page.evaluate(() => document.documentElement.removeAttribute('data-copied'))
      await button.focus()
      await page.keyboard.down('Space')
      await expect(icon).not.toHaveCSS('transform', 'none')
      await expect(code).not.toHaveCSS('transform', 'none')
      await page.keyboard.up('Space')
      await expect(page.locator('html')).toHaveAttribute('data-copied', 'ABCD-1234')
      await expect(icon).toHaveCSS('transform', 'none')
      await expect(code).toHaveCSS('transform', 'none')
      const codeBounds = (await code.boundingBox())!
      const iconBounds = (await icon.boundingBox())!
      expect(iconBounds.x).toBeGreaterThan(codeBounds.x + codeBounds.width)
      expect(Math.abs(iconBounds.y + iconBounds.height / 2 - codeBounds.y - codeBounds.height / 2)).toBeLessThan(1)
      await page.emulateMedia({ reducedMotion: 'reduce' })
      for (const target of [code, icon]) {
        await target.hover()
        await page.mouse.down()
        await expect(icon).toHaveCSS('transform', 'none')
        await expect(code).toHaveCSS('transform', 'none')
        await page.mouse.up()
        await expect(button.getByRole('status')).toHaveText('Copied')
      }
      await expect(card.getByRole('link', { name: 'Authorize ↗' })).toHaveAttribute('href', 'https://github.com/login/device')
      await expect(page.locator('vite-error-overlay')).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await card.screenshot({ animations: 'disabled', path: `/private/tmp/trunkline-status/code-press-${width}.png` })
      expect(errors).toEqual([])
    })

    test('user and agent messages reveal Copy on hover or focus and retain their Markdown source', async ({ page }) => {
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.goto('/tests/browser/markdown.html')
      await page.getByRole('button', { name: 'Reasoning', exact: true }).click()
      const user = page.locator('#pm-copy-user')
      const copyUser = user.getByRole('button', { name: 'Copy message', exact: true })
      await expect(copyUser).toHaveCSS('opacity', width === 390 ? '1' : '0')
      await user.hover()
      await expect(copyUser).toHaveCSS('opacity', '1')
      await copyUser.click()
      await expect(copyUser.getByRole('status')).toHaveText('Copied')
      await expect(page.locator('html')).toHaveAttribute('data-copied', '**Keep** the `workspace`.\n\nSecond line.')
      await page.getByRole('button', { name: 'Finish reasoning', exact: true }).focus()
      await page.locator('nav').hover()
      await page.keyboard.press('Tab')
      await expect(copyUser).toBeFocused()
      await expect(copyUser).toHaveCSS('opacity', '1')
      await expect(copyUser).toHaveCSS('outline-style', 'solid')
      await page.keyboard.press('Space')
      await expect(copyUser.getByRole('status')).toHaveText('Copied')
      const progress = page.locator('#pm-progress')
      await progress.hover()
      await expect(progress.getByRole('button', { name: 'Copy message', exact: true })).toHaveCount(0)
      await expect(progress.locator('.trk-message-actions')).toHaveCount(0)
      await expect(page.locator('#pm-claude-reply .trk-message-actions')).toHaveCount(0)
      const agent = page.locator('#pm-copy-agent')
      await agent.hover()
      await agent.getByRole('button', { name: 'Copy message', exact: true }).click()
      await expect(page.locator('html')).toHaveAttribute('data-copied', '**Ready** to continue.\n\n- First item\n- Second `item`\n\n```diff\n+ ready = True\n```')
      const stream = page.locator('#pm-copy-stream')
      await stream.hover()
      await stream.getByRole('button', { name: 'Copy message', exact: true }).click()
      await expect(page.locator('html')).toHaveAttribute('data-copied', 'A **native** reply with `code`.')
      for (const row of [user, agent, stream]) {
        const button = row.getByRole('button', { name: 'Copy message', exact: true })
        const position = await button.evaluate(el => {
          const actions = el.parentElement!
          const body = actions.previousElementSibling!.getBoundingClientRect()
          const box = el.getBoundingClientRect()
          const icon = el.querySelector('svg')!.getBoundingClientRect()
          const row = actions.parentElement!
          const height = row.getBoundingClientRect().height
          actions.style.display = 'none'
          const heightWithoutActions = row.getBoundingClientRect().height
          actions.style.display = ''
          return {
            bottom: actions === actions.parentElement!.lastElementChild,
            below: box.top >= body.bottom,
            topGap: (box.top - body.bottom) / (box.height / Number.parseFloat(getComputedStyle(el).height)),
            leftGap: Math.abs(box.left - body.left),
            rowHeight: getComputedStyle(actions).height,
            addedHeight: height - heightWithoutActions,
            square: Math.abs(box.width - box.height),
            centerX: Math.abs(icon.x + icon.width / 2 - box.x - box.width / 2),
            centerY: Math.abs(icon.y + icon.height / 2 - box.y - box.height / 2),
          }
        })
        expect(position.bottom).toBe(true)
        expect(position.addedHeight).toBe(0)
        expect(position.below).toBe(true)
        if (row !== user) {
          expect(position.topGap).toBeCloseTo(6, 0)
          expect(position.leftGap).toBeLessThan(1)
          expect(position.rowHeight).toBe('26px')
        }
        expect(position.square).toBeLessThan(1)
        expect(position.centerX).toBeLessThan(1)
        expect(position.centerY).toBeLessThan(1)
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await agent.hover()
      await agent.screenshot({ path: `/private/tmp/inky-copy-message-${width}.png` })
      expect(errors).toEqual([])
    })
  })
}
