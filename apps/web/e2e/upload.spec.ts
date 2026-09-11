import { expect, test } from '@playwright/test';
import path from 'node:path';

const uploadFixture = process.env.UPLOAD_E2E_FILE;
const apiBaseUrl = process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

type RecoveryEntry = {
  clientRequestId: string;
  capabilityToken: string;
  sessionId: string | null;
};

type UploadStatus = {
  session_id: string;
  state: string;
  received_bytes: number;
};

async function activeRecovery(page: import('@playwright/test').Page): Promise<RecoveryEntry> {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem('recantor:upload-recovery:v1');
    if (!raw) throw new Error('upload recovery state is missing');
    const entries = Object.values(JSON.parse(raw)) as RecoveryEntry[];
    if (entries.length !== 1) throw new Error(`expected one recovery entry, got ${entries.length}`);
    return entries[0];
  });
}

async function statusFor(
  request: import('@playwright/test').APIRequestContext,
  recovery: RecoveryEntry,
): Promise<UploadStatus> {
  if (!recovery.sessionId) throw new Error('upload session is not bound');
  const response = await request.get(`${apiBaseUrl}/api/v1/uploads/${recovery.sessionId}`, {
    headers: { 'X-Recantor-Upload-Token': recovery.capabilityToken },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as UploadStatus;
}

test.describe('existing recording resumable upload', () => {
  test.skip(!uploadFixture, 'UPLOAD_E2E_FILE enables the real tusd upload proof');

  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
  });

  test('pauses after durable progress, reloads, and resumes the same upload', async ({
    page,
    request,
  }) => {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 30,
      downloadThroughput: -1,
      uploadThroughput: 512 * 1024,
    });

    await page.getByTestId('upload-file-input').setInputFiles(uploadFixture!);
    await expect(page.getByTestId('upload-selected-file')).toContainText(path.basename(uploadFixture!));
    await page.getByTestId('upload-start').click();

    await expect
      .poll(async () => Number.parseInt((await page.getByTestId('upload-progress').textContent()) ?? '0'))
      .toBeGreaterThan(0);
    await page.getByTestId('upload-pause').click();
    await expect(page.getByTestId('upload-message')).toContainText('Paused');

    const recovery = await activeRecovery(page);
    expect(recovery.capabilityToken).toMatch(/^[A-Za-z0-9_-]{43}$/);
    await expect.poll(async () => (await statusFor(request, recovery)).received_bytes).toBeGreaterThan(0);
    const beforeReload = await statusFor(request, recovery);
    expect(beforeReload.state).toBe('uploading');

    await page.reload();
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
    await page.getByTestId('upload-file-input').setInputFiles(uploadFixture!);
    await page.getByTestId('upload-start').click();

    await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
      timeout: 60_000,
    });
    await expect(page.getByTestId('upload-progress')).toHaveText('100%');
    const afterReload = await statusFor(request, recovery);
    expect(afterReload.session_id).toBe(beforeReload.session_id);
    expect(afterReload.state).toBe('uploaded');
    expect(afterReload.received_bytes).toBeGreaterThan(beforeReload.received_bytes);
    await expect(page.locator('body')).not.toContainText('/files/');
  });

  test('shows server policy failure for an unsupported file', async ({ page }) => {
    await page.getByTestId('upload-file-input').setInputFiles({
      name: 'not-media.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('not media'),
    });
    await page.getByTestId('upload-start').click();
    await expect(page.getByTestId('upload-message')).toContainText('unsupported file extension');
  });
});
