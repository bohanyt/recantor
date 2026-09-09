import { expect, test } from '@playwright/test';

const CHUNK_ROUTE = '**/api/v1/sessions/*/chunks/*';

test.describe.configure({ retries: 0 });

test('clean Stop flushes a fresh stable spool after cancelling a pre-Stop sync', async ({ page }) => {
  test.setTimeout(30_000);
  let chunkRequests = 0;

  await page.route(CHUNK_ROUTE, async (route) => {
    chunkRequests += 1;
    if (chunkRequests === 1) {
      await new Promise<void>(() => {
        // Hold the first ordinary-recording PUT open. Stop must not merely await this
        // pre-Stop sync snapshot after the final MediaRecorder fragment is persisted.
      });
      return;
    }
    await route.continue();
  });

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

  await expect.poll(() => chunkRequests, { timeout: 10_000 }).toBe(1);
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await page.getByTestId('stop-recording').click();

  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
  expect(chunkRequests).toBeGreaterThanOrEqual(3);
});
