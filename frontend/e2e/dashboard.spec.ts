import { expect, test } from '@playwright/test'

test('loads data and drills from a confidence bar into markets', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Historical mispredictions' })).toBeVisible()
  await page.getByRole('button', { name: /^(Load|Reload) data$/ }).click()
  await expect(page.getByText('Local data ready')).toBeVisible({ timeout: 15000 })
  await expect(page.getByText('Will rainfall exceed 2 inches?')).toBeVisible()

  const chart = page.getByRole('img', { name: 'Wrong predictions by peak confidence band' })
  await expect(chart).toBeVisible()
  await chart.click({ position: { x: 161, y: 80 } })
  await expect(page.getByRole('button', { name: 'Show all bands' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '80–84% confidence misses' })).toBeVisible()
})

test('shows filtered open markets in soonest-close order', async ({ page }) => {
  await page.goto('/')
  const firstRequestPromise = page.waitForRequest((request) => request.url().includes('/api/v1/open-markets?'))
  await page.getByRole('button', { name: 'Open markets' }).click()
  await expect(page.getByRole('heading', { name: 'Open markets' })).toBeVisible()
  const firstRequest = await firstRequestPromise
  expect(firstRequest.url()).toContain('threshold=80')
  expect(firstRequest.url()).toContain('horizon=7d')

  const rows = page.locator('.open-table tbody tr')
  await expect(rows.first()).toBeVisible()
  await expect(rows).toHaveCount(2)
  await expect(rows.first().locator('td').nth(5)).not.toHaveText('—')
  await expect(page.getByText('Fixed order: soonest close first')).toBeVisible()

  const filteredRequest = page.waitForRequest((request) => request.url().includes('/api/v1/open-markets?') && request.url().includes('threshold=90') && request.url().includes('horizon=3d'))
  await page.getByRole('button', { name: '90%+' }).click()
  await page.getByLabel('Closing horizon').selectOption('3d')
  await filteredRequest
})
