import { test, expect } from '@playwright/test'

for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  test(`text replacements crossfade and preserve focus with ${reducedMotion} motion`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/tests/browser/textMotion.html')
    await expect(page).toHaveTitle('Text motion test')
    const button = page.getByRole('button')
    await expect(button).toHaveAccessibleName('Ready')
    await button.focus()
    expect(await button.locator('.lt-text-swap').evaluate(el => el.getAnimations({ subtree: true }).length)).toBe(0)
    // Pause at the committed replacement to inspect both frames, including the short reduced-motion fade.
    await button.evaluate(el => {
      const observer = new MutationObserver(() => el.querySelector('.lt-text-swap')!.getAnimations({ subtree: true }).forEach(animation => animation.pause()))
      observer.observe(el, { childList: true, subtree: true, characterData: true })
      el.addEventListener('blur', () => observer.disconnect(), { once: true })
    })
    await button.press('Space')
    await expect(button).toBeFocused()
    await expect(button).toHaveAccessibleName('A longer status label')
    await expect(button.locator('.lt-text-old')).toHaveAttribute('aria-hidden', 'true')
    const timing = await button.locator('.lt-text-new').evaluate(el => {
      const animation = el.getAnimations()[0]
      return { name: getComputedStyle(el).animationName, duration: animation.effect!.getTiming().duration }
    })
    expect(timing).toEqual({ name: 'lt-fade-in', duration: reducedMotion === 'reduce' ? 80 : 240 })
    expect(await page.getByRole('status').evaluate(el => el.getBoundingClientRect().width)).toBeLessThanOrEqual(90)
    await button.press('Space')
    await button.press('Space')
    await expect(button).toHaveAccessibleName('')
    await expect(button.locator('.lt-text-old')).toHaveText('Done')
    expect(await button.locator('.lt-text-old').evaluate(el => el.getBoundingClientRect().width)).toBeGreaterThan(0)
    await button.press('Space')
    await expect(button).toHaveAccessibleName('Ready')
    await expect(page.getByTestId('counter').locator('[data-text-current]')).toHaveText('0')
    await expect(button.locator('.lt-text-old')).toHaveCount(1)
    await button.evaluate(el => el.getAnimations({ subtree: true }).forEach(animation => animation.finish()))
    await expect(button.locator('.lt-text-old')).toHaveCount(0)
    await expect(button).toBeFocused()
    await expect(page.getByRole('status')).toHaveText('Ready')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
  })
}
