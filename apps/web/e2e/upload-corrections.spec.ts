import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test';
import { statSync, writeFileSync } from 'node:fs';

const uploadFixture = process.env.UPLOAD_E2E_FILE;
const expectedPipelineText =
  process.env.UPLOAD_E2E_TRANSCRIPT_TEXT ?? 'deterministic upload fixture transcript';
const apiBaseUrl = process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';
const storeKey = 'recantor:upload-recovery:v1';
const failedSessionId = '11111111-2222-4333-8444-555555555555';
const freshSessionId = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const unrelatedSessionId = '99999999-8888-4777-8666-555555555555';
const failedToken = 'Q'.repeat(43);
const unrelatedToken = 'R'.repeat(43);

type RecoveryEntry = {
  fingerprint: string;
  clientRequestId: string;
  capabilityToken: string;
  sessionId: string | null;
  expiresAt: string | null;
  updatedAt: string;
  durableCompletedAt: string | null;
};

type CreateUploadBody = {
  client_request_id: string;
  capability_token: string;
  original_filename: string;
  content_type: string;
  byte_length: number;
};

async function openUpload(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByTestId('workflow-upload').click();
  await expect(page.getByTestId('upload-dropzone')).toBeVisible();
}

async function onlyRecovery(page: Page): Promise<RecoveryEntry> {
  return page.evaluate((key) => {
    const raw = window.localStorage.getItem(key);
    if (!raw) throw new Error('upload recovery state is missing');
    const entries = Object.values(JSON.parse(raw)) as RecoveryEntry[];
    if (entries.length !== 1) throw new Error(`expected one recovery entry, got ${entries.length}`);
    return entries[0];
  }, storeKey);
}

async function resultStatus(
  request: APIRequestContext,
  recovery: RecoveryEntry,
): Promise<{ session_id: string; state: string; transcript_segment_count: number }> {
  if (!recovery.sessionId) throw new Error('upload recovery has no session id');
  const response = await request.get(`${apiBaseUrl}/api/v1/uploads/${recovery.sessionId}/result`, {
    headers: { 'X-Recantor-Upload-Token': recovery.capabilityToken },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as {
    session_id: string;
    state: string;
    transcript_segment_count: number;
  };
}

test('real decodable upload reaches visible canonical transcript through the ordinary pipeline', async ({
  page,
  request,
}) => {
  test.skip(!uploadFixture, 'UPLOAD_E2E_FILE enables the real fixture-to-visible-result proof');

  await openUpload(page);
  await page.evaluate(() => window.localStorage.clear());
  await page.reload();
  await page.getByTestId('workflow-upload').click();

  await page.getByTestId('upload-file-input').setInputFiles(uploadFixture!);
  await page.getByTestId('upload-start').click();

  await expect(page.getByTestId('upload-progress')).toHaveText('100%', { timeout: 90_000 });
  await expect(page.getByTestId('upload-phase')).toHaveText('Complete', { timeout: 180_000 });
  await expect(page.getByText(expectedPipelineText)).toBeVisible();
  await expect(page.getByTestId('upload-result')).toBeVisible();
  for (const exportFormat of ['txt', 'json', 'vtt', 'srt']) {
    await expect(page.getByTestId(`upload-export-${exportFormat}`)).toBeVisible();
  }

  const recovery = await onlyRecovery(page);
  expect(recovery.capabilityToken).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(recovery.sessionId).toBeTruthy();
  expect(recovery.durableCompletedAt).toBeTruthy();

  const status = await resultStatus(request, recovery);
  expect(status.session_id).toBe(recovery.sessionId);
  expect(status.state).toBe('complete');
  expect(status.transcript_segment_count).toBeGreaterThan(0);
});

test('terminal processing failure starts a genuinely fresh upload identity without repolling the failed result', async ({
  page,
}, testInfo) => {
  const fixturePath = testInfo.outputPath('processing-failed.wav');
  writeFileSync(fixturePath, Buffer.alloc(4096, 7));
  const fixtureStat = statSync(fixturePath);

  await openUpload(page);
  await page.getByTestId('upload-file-input').setInputFiles(fixturePath);
  const selected = await page.getByTestId('upload-file-input').evaluate((input) => {
    const file = (input as HTMLInputElement).files?.[0];
    if (!file) throw new Error('test fixture was not selected');
    return {
      name: file.name,
      size: file.size,
      lastModified: file.lastModified,
      type: file.type || 'application/octet-stream',
    };
  });
  expect(selected.size).toBe(fixtureStat.size);
  const fingerprint = [selected.name, selected.size, selected.lastModified, selected.type].join(
    ':',
  );
  const failedRecovery: RecoveryEntry = {
    fingerprint,
    clientRequestId: '12345678-1234-4234-8234-123456789abc',
    capabilityToken: failedToken,
    sessionId: failedSessionId,
    expiresAt: '2099-01-01T00:00:00Z',
    updatedAt: '2098-12-31T23:59:59Z',
    durableCompletedAt: '2026-09-16T02:00:00Z',
  };
  const unrelatedRecovery: RecoveryEntry = {
    fingerprint: 'unrelated.wav:128:1700000000000:audio/wav',
    clientRequestId: '87654321-4321-4321-8321-cba987654321',
    capabilityToken: unrelatedToken,
    sessionId: unrelatedSessionId,
    expiresAt: '2099-01-01T00:00:00Z',
    updatedAt: '2026-09-15T00:00:00Z',
    durableCompletedAt: null,
  };

  let failedResultRequests = 0;
  let oldExportRequests = 0;
  await page.route(`**/api/v1/uploads/${failedSessionId}/result`, async (route) => {
    failedResultRequests += 1;
    expect(route.request().headers()['x-recantor-upload-token']).toBe(failedToken);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: failedSessionId,
        state: 'failed',
        transcript_segment_count: 0,
        exports_available: false,
        completed_at: '2026-09-16T02:00:01Z',
        expires_at: '2099-01-01T00:00:00Z',
        failure_message: 'Transcription could not be completed. Start a fresh upload to try again.',
      }),
    });
  });
  page.on('request', (browserRequest) => {
    if (browserRequest.url().includes(`/api/v1/uploads/${failedSessionId}/exports/`)) {
      oldExportRequests += 1;
    }
  });

  await page.evaluate(
    ({ key, failed, unrelated }) => {
      window.localStorage.setItem(
        key,
        JSON.stringify({ [failed.fingerprint]: failed, [unrelated.fingerprint]: unrelated }),
      );
    },
    { key: storeKey, failed: failedRecovery, unrelated: unrelatedRecovery },
  );
  await page.reload();
  await page.getByTestId('workflow-upload').click();

  await expect(page.getByTestId('upload-phase')).toHaveText('Failed');
  await expect(page.getByTestId('upload-message')).toContainText('Start a fresh upload');
  await expect(page.getByTestId('upload-result')).toHaveCount(0);
  for (const exportFormat of ['txt', 'json', 'vtt', 'srt']) {
    await expect(page.getByTestId(`upload-export-${exportFormat}`)).toHaveCount(0);
  }
  expect(failedResultRequests).toBe(1);
  expect(oldExportRequests).toBe(0);

  await page.getByTestId('upload-file-input').setInputFiles(fixturePath);
  await expect(page.getByTestId('upload-phase')).toHaveText('Failed');
  await expect(page.getByTestId('upload-start')).toHaveText('Start fresh upload');

  let createBody: CreateUploadBody | null = null;
  await page.route('**/api/v1/uploads', async (route: Route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    createBody = route.request().postDataJSON() as CreateUploadBody;
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: freshSessionId,
        client_request_id: createBody.client_request_id,
        kind: 'upload',
        state: 'uploading',
        original_filename: createBody.original_filename,
        content_type: createBody.content_type,
        declared_byte_length: createBody.byte_length,
        declared_duration_ms: null,
        received_bytes: 0,
        expires_at: '2099-01-01T00:00:00Z',
        completed_at: null,
        failure_code: null,
        failure_message: null,
        upload_endpoint: 'http://127.0.0.1:1080/files/',
      }),
    });
  });

  let tusToken = '';
  await page.route('http://127.0.0.1:1080/files/', async (route) => {
    if (route.request().method() === 'OPTIONS') {
      await route.fulfill({
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST,OPTIONS',
          'Access-Control-Allow-Headers':
            'Tus-Resumable,Upload-Length,Upload-Metadata,X-Recantor-Upload-Token',
        },
      });
      return;
    }
    tusToken = route.request().headers()['x-recantor-upload-token'] ?? '';
    await route.fulfill({
      status: 400,
      headers: { 'Access-Control-Allow-Origin': '*' },
      body: 'identity proof stops before transfer',
    });
  });

  await page.getByTestId('upload-start').click();
  await expect.poll(() => createBody).not.toBeNull();
  expect(createBody!.client_request_id).not.toBe(failedRecovery.clientRequestId);
  expect(createBody!.capability_token).not.toBe(failedToken);
  expect(createBody!.capability_token).toMatch(/^[A-Za-z0-9_-]{43}$/);
  await expect.poll(() => tusToken).toBe(createBody!.capability_token);

  const persisted = await page.evaluate(
    (key) => JSON.parse(window.localStorage.getItem(key) ?? '{}'),
    storeKey,
  );
  expect(Object.keys(persisted)).toHaveLength(2);
  expect(persisted[fingerprint].sessionId).toBe(freshSessionId);
  expect(persisted[fingerprint].clientRequestId).toBe(createBody!.client_request_id);
  expect(persisted[fingerprint].capabilityToken).toBe(createBody!.capability_token);
  expect(persisted[unrelatedRecovery.fingerprint]).toEqual(unrelatedRecovery);

  await page.waitForTimeout(1_300);
  expect(failedResultRequests).toBe(1);
  expect(oldExportRequests).toBe(0);
});
