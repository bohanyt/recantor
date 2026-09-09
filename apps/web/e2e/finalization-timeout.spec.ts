import { expect, test } from '@playwright/test';

async function stallChunkUploads(page: Parameters<Parameters<typeof test>[1]>[0]['page']) {
  await page.route('**/api/v1/sessions/*/chunks/*', async () => {
    await new Promise<void>(() => {
      // Intentionally never settle. The recorder must cancel this request itself.
    });
  });
}

test('returns to recovery when a stalled chunk upload exhausts bounded attempts', async ({ page }) => {
  test.setTimeout(45_000);
  await stallChunkUploads(page);

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('defer-finalization')).toBeVisible({ timeout: 5_000 });

  await expect(page.getByTestId('finish-recovered')).toBeVisible({ timeout: 35_000 });
  await expect(page.getByTestId('recorder-message')).toContainText('Finish recovery later');
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent())
    .not.toContain('0 fragments');
});

test('lets the user defer a stalled finalization without discarding local audio', async ({ page }) => {
  await stallChunkUploads(page);

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await page.getByTestId('stop-recording').click();
  const defer = page.getByTestId('defer-finalization');
  await expect(defer).toBeVisible({ timeout: 5_000 });
  await defer.click();

  await expect(page.getByTestId('finish-recovered')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('recorder-message')).toContainText('Finalization deferred');
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent())
    .not.toContain('0 fragments');
});
