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
