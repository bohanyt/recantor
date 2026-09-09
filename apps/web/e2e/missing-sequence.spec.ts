import { expect, test, type Page } from '@playwright/test';

const API_BASE_URL = 'http://127.0.0.1:8000';
const CHUNK_ROUTE = '**/api/v1/sessions/*/chunks/*';

test.describe.configure({ retries: 0 });

type SerializedChunk = {
  key: string;
  sessionId: string;
  writerId: string;
  captureEpoch: number;
  sequence: number;
  monotonicStartMs: number;
  monotonicEndMs: number;
  wallEndMs: number;
  contentType: string;
  sha256: string;
  byteLength: number;
  createdAt: number;
  blobBase64: string;
};

type MissingFixture = {
  sessionId: string;
  victim: SerializedChunk;
};

type ServerState = {
  session: {
    state: string;
  };
  gaps: Array<{
    sequence_start: number | null;
    sequence_end: number | null;
    reason: string;
  }>;
};

async function pendingCount(page: Page): Promise<number> {
  const text = await page.getByTestId('pending-chunks').textContent();
  const match = text?.match(/\d+/);
  return match ? Number(match[0]) : 0;
}

async function evictSequence(page: Page, sequence: number): Promise<MissingFixture> {
  return page.evaluate(async (sequenceToEvict) => {
    function requestResult<T>(request: IDBRequest<T>): Promise<T> {
      return new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }

    function transactionDone(transaction: IDBTransaction): Promise<void> {
      return new Promise((resolve, reject) => {
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
    }

    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('recantor-recorder');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

    try {
      const read = database.transaction(['sessions', 'chunks'], 'readonly');
      const sessions = (await requestResult(read.objectStore('sessions').getAll())) as Array<{
        sessionId: string;
        updatedAt: number;
      }>;
      const latest = sessions.sort((left, right) => right.updatedAt - left.updatedAt)[0];
      if (!latest) throw new Error('recording session unavailable');
      const key = `${latest.sessionId}:${sequenceToEvict}`;
      const chunk = (await requestResult(read.objectStore('chunks').get(key))) as
        | {
            key: string;
            sessionId: string;
            writerId: string;
            captureEpoch: number;
            sequence: number;
            monotonicStartMs: number;
            monotonicEndMs: number;
            wallEndMs: number;
            contentType: string;
            sha256: string;
            byteLength: number;
            createdAt: number;
            blob: Blob;
          }
        | undefined;
      if (!chunk) throw new Error(`sequence ${sequenceToEvict} unavailable in IndexedDB`);
      const bytes = new Uint8Array(await chunk.blob.arrayBuffer());
      let binary = '';
      for (const byte of bytes) binary += String.fromCharCode(byte);

      const remove = database.transaction('chunks', 'readwrite');
      remove.objectStore('chunks').delete(key);
      await transactionDone(remove);

      return {
        sessionId: latest.sessionId,
        victim: {
          key: chunk.key,
          sessionId: chunk.sessionId,
          writerId: chunk.writerId,
          captureEpoch: chunk.captureEpoch,
          sequence: chunk.sequence,
          monotonicStartMs: chunk.monotonicStartMs,
          monotonicEndMs: chunk.monotonicEndMs,
          wallEndMs: chunk.wallEndMs,
          contentType: chunk.contentType,
          sha256: chunk.sha256,
          byteLength: chunk.byteLength,
          createdAt: chunk.createdAt,
          blobBase64: btoa(binary),
        },
      };
    } finally {
      database.close();
    }
  }, sequence);
}

async function restoreSequence(page: Page, chunk: SerializedChunk): Promise<void> {
  await page.evaluate(async (serialized) => {
    function transactionDone(transaction: IDBTransaction): Promise<void> {
      return new Promise((resolve, reject) => {
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
    }

    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('recantor-recorder');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

    try {
      const binary = atob(serialized.blobBase64);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      const transaction = database.transaction('chunks', 'readwrite');
      transaction.objectStore('chunks').put({
        key: serialized.key,
        sessionId: serialized.sessionId,
        writerId: serialized.writerId,
        captureEpoch: serialized.captureEpoch,
        sequence: serialized.sequence,
        monotonicStartMs: serialized.monotonicStartMs,
        monotonicEndMs: serialized.monotonicEndMs,
        wallEndMs: serialized.wallEndMs,
        contentType: serialized.contentType,
        sha256: serialized.sha256,
        byteLength: serialized.byteLength,
        createdAt: serialized.createdAt,
        blob: new Blob([bytes], { type: serialized.contentType }),
      });
      await transactionDone(transaction);
    } finally {
      database.close();
    }
  }, chunk);
}

async function fetchServerState(page: Page, sessionId: string): Promise<ServerState> {
  const response = await page.request.get(
    `${API_BASE_URL}/api/v1/sessions/${sessionId}/recording-state`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as ServerState;
}

async function createMissingMiddleFragment(page: Page): Promise<MissingFixture> {
  await page.route(CHUNK_ROUTE, async (route) => route.abort('failed'));
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect.poll(() => pendingCount(page), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);

  const fixture = await evictSequence(page, 2);
  await page.reload();
  await expect(page.getByTestId('finish-recovered')).toBeVisible({ timeout: 10_000 });
  await page.unroute(CHUNK_ROUTE);

  // Make the interruption interval unambiguously long enough that the first recovery attempt
  // records its normal wall-clock interruption evidence. Subsequent retries must not amplify it.
  await page.waitForTimeout(1_200);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('missing-sequences')).toContainText('2', { timeout: 20_000 });
  await expect(page.getByTestId('declare-missing-gaps')).toBeVisible();
  await expect(page.getByTestId('resume-recording')).toHaveCount(0);
  return fixture;
}

test('surfaces a missing middle sequence and only completes after explicit gap declaration', async ({
  page,
}) => {
  const fixture = await createMissingMiddleFragment(page);
  const firstFailure = await fetchServerState(page, fixture.sessionId);
  expect(firstFailure.session.state).toBe('finalizing');
  const stableGapCount = firstFailure.gaps.length;

  await page.waitForTimeout(1_200);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('missing-sequences')).toContainText('2');
  const secondFailure = await fetchServerState(page, fixture.sessionId);
  expect(secondFailure.gaps).toHaveLength(stableGapCount);

  await page.waitForTimeout(1_200);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('missing-sequences')).toContainText('2');
  const thirdFailure = await fetchServerState(page, fixture.sessionId);
  expect(thirdFailure.gaps).toHaveLength(stableGapCount);

  await page.getByTestId('declare-missing-gaps').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  const complete = await fetchServerState(page, fixture.sessionId);
  expect(complete.session.state).toBe('complete');
  expect(
    complete.gaps.filter(
      (gap) =>
        gap.sequence_start === 2 &&
        gap.sequence_end === 2 &&
        gap.reason === 'client_declared_missing_at_finalize',
    ),
  ).toHaveLength(1);
});

test('late local recovery uploads the missing sequence and completes without declaring it lost', async ({
  page,
}) => {
  const fixture = await createMissingMiddleFragment(page);
  const beforeRestore = await fetchServerState(page, fixture.sessionId);
  const gapCountBeforeRestore = beforeRestore.gaps.length;

  await restoreSequence(page, fixture.victim);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });

  const complete = await fetchServerState(page, fixture.sessionId);
  expect(complete.session.state).toBe('complete');
  expect(complete.gaps).toHaveLength(gapCountBeforeRestore);
  expect(
    complete.gaps.some(
      (gap) => gap.sequence_start === 2 && gap.sequence_end === 2,
    ),
  ).toBe(false);
});
