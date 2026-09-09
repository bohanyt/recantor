import { expect, test } from '@playwright/test';

test('records microphone audio through durable ACK and clean finalization', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  await page.waitForTimeout(2_600);
  await page.getByTestId('stop-recording').click();

  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
  await expect(page.getByTestId('durability-state')).toContainText('Server synced');
});

test('keeps two independent browser contexts isolated while recording concurrently', async ({
  browser,
  page,
}) => {
  const secondContext = await browser.newContext({ permissions: ['microphone'] });
  const secondPage = await secondContext.newPage();
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:4173';

  try {
    await Promise.all([page.goto('/'), secondPage.goto(baseUrl)]);
    await Promise.all([
      page.getByTestId('start-recording').click(),
      secondPage.getByTestId('start-recording').click(),
    ]);
    await Promise.all([
      expect(page.getByTestId('recorder-message')).toContainText('Recording'),
      expect(secondPage.getByTestId('recorder-message')).toContainText('Recording'),
    ]);

    await page.waitForTimeout(2_600);
    await Promise.all([
      page.getByTestId('stop-recording').click(),
      secondPage.getByTestId('stop-recording').click(),
    ]);

    await Promise.all([
      expect(page.getByTestId('recorder-message')).toContainText('finalized', {
        timeout: 20_000,
      }),
      expect(secondPage.getByTestId('recorder-message')).toContainText('finalized', {
        timeout: 20_000,
      }),
    ]);
    await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
    await expect(secondPage.getByTestId('pending-chunks')).toContainText('0 fragments');
  } finally {
    await secondContext.close();
  }
});

test('keeps recording locally through temporary chunk-upload loss and catches up in place', async ({
  page,
}) => {
  await page.route('**/api/v1/sessions/*/chunks/*', async (route) => route.abort('failed'));
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  await expect
    .poll(async () => page.getByTestId('pending-chunks').textContent(), { timeout: 10_000 })
    .not.toContain('0 fragments');

  await page.unroute('**/api/v1/sessions/*/chunks/*');
  await page.getByTestId('sync-recording').click();
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments', {
    timeout: 20_000,
  });

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('recovers IndexedDB audio after refresh when chunk uploads were unavailable', async ({
  page,
}) => {
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

  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
});

test('a second same-origin tab cannot silently become the active recorder', async ({
  page,
  context,
}) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  const secondPage = await context.newPage();
  await secondPage.goto('/');
  await expect(secondPage.getByTestId('resume-recording')).toBeVisible({ timeout: 10_000 });
  await secondPage.getByTestId('resume-recording').click();
  await expect(secondPage.getByRole('alert')).toContainText('Another tab', { timeout: 10_000 });

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('continues through a bounded Chromium background-tab interval while the browser stays awake', async ({
  page,
  context,
}) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  const foregroundPage = await context.newPage();
  await foregroundPage.goto('/');
  await foregroundPage.bringToFront();
  await foregroundPage.waitForTimeout(4_500);
  await page.bringToFront();

  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
  await foregroundPage.close();
});

test('stops and surfaces an explicit unsafe state when the IndexedDB chunk append fails', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const originalAdd = IDBObjectStore.prototype.add;
    IDBObjectStore.prototype.add = function add(value: unknown, key?: IDBValidKey) {
      if (this.name === 'chunks') {
        throw new DOMException('Injected recorder quota failure', 'QuotaExceededError');
      }
      return originalAdd.call(this, value, key);
    };
  });

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Recording');

  await expect(page.getByTestId('durability-state')).toContainText('Unsafe', { timeout: 10_000 });
  await expect(page.getByTestId('recorder-message')).toContainText('Capture stopped', {
    timeout: 10_000,
  });
  await expect(page.getByTestId('finish-recovered')).toBeVisible();

  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('recorder-message')).toContainText('Explicit interruption evidence', {
    timeout: 20_000,
  });
});
