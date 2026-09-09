import { expect, test } from '@playwright/test';

test('records microphone audio through durable ACK and clean finalization', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  await page.waitForTimeout(2_600);
  await page.getByTestId('stop-recording').click();

  await expect(page.getByTestId('recorder-message')).toContainText('finalized', { timeout: 20_000 });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
  await expect(page.getByTestId('durability-state')).toContainText('Server synced');
});

test('recovers IndexedDB audio after refresh when chunk uploads were unavailable', async ({ page }) => {
  await page.route('**/api/v1/sessions/*/chunks/*', async (route) => route.abort('failed'));
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await page.reload();
  await expect(page.getByTestId('resume-recording')).toBeVisible({ timeout: 10_000 });
  await page.unroute('**/api/v1/sessions/*/chunks/*');
  await page.getByTestId('finish-recovered').click();

  await expect(page.getByTestId('recorder-message')).toContainText('finalized', { timeout: 20_000 });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
});

test('a second same-origin tab cannot silently become the active recorder', async ({ page, context }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  const secondPage = await context.newPage();
  await secondPage.goto('/');
  await expect(secondPage.getByTestId('resume-recording')).toBeVisible({ timeout: 10_000 });
  await secondPage.getByTestId('resume-recording').click();
  await expect(secondPage.getByRole('alert')).toContainText('Another tab', { timeout: 10_000 });

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', { timeout: 20_000 });
});
