import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const formatterTargets = [
  'e2e/product-shell.spec.ts',
  'src/App.tsx',
  'src/RecorderPanel.tsx',
  'src/recording/recorderUx.test.ts',
  'src/recording/recorderUx.ts',
  'src/UploadPanel.tsx',
];

test('temporary CI formatter diff capture', () => {
  test.skip(process.env.CI !== 'true', 'Formatter capture only runs on CI.');
  const repositoryRoot = process.env.GITHUB_WORKSPACE ?? path.resolve(process.cwd(), '../..');
  const webRoot = path.join(repositoryRoot, 'apps/web');
  execFileSync('pnpm', ['exec', 'prettier', '--write', ...formatterTargets], {
    cwd: webRoot,
    encoding: 'utf8',
  });
  const repositoryTargets = formatterTargets.map((target) => `apps/web/${target}`);
  const diff = execFileSync('git', ['diff', '--no-color', '--', ...repositoryTargets], {
    cwd: repositoryRoot,
    encoding: 'utf8',
  });
  console.log(`PRETTIER_DIFF_CAPTURE_START\n${diff}\nPRETTIER_DIFF_CAPTURE_END`);
});

const laptopViewports = [
  { width: 1280, height: 720 },
  { width: 1366, height: 768 },
  { width: 1440, height: 900 },
];

for (const viewport of laptopViewports) {
  test(`product shell fits ${viewport.width}x${viewport.height} without horizontal scroll`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');

    await expect(page.getByTestId('workflow-live')).toHaveAttribute('aria-current', 'page');
    await expect(page.getByTestId('start-recording')).toBeVisible();
    await expect(page.getByTestId('durability-state')).toBeVisible();
    await expect(page.getByTestId('transcription-state')).toBeVisible();
    await expect(page.getByTestId('live-transcript-panel')).toBeVisible();
    await expect(page.getByTestId('diagnostics')).not.toHaveAttribute('open');

    const dimensions = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.innerWidth);

    const critical = await Promise.all(
      ['start-recording', 'durability-state', 'transcription-state'].map(async (testId) => {
        const box = await page.getByTestId(testId).boundingBox();
        if (!box) throw new Error(`${testId} has no layout box`);
        return { testId, box };
      }),
    );
    for (const { testId, box } of critical) {
      expect(box.x, `${testId} starts outside viewport`).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, `${testId} overflows viewport`).toBeLessThanOrEqual(viewport.width);
      expect(box.y, `${testId} starts above viewport`).toBeGreaterThanOrEqual(0);
      expect(box.y, `${testId} is below the initial laptop viewport`).toBeLessThan(viewport.height);
    }
  });
}

test('workflow navigation, diagnostics, and focus treatment are keyboard reachable', async ({ page }) => {
  await page.goto('/');

  await page.keyboard.press('Tab');
  await expect(page.getByTestId('workflow-live')).toBeFocused();
  const focusOutline = await page.getByTestId('workflow-live').evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: style.outlineWidth };
  });
  expect(focusOutline.style).not.toBe('none');
  expect(Number.parseFloat(focusOutline.width)).toBeGreaterThanOrEqual(3);

  await page.keyboard.press('Tab');
  await expect(page.getByTestId('workflow-upload')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('workflow-upload')).toHaveAttribute('aria-current', 'page');
  await expect(page.getByTestId('upload-dropzone')).toBeVisible();
  await expect(page.getByText(/uploaded-media processing is not available/i)).toBeVisible();

  await page.getByTestId('workflow-live').click();
  const diagnostics = page.getByTestId('diagnostics');
  const summary = diagnostics.locator('summary');
  await summary.focus();
  await expect(summary).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(diagnostics).toHaveAttribute('open');
  await expect(page.getByTestId('health-state')).toBeVisible();
});

test('normal UI omits stale phase/developer setup copy', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('body')).not.toContainText('Phase 1 reliable capture');
  await expect(page.locator('body')).not.toContainText('Durable audio first, intelligence second');
  await expect(page.locator('body')).not.toContainText('STT_PRIMARY_PROVIDER');
  await expect(page.locator('body')).not.toContainText('STT_FALLBACK_PROVIDER');
});

test('README quick-start GROQ key reaches only server-side Compose services', () => {
  test.skip(process.env.CI !== 'true', 'Exact .env auto-loading proof runs only in clean CI checkouts.');

  const root = process.env.GITHUB_WORKSPACE ?? path.resolve(process.cwd(), '../..');
  const envPath = path.join(root, '.env');
  expect(existsSync(envPath), 'CI checkout unexpectedly already contains .env').toBe(false);
  writeFileSync(envPath, 'GROQ_API_KEY=fake-local-config-check\n', { encoding: 'utf8', mode: 0o600 });

  try {
    const childEnv = { ...process.env };
    delete childEnv.GROQ_API_KEY;
    const rendered = execFileSync(
      'docker',
      ['compose', '-f', 'infra/compose.yaml', 'config', '--format', 'json'],
      { cwd: root, env: childEnv, encoding: 'utf8' },
    );
    const config = JSON.parse(rendered) as {
      services: Record<string, { environment?: Record<string, string> }>;
    };
    expect(config.services.api?.environment?.GROQ_API_KEY).toBe('fake-local-config-check');
    expect(config.services['stt-worker']?.environment?.GROQ_API_KEY).toBe(
      'fake-local-config-check',
    );
    expect(config.services.web?.environment?.GROQ_API_KEY).toBeUndefined();
  } finally {
    rmSync(envPath, { force: true });
  }
});
