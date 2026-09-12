import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { execFile, spawn } from 'node:child_process';
import { closeSync, openSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const uploadFixture = process.env.UPLOAD_E2E_FILE;
const apiBaseUrl = process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';
const tusEndpoint = process.env.TUS_PUBLIC_ENDPOINT ?? 'http://127.0.0.1:1080/files/';
const apiPidFile = '/tmp/recantor-upload-api.pid';

type RecoveryEntry = {
  clientRequestId: string;
  capabilityToken: string;
  sessionId: string | null;
};

type UploadStatus = {
  session_id: string;
  state: string;
  received_bytes: number;
  completed_at: string | null;
};

async function activeRecovery(page: Page): Promise<RecoveryEntry> {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem('recantor:upload-recovery:v1');
    if (!raw) throw new Error('upload recovery state is missing');
    const entries = Object.values(JSON.parse(raw)) as RecoveryEntry[];
    if (entries.length !== 1) throw new Error(`expected one recovery entry, got ${entries.length}`);
    return entries[0];
  });
}

async function statusFor(
  request: APIRequestContext,
  recovery: RecoveryEntry,
): Promise<UploadStatus> {
  if (!recovery.sessionId) throw new Error('upload session is not bound');
  const response = await request.get(`${apiBaseUrl}/api/v1/uploads/${recovery.sessionId}`, {
    headers: { 'X-Recantor-Upload-Token': recovery.capabilityToken },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as UploadStatus;
}

async function probeApi(request: APIRequestContext, pathName: string): Promise<boolean> {
  try {
    return (await request.get(`${apiBaseUrl}${pathName}`, { timeout: 1_000 })).ok();
  } catch {
    return false;
  }
}

async function restartApi(request: APIRequestContext): Promise<void> {
  const oldPid = Number.parseInt(readFileSync(apiPidFile, 'utf8').trim(), 10);
  if (!Number.isSafeInteger(oldPid) || oldPid <= 0) throw new Error('invalid upload API pid');
  process.kill(oldPid, 'SIGTERM');
  await expect
    .poll(() => probeApi(request, '/healthz'), { timeout: 15_000, intervals: [100, 250, 500] })
    .toBeFalsy();

  const repositoryRoot = process.env.GITHUB_WORKSPACE ?? path.resolve(process.cwd(), '../..');
  const logFd = openSync('/tmp/recantor-upload-api-restart.log', 'a');
  const child = spawn(
    'uv',
    [
      'run',
      '--project',
      'apps/api',
      'uvicorn',
      'recantor.main:app',
      '--host',
      '127.0.0.1',
      '--port',
      '8000',
    ],
    {
      cwd: repositoryRoot,
      env: process.env,
      detached: true,
      stdio: ['ignore', logFd, logFd],
    },
  );
  child.unref();
  closeSync(logFd);
  if (!child.pid) throw new Error('restarted upload API did not expose a pid');
  writeFileSync(apiPidFile, `${child.pid}\n`);

  await expect
    .poll(() => probeApi(request, '/readyz'), { timeout: 30_000, intervals: [100, 250, 500] })
    .toBeTruthy();
}

async function probeTusOffset(
  request: APIRequestContext,
  uploadUrl: string,
  capabilityToken: string,
): Promise<number> {
  try {
    const response = await request.head(uploadUrl, {
      headers: {
        'Tus-Resumable': '1.0.0',
        'X-Recantor-Upload-Token': capabilityToken,
      },
      timeout: 2_000,
    });
    if (!response.ok()) return -1;
    const raw = response.headers()['upload-offset'];
    if (!raw) return -1;
    const offset = Number.parseInt(raw, 10);
    return Number.isSafeInteger(offset) ? offset : -1;
  } catch {
    return -1;
  }
}

async function tusOffset(
  request: APIRequestContext,
  uploadUrl: string,
  capabilityToken: string,
): Promise<number> {
  const offset = await probeTusOffset(request, uploadUrl, capabilityToken);
  expect(offset).toBeGreaterThanOrEqual(0);
  return offset;
}

async function restartTusdAtOffset(
  request: APIRequestContext,
  uploadUrl: string,
  capabilityToken: string,
  expectedOffset: number,
): Promise<void> {
  await execFileAsync('docker', ['restart', 'recantor-upload-tusd']);
  await expect
    .poll(() => probeTusOffset(request, uploadUrl, capabilityToken), {
      timeout: 30_000,
      intervals: [100, 250, 500],
    })
    .toBe(expectedOffset);
}

test.describe('existing recording resumable upload', () => {
  test.skip(!uploadFixture, 'UPLOAD_E2E_FILE enables the real tusd upload proof');

  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await page.getByTestId('workflow-upload').click();
    await expect(page.getByTestId('upload-dropzone')).toBeVisible();
  });

  test('persists a real tus offset across reload and API/tusd restart, then resumes the same upload', async ({
    page,
    request,
  }) => {
    let tusCreateCount = 0;
    page.on('request', (browserRequest) => {
      if (browserRequest.method() === 'POST' && browserRequest.url() === tusEndpoint) {
        tusCreateCount += 1;
      }
    });

    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 30,
      downloadThroughput: -1,
      uploadThroughput: 512 * 1024,
    });

    await page.getByTestId('upload-file-input').setInputFiles(uploadFixture!);
    await expect(page.getByTestId('upload-selected-file')).toContainText(
      path.basename(uploadFixture!),
    );
    const createResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url() === tusEndpoint &&
        response.status() === 201,
    );
    await page.getByTestId('upload-start').click();
    const createResponse = await createResponsePromise;
    const location = await createResponse.headerValue('location');
    expect(location).toBeTruthy();
    const tusUploadUrl = new URL(location!, tusEndpoint).toString();

    await expect
      .poll(async () =>
        Number.parseInt((await page.getByTestId('upload-progress').textContent()) ?? '0'),
      )
      .toBeGreaterThan(0);
    await page.getByTestId('upload-pause').click();
    await expect(page.getByTestId('upload-message')).toContainText('Paused');

    const recovery = await activeRecovery(page);
    expect(recovery.capabilityToken).toMatch(/^[A-Za-z0-9_-]{43}$/);
    await expect
      .poll(() => probeTusOffset(request, tusUploadUrl, recovery.capabilityToken), {
        timeout: 30_000,
        intervals: [100, 250, 500],
      })
      .toBeGreaterThan(0);
    const persistedOffset = await tusOffset(request, tusUploadUrl, recovery.capabilityToken);
    expect(persistedOffset).toBeGreaterThan(0);

    await expect
      .poll(async () => (await statusFor(request, recovery)).received_bytes)
      .toBeGreaterThan(0);
    const beforeRestart = await statusFor(request, recovery);
    expect(beforeRestart.state).toBe('uploading');

    await restartApi(request);
    const afterApiRestart = await statusFor(request, recovery);
    expect(afterApiRestart.session_id).toBe(beforeRestart.session_id);
    expect(afterApiRestart.state).toBe('uploading');
    expect(afterApiRestart.received_bytes).toBeGreaterThan(0);

    await restartTusdAtOffset(request, tusUploadUrl, recovery.capabilityToken, persistedOffset);
    expect(await tusOffset(request, tusUploadUrl, recovery.capabilityToken)).toBe(persistedOffset);

    await page.reload();
    await page.getByTestId('workflow-upload').click();
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
    await page.getByTestId('upload-file-input').setInputFiles(uploadFixture!);
    const resumedPatchPromise = page.waitForRequest(
      (browserRequest) =>
        browserRequest.method() === 'PATCH' && browserRequest.url() === tusUploadUrl,
      { timeout: 30_000 },
    );
    await page.getByTestId('upload-start').click();
    const resumedPatch = await resumedPatchPromise;
    expect(resumedPatch.url()).toBe(tusUploadUrl);
    expect(Number.parseInt(resumedPatch.headers()['upload-offset'] ?? '-1', 10)).toBe(
      persistedOffset,
    );
    expect(tusCreateCount).toBe(1);

    await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
      timeout: 60_000,
    });
    await expect(page.getByTestId('upload-progress')).toHaveText('100%');
    const afterReload = await statusFor(request, recovery);
    expect(afterReload.session_id).toBe(beforeRestart.session_id);
    expect(afterReload.state).toBe('uploaded');
    expect(afterReload.completed_at).toBeTruthy();
    expect(afterReload.received_bytes).toBeGreaterThan(persistedOffset);
    expect(await tusOffset(request, tusUploadUrl, recovery.capabilityToken)).toBe(
      statSync(uploadFixture!).size,
    );
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
