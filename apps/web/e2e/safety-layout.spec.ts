import { expect, test, type Page } from '@playwright/test';

const API_BASE_URL = 'http://127.0.0.1:8000';
const CHUNK_ROUTE = '**/api/v1/sessions/*/chunks/*';
const laptopViewports = [
  { width: 1280, height: 720 },
  { width: 1366, height: 768 },
  { width: 1440, height: 900 },
];

type BrowserSession = {
  sessionId: string;
  writerId: string;
  captureEpoch: number;
  recoveryToken: string | null;
};

async function expectCriticalStateAcrossLaptopViewports(
  page: Page,
  testIds: string[],
): Promise<void> {
  for (const viewport of laptopViewports) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => window.scrollTo(0, 0));

    const dimensions = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.innerWidth);

    for (const testId of testIds) {
      const locator = page.getByTestId(testId);
      await expect(locator).toBeVisible();
      await locator.scrollIntoViewIfNeeded();
      const box = await locator.boundingBox();
      if (!box) throw new Error(`${testId} has no layout box`);
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeLessThan(viewport.height);
    }
  }
}

async function pendingCount(page: Page): Promise<number> {
  const text = await page.getByTestId('pending-chunks').textContent();
  const match = text?.match(/\d+/);
  return match ? Number(match[0]) : 0;
}

async function evictSequence(page: Page, sequence: number): Promise<void> {
  await page.evaluate(async (sequenceToEvict) => {
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
      const read = database.transaction('sessions', 'readonly');
      const sessions = (await requestResult(read.objectStore('sessions').getAll())) as Array<{
        sessionId: string;
        updatedAt: number;
      }>;
      const latest = sessions.sort((left, right) => right.updatedAt - left.updatedAt)[0];
      if (!latest) throw new Error('recording session unavailable');
      const remove = database.transaction('chunks', 'readwrite');
      remove.objectStore('chunks').delete(`${latest.sessionId}:${sequenceToEvict}`);
      await transactionDone(remove);
    } finally {
      database.close();
    }
  }, sequence);
}

async function latestBrowserSession(page: Page): Promise<BrowserSession> {
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
      const transaction = database.transaction('sessions', 'readonly');
      const sessions = (await requestResult(transaction.objectStore('sessions').getAll())) as Array<{
        sessionId: string;
        writerId: string;
        captureEpoch: number;
        recoveryToken?: string | null;
        updatedAt: number;
      }>;
      const latest = sessions.sort((left, right) => right.updatedAt - left.updatedAt)[0];
      if (!latest) throw new Error('recording session unavailable');
      return {
        sessionId: latest.sessionId,
        writerId: latest.writerId,
        captureEpoch: latest.captureEpoch,
        recoveryToken: latest.recoveryToken ?? null,
      };
    } finally {
      database.close();
    }
  });
}

async function externalClaim(page: Page, session: BrowserSession): Promise<void> {
  expect(session.recoveryToken).toBeTruthy();
  const url = `${API_BASE_URL}/api/v1/sessions/${session.sessionId}/capture/claim`;
  const response = await page.request.post(url, {
    data: {
      writer_id: 'layout-fence-writer',
      expected_epoch: session.captureEpoch,
      recovery_token: session.recoveryToken,
    },
  });
  expect(response.ok()).toBeTruthy();
}

test('missing-audio safety state fits every required laptop viewport', async ({ page }) => {
  await page.route(CHUNK_ROUTE, async (route) => route.abort('failed'));
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect
    .poll(() => pendingCount(page), { timeout: 15_000 })
    .toBeGreaterThanOrEqual(3);

  await evictSequence(page, 2);
  await page.reload();
  await expect(page.getByTestId('finish-recovered')).toBeVisible({ timeout: 10_000 });
  await page.unroute(CHUNK_ROUTE);
  await page.waitForTimeout(1_200);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('missing-sequences')).toContainText('2', { timeout: 20_000 });
  await expect(page.getByTestId('declare-missing-gaps')).toBeVisible();
  await expect(page.getByTestId('resume-recording')).toHaveCount(0);

  await expectCriticalStateAcrossLaptopViewports(page, [
    'finish-recovered',
    'declare-missing-gaps',
    'missing-sequences',
  ]);
});

test('fenced state fits every required laptop viewport', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('acked-sequence')).toContainText('through sequence 1', {
    timeout: 10_000,
  });

  await page.route(CHUNK_ROUTE, async (route) => route.abort('failed'));
  const session = await latestBrowserSession(page);
  await externalClaim(page, session);

  await expect(page.getByTestId('durability-state')).toContainText('Orphaned local evidence', {
    timeout: 15_000,
  });
  await expect(page.getByTestId('fenced-warning')).toBeVisible();
  await expect(page.getByTestId('stop-recording')).toHaveCount(0);
  await expect(page.getByTestId('resume-recording')).toHaveCount(0);
  await expect(page.getByTestId('finish-recovered')).toHaveCount(0);
  await expect(page.getByTestId('sync-recording')).toHaveCount(0);
  await expect(page.getByTestId('declare-missing-gaps')).toHaveCount(0);

  await expectCriticalStateAcrossLaptopViewports(page, ['durability-state', 'fenced-warning']);
});

test('unsafe local-spool state fits every required laptop viewport', async ({ page }) => {
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
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect(page.getByTestId('durability-state')).toContainText('Unsafe', { timeout: 10_000 });
  await expect(page.getByTestId('storage-unsafe-warning')).toBeVisible();
  await expect(page.getByTestId('finish-recovered')).toBeVisible();

  await expectCriticalStateAcrossLaptopViewports(page, [
    'durability-state',
    'finish-recovered',
    'storage-unsafe-warning',
  ]);
});
