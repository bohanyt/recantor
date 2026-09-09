import { expect, test } from '@playwright/test';

test('web shell reaches API and PostgreSQL readiness', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Recantor' })).toBeVisible();
  await expect(page.getByTestId('health-state')).toHaveText('Online');
  await expect(page.getByTestId('readiness-state')).toHaveText('Online');
});
