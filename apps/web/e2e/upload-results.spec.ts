import { expect, test, type Page, type Route } from '@playwright/test';

const sessionId = '11111111-2222-4333-8444-555555555555';
const token = 'Q'.repeat(43);
const storeKey = 'recantor:upload-recovery:v1';
const processingStates = ['preparing', 'transcribing', 'complete'] as const;

type ResultState = 'preparing' | 'transcribing' | 'complete' | 'no_speech' | 'failed';

function recoveryEntry() {
  const now = new Date().toISOString();
  return {
    fingerprint: 'result.wav:4096:1700000000000:audio/wav',
    clientRequestId: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    capabilityToken: token,
    sessionId,
    expiresAt: '2099-01-01T00:00:00Z',
    updatedAt: now,
    durableCompletedAt: now,
  };
}

async function seedRecovery(page: Page): Promise<void> {
  const entry = recoveryEntry();
  await page.addInitScript(
    ({ key, value }) => window.localStorage.setItem(key, JSON.stringify(value)),
    { key: storeKey, value: { [entry.fingerprint]: entry } },
  );
}

function assertCapability(route: Route): void {
  expect(route.request().headers()['x-recantor-upload-token']).toBe(token);
}

function resultBody(state: ResultState) {
  return {
    session_id: sessionId,
    state,
    transcript_segment_count: state === 'complete' ? 1 : 0,
    exports_available: state === 'complete' || state === 'no_speech',
    completed_at: ['complete', 'no_speech', 'failed'].includes(state)
      ? '2026-09-14T05:00:00Z'
      : null,
    expires_at: '2099-01-01T00:00:00Z',
    failure_message: state === 'failed' ? 'Transcription could not be completed.' : null,
  };
}

async function openUpload(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByTestId('workflow-upload').click();
}

test(
  'reload during processing resumes the same capability and reaches visible canonical transcript',
  async ({ page }) => {
    await seedRecovery(page);
    let stage: 0 | 1 | 2 = 0;
    const seenTokens: string[] = [];

    await page.route(`**/api/v1/uploads/${sessionId}/result`, async (route) => {
      seenTokens.push(route.request().headers()['x-recantor-upload-token'] ?? '');
      assertCapability(route);
      const state = processingStates[stage];
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(resultBody(state)),
      });
    });
    await page.route(`**/api/v1/uploads/${sessionId}/transcript?**`, async (route) => {
      assertCapability(route);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: sessionId,
          after_sequence: 0,
          next_after_sequence: 1,
          has_more: false,
          segments: [
            {
              id: '99999999-8888-4777-8666-555555555555',
              sequence: 1,
              start_ms: 125,
              end_ms: 2125,
              text: 'canonical upload transcript',
              language: 'id',
              created_at: '2026-09-14T05:00:00Z',
            },
          ],
        }),
      });
    });
    await page.route(`**/api/v1/uploads/${sessionId}/exports/json`, async (route) => {
      assertCapability(route);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ session_id: sessionId, segments: [] }),
      });
    });

    await openUpload(page);
    await expect(page.getByTestId('upload-phase')).toHaveText('Preparing audio');

    stage = 1;
    await expect(page.getByTestId('upload-phase')).toHaveText('Transcribing', { timeout: 5_000 });

    await page.reload();
    await page.getByTestId('workflow-upload').click();
    await expect(page.getByTestId('upload-phase')).toHaveText('Transcribing');

    stage = 2;
    await expect(page.getByTestId('upload-phase')).toHaveText('Complete', { timeout: 5_000 });
    await expect(page.getByText('canonical upload transcript')).toBeVisible();
    await expect(page.getByTestId('upload-result')).toContainText('1 canonical transcript segment');

    const exportRequest = page.waitForRequest((request) =>
      request.url().endsWith(`/api/v1/uploads/${sessionId}/exports/json`),
    );
    await page.getByTestId('upload-export-json').click();
    expect((await exportRequest).headers()['x-recantor-upload-token']).toBe(token);
    expect(seenTokens.length).toBeGreaterThanOrEqual(3);
    expect(seenTokens.every((seen) => seen === token)).toBeTruthy();

    const persisted = await page.evaluate(
      (key) => JSON.parse(window.localStorage.getItem(key) ?? '{}'),
      storeKey,
    );
    expect(Object.values(persisted)).toHaveLength(1);
  },
);

test(
  '410 clears browser capability recovery, stops polling, and tells the truth about server data',
  async ({ page }) => {
    await seedRecovery(page);
    let resultRequests = 0;
    await page.route(`**/api/v1/uploads/${sessionId}/result`, async (route) => {
      resultRequests += 1;
      assertCapability(route);
      await route.fulfill({
        status: 410,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'upload capability has expired' }),
      });
    });

    await openUpload(page);
    await expect(page.getByTestId('upload-phase')).toHaveText('Access expired');
    await expect(page.getByTestId('upload-message')).toContainText(
      'server recording was not deleted',
    );
    expect(await page.evaluate((key) => window.localStorage.getItem(key), storeKey)).toBe('{}');
    await page.waitForTimeout(1_300);
    expect(resultRequests).toBe(1);
  },
);

test('temporary result fetch failure reconnects without changing the saved session', async ({
  page,
}) => {
  await seedRecovery(page);
  let attempts = 0;
  await page.route(`**/api/v1/uploads/${sessionId}/result`, async (route) => {
    attempts += 1;
    assertCapability(route);
    if (attempts === 1) {
      await route.abort('failed');
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(resultBody('no_speech')),
    });
  });

  await openUpload(page);
  await expect(page.getByTestId('upload-phase')).toHaveText('Reconnecting');
  await expect(page.getByTestId('upload-phase')).toHaveText('No speech detected', {
    timeout: 5_000,
  });
  await expect(page.getByTestId('upload-no-speech')).toContainText('No speech was detected');
  expect(attempts).toBe(2);
});
