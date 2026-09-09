import { createHash } from 'node:crypto';

import { expect, test, type Page } from '@playwright/test';

const API_BASE_URL = 'http://127.0.0.1:8000';
const CHUNK_ROUTE = '**/api/v1/sessions/*/chunks/*';
const HEARTBEAT_ROUTE = '**/api/v1/sessions/*/heartbeat';

test.describe.configure({ retries: 0 });

type BrowserSession = {
  sessionId: string;
  writerId: string;
  captureEpoch: number;
  recoveryToken: string | null;
};

type BrowserChunk = {
  sequence: number;
  captureEpoch: number;
  sha256: string;
  byteLength: number;
};

async function readRecorderState(page: Page): Promise<{
  session: BrowserSession | null;
  chunks: BrowserChunk[];
}> {
  return page.evaluate(async () => {
    function requestResult<T>(request: IDBRequest<T>): Promise<T> {
      return new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }

    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('recantor-recorder');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

    try {
      const transaction = database.transaction(['sessions', 'chunks'], 'readonly');
      const sessions = (await requestResult(transaction.objectStore('sessions').getAll())) as Array<{
        sessionId: string;
        writerId: string;
        captureEpoch: number;
        recoveryToken?: string | null;
        updatedAt: number;
      }>;
      const chunks = (await requestResult(transaction.objectStore('chunks').getAll())) as Array<{
        sequence: number;
        captureEpoch: number;
        sha256: string;
        byteLength: number;
      }>;
      const latest = sessions.sort((left, right) => right.updatedAt - left.updatedAt)[0];
      return {
        session: latest
          ? {
              sessionId: latest.sessionId,
              writerId: latest.writerId,
              captureEpoch: latest.captureEpoch,
              recoveryToken: latest.recoveryToken ?? null,
            }
          : null,
        chunks: chunks
          .map(({ sequence, captureEpoch, sha256, byteLength }) => ({
            sequence,
            captureEpoch,
            sha256,
            byteLength,
          }))
          .sort((left, right) => left.sequence - right.sequence),
      };
    } finally {
      database.close();
    }
  });
}

async function externalClaim(page: Page, session: BrowserSession, writerId: string): Promise<number> {
  expect(session.recoveryToken).toBeTruthy();
  const response = await page.request.post(
    `${API_BASE_URL}/api/v1/sessions/${session.sessionId}/capture/claim`,
    {
      data: {
        writer_id: writerId,
        expected_epoch: session.captureEpoch,
        recovery_token: session.recoveryToken,
      },
    },
  );
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { capture_epoch: number };
  return body.capture_epoch;
}

async function expectFencedUi(page: Page): Promise<void> {
  await expect(page.getByTestId('durability-state')).toContainText('Orphaned local evidence', {
    timeout: 15_000,
  });
  await expect(page.getByTestId('recorder-message')).toContainText('ownership changed');
  await expect(page.getByTestId('capture-lock-state')).toHaveText('not held', { timeout: 10_000 });
  await expect(page.getByTestId('stop-recording')).toHaveCount(0);
  await expect(page.getByTestId('resume-recording')).toHaveCount(0);
  await expect(page.getByTestId('finish-recovered')).toHaveCount(0);
  await expect(page.getByTestId('sync-recording')).toHaveCount(0);
}

test('heartbeat fencing stops capture and labels retained audio as orphaned evidence', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('acked-sequence')).toContainText('through sequence 1', {
    timeout: 10_000,
  });

  await page.route(CHUNK_ROUTE, async (route) => route.abort('failed'));
  const beforeClaim = await readRecorderState(page);
  expect(beforeClaim.session).not.toBeNull();
  await externalClaim(page, beforeClaim.session!, 'external-heartbeat-writer');

  await expectFencedUi(page);
  await expect
    .poll(async () => (await readRecorderState(page)).chunks.length, { timeout: 10_000 })
    .toBeGreaterThan(0);
});

test('chunk fencing retains conflicting old-epoch audio through reconciliation', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('acked-sequence')).toContainText('through sequence 1', {
    timeout: 10_000,
  });

  await page.route(HEARTBEAT_ROUTE, async (route) => route.abort('failed'));
  const beforeClaim = await readRecorderState(page);
  expect(beforeClaim.session).not.toBeNull();
  const newWriterId = 'external-chunk-writer';
  const newEpoch = await externalClaim(page, beforeClaim.session!, newWriterId);

  await expectFencedUi(page);
  const fencedState = await expect
    .poll(async () => {
      const state = await readRecorderState(page);
      return state.chunks.find((chunk) => chunk.captureEpoch === beforeClaim.session!.captureEpoch) ?? null;
    }, { timeout: 10_000 })
    .not.toBeNull();

  const afterFence = await readRecorderState(page);
  const victim = afterFence.chunks.find(
    (chunk) => chunk.captureEpoch === beforeClaim.session!.captureEpoch && chunk.sequence >= 2,
  );
  expect(victim).toBeTruthy();

  const replacement = Buffer.from(`new-generation-sequence-${victim!.sequence}`);
  const replacementSha = createHash('sha256').update(replacement).digest('hex');
  expect(replacementSha).not.toBe(victim!.sha256);
  const params = new URLSearchParams({
    writer_id: newWriterId,
    capture_epoch: String(newEpoch),
    monotonic_start_ms: '100000',
    monotonic_end_ms: '101000',
    sha256: replacementSha,
    content_type: 'audio/webm',
  });
  const replacementResponse = await page.request.put(
    `${API_BASE_URL}/api/v1/sessions/${beforeClaim.session!.sessionId}/chunks/${victim!.sequence}?${params}`,
    {
      headers: { 'content-type': 'application/octet-stream' },
      data: replacement,
    },
  );
  expect(replacementResponse.ok()).toBeTruthy();

  await page.reload();
  await expectFencedUi(page);
  const afterReconcile = await readRecorderState(page);
  expect(
    afterReconcile.chunks.some(
      (chunk) =>
        chunk.sequence === victim!.sequence &&
        chunk.captureEpoch === victim!.captureEpoch &&
        chunk.sha256 === victim!.sha256 &&
        chunk.byteLength === victim!.byteLength,
    ),
  ).toBe(true);
});
