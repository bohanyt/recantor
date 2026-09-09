import { expect, test } from '@playwright/test';

test('records microphone audio through durable ACK and clean finalization', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

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
      expect(page.getByTestId('recorder-message')).toContainText('Recording capture generation 1'),
      expect(secondPage.getByTestId('recorder-message')).toContainText(
        'Recording capture generation 1',
      ),
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
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

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
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

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

test('starts a fresh fenced capture generation after the recording tab disappears', async ({
  context,
}) => {
  const recordingPage = await context.newPage();
  await recordingPage.goto('/');
  await recordingPage.getByTestId('start-recording').click();
  await expect(recordingPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await recordingPage.waitForTimeout(2_600);
  await recordingPage.close();

  const recoveryPage = await context.newPage();
  await recoveryPage.goto('/');
  await expect(recoveryPage.getByTestId('resume-recording')).toBeVisible({ timeout: 10_000 });
  await recoveryPage.getByTestId('resume-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 2',
    { timeout: 10_000 },
  );
  await recoveryPage.waitForTimeout(2_200);
  await recoveryPage.getByTestId('stop-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('retries a lost successful capture claim without incrementing the generation twice', async ({
  context,
}) => {
  const recordingPage = await context.newPage();
  await recordingPage.goto('/');
  await recordingPage.getByTestId('start-recording').click();
  await expect(recordingPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await recordingPage.waitForTimeout(2_600);
  await recordingPage.close();

  const recoveryPage = await context.newPage();
  let claimCalls = 0;
  await recoveryPage.route('**/api/v1/sessions/*/capture/claim', async (route) => {
    claimCalls += 1;
    if (claimCalls === 1) {
      const response = await route.fetch();
      expect(response.ok()).toBeTruthy();
      await route.abort('failed');
      return;
    }
    await route.continue();
  });

  await recoveryPage.goto('/');
  await expect(recoveryPage.getByTestId('resume-recording')).toBeVisible({ timeout: 10_000 });
  await recoveryPage.getByTestId('resume-recording').click();
  await expect(recoveryPage.getByRole('alert')).toBeVisible({ timeout: 10_000 });
  await expect(recoveryPage.getByTestId('resume-recording')).toBeVisible();

  await recoveryPage.getByTestId('resume-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 2',
    { timeout: 10_000 },
  );
  expect(claimCalls).toBe(2);

  await recoveryPage.waitForTimeout(2_200);
  await recoveryPage.getByTestId('stop-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('does not claim a new live generation until old recovery chunks are durably ACKed', async ({
  context,
}) => {
  await context.route('**/api/v1/sessions/*/chunks/*', async (route) => route.abort('failed'));

  const recordingPage = await context.newPage();
  await recordingPage.goto('/');
  await recordingPage.getByTestId('start-recording').click();
  await expect(recordingPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await expect
    .poll(async () => recordingPage.getByTestId('pending-chunks').textContent(), {
      timeout: 10_000,
    })
    .not.toContain('0 fragments');
  await recordingPage.close();

  const recoveryPage = await context.newPage();
  let claimCalls = 0;
  await recoveryPage.route('**/api/v1/sessions/*/capture/claim', async (route) => {
    claimCalls += 1;
    await route.continue();
  });
  await recoveryPage.goto('/');
  await recoveryPage.getByTestId('resume-recording').click();
  await expect(recoveryPage.getByRole('alert')).toContainText('must receive a durable ACK', {
    timeout: 15_000,
  });
  expect(claimCalls).toBe(0);

  await context.unroute('**/api/v1/sessions/*/chunks/*');
  await recoveryPage.getByTestId('resume-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 2',
    { timeout: 20_000 },
  );
  expect(claimCalls).toBe(1);

  await recoveryPage.waitForTimeout(2_200);
  await recoveryPage.getByTestId('stop-recording').click();
  await expect(recoveryPage.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});

test('a second same-origin tab cannot silently become the active recorder', async ({
  page,
  context,
}) => {
  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

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
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

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
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );

  await expect(page.getByTestId('durability-state')).toContainText('Unsafe', { timeout: 10_000 });
  await expect(page.getByTestId('recorder-message')).toContainText('Capture stopped', {
    timeout: 10_000,
  });
  await expect(page.getByTestId('finish-recovered')).toBeVisible();

  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Explicit interruption evidence',
    {
      timeout: 20_000,
    },
  );
});

test('turns an unexpected microphone-track end into visible recoverable gap evidence', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const mediaDevices = navigator.mediaDevices;
    const original = mediaDevices.getUserMedia.bind(mediaDevices);
    mediaDevices.getUserMedia = async (...args) => {
      const stream = await original(...args);
      (window as unknown as { __recantorTestStream?: MediaStream }).__recantorTestStream = stream;
      return stream;
    };
  });

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
  );
  await page.waitForTimeout(2_600);

  await page.evaluate(() => {
    const stream = (window as unknown as { __recantorTestStream?: MediaStream })
      .__recantorTestStream;
    const track = stream?.getAudioTracks()[0];
    if (!track) throw new Error('test microphone track unavailable');
    track.dispatchEvent(new Event('ended'));
  });

  await expect(page.getByTestId('recorder-message')).toContainText(
    'Capture stopped after a browser or microphone interruption',
    { timeout: 10_000 },
  );
  await expect(page.getByTestId('finish-recovered')).toBeVisible();
  await page.waitForTimeout(1_200);
  await page.getByTestId('finish-recovered').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Explicit interruption evidence',
    { timeout: 20_000 },
  );
});

test('allows retry after an initial start request failure', async ({ page }) => {
  let createCalls = 0;
  await page.route('**/api/v1/sessions/live', async (route) => {
    createCalls += 1;
    if (createCalls === 1) {
      await route.abort('failed');
      return;
    }
    await route.continue();
  });

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByRole('alert')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('start-recording')).toBeVisible();

  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText(
    'Recording capture generation 1',
    { timeout: 10_000 },
  );
  expect(createCalls).toBe(2);

  await page.waitForTimeout(2_200);
  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
});
