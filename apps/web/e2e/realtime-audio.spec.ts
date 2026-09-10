import { expect, test } from '@playwright/test';

test('starts the realtime PCM lane from the same recording flow without weakening archive capture', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();

  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('realtime-status')).toHaveText('live', { timeout: 15_000 });

  await page.waitForTimeout(2_600);
  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('durability-state')).toContainText('Server synced');
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
});

test('archive recording still succeeds when realtime worklet startup fails', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'AudioWorkletNode', {
      configurable: true,
      value: class FailingAudioWorkletNode {
        constructor() {
          throw new Error('forced realtime worklet startup failure');
        }
      },
    });
  });
  await page.goto('/');
  await page.getByTestId('start-recording').click();

  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('realtime-status')).toHaveText('degraded', { timeout: 10_000 });
  await expect(page.getByTestId('realtime-error')).toContainText('Archive recording continues');

  await page.waitForTimeout(2_600);
  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('durability-state')).toContainText('Server synced');
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
});
