import { expect, test } from '@playwright/test'
import type { ApiOAuthConsent } from '../../src/lib/api/auth'

const alice = { id: 'alice-id', username: 'alice', name: 'Alice', nickname: null }
const details: ApiOAuthConsent = {
  client_name: 'Native harness', redirect_uri: 'http://127.0.0.1:5174/native-callback',
  resource: 'https://octomate.test/octomate/mcp', scopes: ['mcp'], user_id: alice.id, username: alice.username,
}

for (const mobile of [false, true]) {
  test(`shared login preserves consent and completes ${mobile ? 'denial on mobile' : 'approval on desktop'}`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 })
    await page.emulateMedia({ colorScheme: mobile ? 'light' : 'dark', reducedMotion: 'reduce' })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    let signedIn = false
    await page.route('http://127.0.0.1:5174/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/login') {
        signedIn = route.request().postDataJSON().password === 'correct-password'
        await route.fulfill({ status: signedIn ? 204 : 401 })
      } else {
        await route.fulfill(signedIn ? { json: alice } : { status: 401 })
      }
    })
    await page.route('**/oauth/consent', route => route.fulfill({ status: 302, headers: { Location: '/auth/authorize' } }))
    await page.route('**/oauth/consent/inspect', async route => {
      expect(route.request().postDataJSON()).toEqual({ ticket: 'consent-ticket' })
      expect(route.request().headers()['x-octomate-request']).toBe('1')
      await route.fulfill({ json: details })
    })
    await page.route('**/oauth/consent/decide', async route => {
      expect(route.request().postDataJSON()).toEqual({ ticket: 'consent-ticket', expected_user_id: alice.id, approve: !mobile })
      expect(route.request().headers()['x-octomate-request']).toBe('1')
      await route.fulfill({ json: { redirect_uri: `${details.redirect_uri}?${mobile ? 'error=access_denied' : 'code=approved'}` } })
    })
    await page.route('**/native-callback?*', route => route.fulfill({ contentType: 'text/plain', body: 'Returned to native application' }))
    await page.goto('/oauth/consent#ticket=consent-ticket')
    await expect(page.getByRole('heading', { name: 'Sign in.' })).toBeVisible()
    await expect(page.getByText('local account · invite only')).toBeVisible()
    await expect(page).toHaveURL('http://127.0.0.1:5174/auth/authorize')
    await page.screenshot({ path: `/tmp/nautilus-oauth-login-${mobile ? 'mobile' : 'desktop'}.png`, fullPage: true })
    await page.getByLabel('Username', { exact: true }).fill('alice')
    await page.locator('input[name="password"]').fill('wrong-password')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByText('That username and password do not match.')).toBeVisible()
    await page.locator('input[name="password"]').fill('correct-password')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByText('Native harness', { exact: true })).toBeVisible()
    await expect(page.getByText('alice', { exact: true })).toBeVisible()
    await expect(page).toHaveURL('http://127.0.0.1:5174/auth/authorize')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `/tmp/nautilus-oauth-consent-${mobile ? 'mobile' : 'desktop'}.png`, fullPage: true })
    await page.getByRole('button', { name: mobile ? 'Deny' : 'Allow access', exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`native-callback\\?${mobile ? 'error=access_denied' : 'code=approved'}$`))
    expect(errors).toEqual([])
  })
}

test('existing session skips login and an account change requires renewed review', async ({ page }) => {
  let accountChanged = false
  const decisions: string[] = []
  await page.route('http://127.0.0.1:5174/api/**', route => route.fulfill({ json: alice }))
  await page.route('**/oauth/consent/inspect', route => route.fulfill({
    json: accountChanged ? { ...details, user_id: 'bob-id', username: 'bob' } : details,
  }))
  await page.route('**/oauth/consent/decide', async route => {
    decisions.push(route.request().postDataJSON().expected_user_id)
    accountChanged = true
    await route.fulfill({ status: 409, json: { detail: 'Account changed' } })
  })
  await page.goto('/auth/authorize#ticket=existing-session-ticket')
  await expect(page.getByText('alice', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Sign in.' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Allow access' }).click()
  await expect(page.getByRole('alert')).toHaveText('Your account changed. Review the request again.')
  await expect(page.getByText('bob', { exact: true })).toBeVisible()
  expect(decisions).toEqual(['alice-id'])
  await page.getByRole('button', { name: 'Allow access' }).click()
  await expect.poll(() => decisions).toEqual(['alice-id', 'bob-id'])
})

test('expired request cannot be approved', async ({ page }) => {
  await page.route('http://127.0.0.1:5174/api/**', route => route.fulfill({ json: alice }))
  await page.route('**/oauth/consent/inspect', route => route.fulfill({ status: 400 }))
  await page.goto('/auth/authorize#ticket=expired-ticket')
  await expect(page.getByRole('alert')).toHaveText('This request is invalid or expired. Start again from your application.')
  await expect(page.getByRole('button', { name: 'Allow access' })).toHaveCount(0)
})


test('reloading consent keeps its path and explains the missing ticket', async ({ page }) => {
  await page.route('http://127.0.0.1:5174/api/**', route => route.fulfill({ json: alice }))
  await page.route('**/oauth/consent/inspect', route => route.fulfill({ json: details }))
  await page.goto('/auth/authorize#ticket=reload-ticket')
  await expect(page.getByText('alice', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page).toHaveURL('http://127.0.0.1:5174/auth/authorize')
  await expect(page.getByRole('alert')).toHaveText('This request is missing. Start again from your application.')
  await expect(page.getByRole('button', { name: 'Allow access' })).toHaveCount(0)
})
