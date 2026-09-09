import { expect, test, type Page } from '@playwright/test';

const CHUNK_ROUTE = '**/api/v1/sessions/*/chunks/*';
const FINALIZE_ROUTE = '**/api/v1/sessions/*/finalize';

test.describe.configure({ retries: 0 });

async function stallChunkUploads(page: Page) {
  await page.route(CHUNK_ROUTE, async () => {
    await new Promise<void>(() => {
      // Intentionally never settle. The recorder must cancel this request itself.
    });
  });
}

async function startRecording(page: Page) {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
}

test('returns to recovery when a stalled chunk upload exhausts bounded attempts', async ({
  page,
}) => {
  test.setTimeout(45_000);
  await stallChunkUploads(page);
  await startRecording(page);

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

test('keeps recording through a hung ordinary upload attempt and catches up', async ({ page }) => {
  test.setTimeout(30_000);
  let chunkRequests = 0;
  await page.route(CHUNK_ROUTE, async (route) => {
    chunkRequests += 1;
    if (chunkRequests === 1) {
      await new Promise<void>(() => {
        // The first ordinary-recording PUT never settles. The per-attempt deadline must abort it.
      });
      return;
    }
    await route.continue();
  });

  await startRecording(page);
  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await expect.poll(() => chunkRequests, { timeout: 12_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId('stop-recording')).toBeVisible();
  await expect(page.getByText('recording', { exact: true })).toBeVisible();

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('lets the user defer a stalled finalization without discarding local audio', async ({
  page,
}) => {
  await stallChunkUploads(page);
  await startRecording(page);

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

test('reconciles remote COMPLETE after a successful finalize response is lost', async ({
  page,
}) => {
  test.setTimeout(30_000);
  let resolveCommitted: ((complete: boolean) => void) | null = null;
  const serverCommitted = new Promise<boolean>((resolve) => {
    resolveCommitted = resolve;
  });

  await page.route(FINALIZE_ROUTE, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as { complete?: boolean };
    resolveCommitted?.(payload.complete === true);
    await new Promise<void>(() => {
      // The server response is deliberately withheld after the real finalize request completed.
      // The browser must time out and later reconcile the server's terminal truth.
    });
  });

  await startRecording(page);
  await page.waitForTimeout(2_200);
  await page.getByTestId('stop-recording').click();

  await expect(serverCommitted).resolves.toBe(true);
  await expect(page.getByTestId('finish-recovered')).toBeVisible({ timeout: 10_000 });

  await page.unroute(FINALIZE_ROUTE);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 10_000,
  });
});
