import { expect, test, type Page, type Route } from '@playwright/test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';

// Deterministic, Docker-free proof of #61. The browser side is real: a genuine
// FileSystemFileHandle (an origin-private-file-system file) is stored in IndexedDB, survives a
// reload, and is reopened by the app. Only the servers are faked: a small in-test API (Playwright
// routes) and a real local HTTP tus server on an ephemeral port, so a non-zero tus offset can be
// produced and observed byte-exactly. The tus side is a real socket server rather than a Playwright
// route because Chromium can crash when a page holding an intercepted request with an OPFS-backed
// body is torn down. Real tusd + a real Windows Chrome/Edge permission lifecycle are separate gates.
//
// The persisted-handle tests are opt-in (RECANTOR_E2E_FILE_HANDLES=1) because Chromium 153 builds
// (Playwright's bundled Chromium and Chrome 153.0.8010.x) crash the browser process when an OPFS
// FileSystemFileHandle is read back from IndexedDB after a reload. Microsoft Edge 154 does not, so
// run them with e.g.  PLAYWRIGHT_CHANNEL=msedge RECANTOR_E2E_FILE_HANDLES=1 pnpm test:e2e
// e2e/upload-handle-recovery.spec.ts   The unsupported-browser fallback test always runs.

const SESSION_ID = '11111111-2222-4333-8444-555555555555';
const UPLOAD_PATH = 'e2e-upload-1';
const FIXTURE_NAME = 'meeting.webm';
const FIXTURE_SIZE = 2 * 1024 * 1024;
const INTERRUPTED_AT = 512 * 1024;

const cors = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET,POST,HEAD,PATCH,OPTIONS',
  'access-control-allow-headers': '*',
  'access-control-expose-headers': 'Location,Upload-Offset,Upload-Length,Tus-Resumable',
};

type Backend = {
  endpoint: string;
  uploadUrl: string;
  close: () => Promise<void>;
  token: string;
  apiCreates: number;
  tusCreates: number;
  received: number;
  length: number;
  tusDown: boolean;
  interruptAt: number | null;
  patches: Array<{ url: string; offset: number }>;
};

const openServers: Array<() => Promise<void>> = [];
const handleTest = process.env.RECANTOR_E2E_FILE_HANDLES ? test : test.skip;

async function installBackend(page: Page): Promise<Backend> {
  const backend: Backend = {
    endpoint: '',
    uploadUrl: '',
    close: async () => undefined,
    token: '',
    apiCreates: 0,
    tusCreates: 0,
    received: 0,
    length: FIXTURE_SIZE,
    tusDown: false,
    interruptAt: null,
    patches: [],
  };

  const tusHeaders = { ...cors, 'tus-resumable': '1.0.0' };
  const server = http.createServer((req, res) => {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, cors).end();
      return;
    }
    if (backend.tusDown) {
      req.socket.destroy();
      return;
    }
    if (req.method === 'POST') {
      backend.tusCreates += 1;
      req.resume();
      res.writeHead(201, { ...tusHeaders, location: backend.uploadUrl }).end();
      return;
    }
    if (req.method === 'HEAD') {
      res
        .writeHead(200, {
          ...tusHeaders,
          'upload-offset': String(backend.received),
          'upload-length': String(backend.length),
          'cache-control': 'no-store',
        })
        .end();
      return;
    }
    if (req.method === 'PATCH') {
      const offset = Number.parseInt(String(req.headers['upload-offset'] ?? '-1'), 10);
      backend.patches.push({
        url: `${backend.endpoint}${(req.url ?? '').replace(/^\/files\//, '')}`,
        offset,
      });
      if (offset !== backend.received) {
        req.resume();
        res.writeHead(409, tusHeaders).end();
        return;
      }
      let bytes = 0;
      req.on('data', (chunk: Buffer) => {
        bytes += chunk.length;
        if (backend.interruptAt !== null && offset + bytes >= backend.interruptAt) {
          backend.received = backend.interruptAt;
          backend.tusDown = true;
          req.socket.destroy();
        }
      });
      req.on('end', () => {
        if (backend.tusDown) return;
        backend.received = offset + bytes;
        res.writeHead(204, { ...tusHeaders, 'upload-offset': String(backend.received) }).end();
      });
      return;
    }
    res.writeHead(405, tusHeaders).end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  backend.endpoint = `http://127.0.0.1:${port}/files/`;
  backend.uploadUrl = `${backend.endpoint}${UPLOAD_PATH}`;
  backend.close = () =>
    new Promise<void>((resolve) => {
      server.closeAllConnections();
      server.close(() => resolve());
    });
  openServers.push(backend.close);

  const complete = () => backend.received >= backend.length;
  const sessionBody = (received: number) => ({
    session_id: SESSION_ID,
    client_request_id: 'e2e-client',
    kind: 'upload',
    state: complete() ? 'uploaded' : 'uploading',
    original_filename: FIXTURE_NAME,
    content_type: 'video/webm',
    declared_byte_length: backend.length,
    declared_duration_ms: null,
    received_bytes: received,
    expires_at: '2099-01-01T00:00:00Z',
    completed_at: complete() ? '2026-09-29T00:00:00Z' : null,
    failure_code: null,
    failure_message: null,
    upload_endpoint: backend.endpoint,
  });
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({
      status,
      contentType: 'application/json',
      headers: cors,
      body: JSON.stringify(body),
    });

  await page.route('**/api/v1/uploads', async (route) => {
    const method = route.request().method();
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (method !== 'POST') return route.fallback();
    backend.apiCreates += 1;
    const body = route.request().postDataJSON() as {
      capability_token: string;
      byte_length: number;
      content_type: string;
    };
    backend.token = body.capability_token;
    backend.length = body.byte_length;
    return json(route, 201, { ...sessionBody(0), content_type: body.content_type });
  });
  await page.route(`**/api/v1/uploads/${SESSION_ID}`, async (route) => {
    if (route.request().method() === 'OPTIONS') {
      return route.fulfill({ status: 204, headers: cors });
    }
    expect(route.request().headers()['x-recantor-upload-token']).toBe(backend.token);
    return json(route, 200, sessionBody(backend.received));
  });
  await page.route(`**/api/v1/uploads/${SESSION_ID}/result`, async (route) => {
    if (route.request().method() === 'OPTIONS') {
      return route.fulfill({ status: 204, headers: cors });
    }
    return json(route, 200, {
      session_id: SESSION_ID,
      state: 'preparing',
      transcript_segment_count: 0,
      exports_available: false,
      completed_at: null,
      expires_at: '2099-01-01T00:00:00Z',
      failure_message: null,
    });
  });
  return backend;
}

async function installBrowserStubs(page: Page, options: { picker: boolean }): Promise<void> {
  await page.addInitScript(
    ({ picker, name }) => {
      const win = window as unknown as Record<string, unknown>;
      if (picker) {
        win.showOpenFilePicker = async () => {
          const root = await navigator.storage.getDirectory();
          return [await root.getFileHandle(name)];
        };
      } else {
        Object.defineProperty(window, 'showOpenFilePicker', {
          configurable: true,
          value: undefined,
        });
      }
      const calls = { query: 0, request: 0 };
      win.__permissionCalls = calls;
      const mode = window.localStorage.getItem('e2e:permission');
      if (!mode) return;
      const proto = FileSystemFileHandle.prototype as unknown as Record<string, unknown>;
      proto.queryPermission = async () => {
        calls.query += 1;
        return mode;
      };
      proto.requestPermission = async () => {
        calls.request += 1;
        return window.localStorage.getItem('e2e:grant') ?? 'granted';
      };
    },
    { picker: options.picker, name: FIXTURE_NAME },
  );
}

async function writeOpfsFixture(page: Page, seed: number): Promise<void> {
  await page.evaluate(
    async ({ seed: fill, size, name }) => {
      const root = await navigator.storage.getDirectory();
      const handle = await root.getFileHandle(name, { create: true });
      const writable = await handle.createWritable();
      const bytes = new Uint8Array(size);
      for (let index = 0; index < size; index += 1) bytes[index] = (index * 31 + fill) & 0xff;
      await writable.write(bytes);
      await writable.close();
    },
    { seed, size: FIXTURE_SIZE, name: FIXTURE_NAME },
  );
}

async function storedHandleCount(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      new Promise<number>((resolve, reject) => {
        const open = indexedDB.open('recantor-upload-handles');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const db = open.result;
          if (!db.objectStoreNames.contains('handles')) {
            db.close();
            resolve(0);
            return;
          }
          const count = db.transaction('handles', 'readonly').objectStore('handles').count();
          count.onsuccess = () => {
            db.close();
            resolve(count.result);
          };
          count.onerror = () => reject(count.error);
        };
      }),
  );
}

async function permissionCalls(page: Page): Promise<{ query: number; request: number }> {
  return page.evaluate(
    () =>
      (window as unknown as { __permissionCalls: { query: number; request: number } })
        .__permissionCalls,
  );
}

async function openUpload(page: Page): Promise<void> {
  await page.getByTestId('workflow-upload').click();
  await expect(page.getByTestId('upload-dropzone')).toBeVisible();
}

// Drive the first page session until the upload is interrupted at a non-zero tus offset.
async function interruptFirstSession(
  page: Page,
  backend: Backend,
  select: () => Promise<void>,
): Promise<void> {
  backend.interruptAt = INTERRUPTED_AT;
  await select();
  await page.getByTestId('upload-start').click();
  await expect.poll(() => backend.tusDown, { timeout: 15_000 }).toBe(true);
  expect(backend.apiCreates).toBe(1);
  expect(backend.tusCreates).toBe(1);
  expect(backend.received).toBe(INTERRUPTED_AT);
  backend.interruptAt = null;
}

async function reopen(page: Page, backend: Backend): Promise<void> {
  // A fresh page load discards every in-memory File/Uppy object, like closing and reopening.
  backend.tusDown = false;
  await page.reload();
  await openUpload(page);
}

function patchesFromReopen(backend: Backend) {
  return backend.patches.filter((patch) => patch.offset > 0);
}

function expectContinuedSameUpload(backend: Backend): void {
  expect(backend.apiCreates).toBe(1);
  expect(backend.tusCreates).toBe(1);
  const resumed = patchesFromReopen(backend);
  expect(resumed).toHaveLength(1);
  expect(resumed[0]).toEqual({ url: backend.uploadUrl, offset: INTERRUPTED_AT });
}

test.describe('#61 automatic upload recovery after browser reopen', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => window.localStorage.clear());
  });

  test.afterEach(async () => {
    await Promise.all(openServers.splice(0).map((close) => close()));
  });

  async function selectViaPicker(page: Page): Promise<void> {
    await page.getByTestId('upload-choose').click();
    await expect(page.getByTestId('upload-selected-file')).toContainText(FIXTURE_NAME);
  }

  handleTest(
    'resumes the same tus upload automatically when the persisted handle is still permitted',
    async ({ page }) => {
      const backend = await installBackend(page);
      await installBrowserStubs(page, { picker: true });
      await page.reload();
      await writeOpfsFixture(page, 1);
      await openUpload(page);

      await interruptFirstSession(page, backend, () => selectViaPicker(page));
      await expect.poll(() => storedHandleCount(page)).toBe(1);

      await reopen(page, backend);

      // No Choose / Start click: recovery is automatic and continues from the prior offset.
      await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
        timeout: 30_000,
      });
      expectContinuedSameUpload(backend);
      expect((await permissionCalls(page)).request).toBe(0);
      await expect(page.locator('body')).not.toContainText('/files/');
      await expect.poll(() => storedHandleCount(page)).toBe(0);
    },
  );

  handleTest(
    'asks for one gesture when permission needs a prompt, then resumes the same upload',
    async ({ page }) => {
      const backend = await installBackend(page);
      await installBrowserStubs(page, { picker: true });
      await page.reload();
      await writeOpfsFixture(page, 2);
      await openUpload(page);
      await interruptFirstSession(page, backend, () => selectViaPicker(page));

      await page.evaluate(() => window.localStorage.setItem('e2e:permission', 'prompt'));
      await reopen(page, backend);

      await expect(page.getByTestId('upload-phase')).toHaveText('Permission needed');
      await expect(page.getByTestId('upload-allow-access')).toHaveText('Allow access and resume');
      expect((await permissionCalls(page)).request).toBe(0);
      expect(patchesFromReopen(backend)).toHaveLength(0);

      await page.getByTestId('upload-allow-access').click();

      await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
        timeout: 30_000,
      });
      expect((await permissionCalls(page)).request).toBe(1);
      expectContinuedSameUpload(backend);
    },
  );

  handleTest(
    'shows revoked permission and resumes the same upload after the user reselects the file',
    async ({ page }) => {
      const backend = await installBackend(page);
      await installBrowserStubs(page, { picker: true });
      await page.reload();
      await writeOpfsFixture(page, 3);
      await openUpload(page);
      await interruptFirstSession(page, backend, () => selectViaPicker(page));

      await page.evaluate(() => window.localStorage.setItem('e2e:permission', 'denied'));
      await reopen(page, backend);

      await expect(page.getByTestId('upload-message')).toContainText(
        'Access to the original file was not granted',
      );
      await expect(page.getByTestId('upload-choose')).toHaveText('Choose the same file to resume');
      expect(patchesFromReopen(backend)).toHaveLength(0);
      expect(backend.apiCreates).toBe(1);

      await selectViaPicker(page);
      await page.getByTestId('upload-start').click();

      await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
        timeout: 30_000,
      });
      expectContinuedSameUpload(backend);
    },
  );

  handleTest(
    'refuses to resume when the file changed and never creates a second upload',
    async ({ page }) => {
      const backend = await installBackend(page);
      await installBrowserStubs(page, { picker: true });
      await page.reload();
      await writeOpfsFixture(page, 4);
      await openUpload(page);
      await interruptFirstSession(page, backend, () => selectViaPicker(page));

      await writeOpfsFixture(page, 5);
      await reopen(page, backend);

      await expect(page.getByTestId('upload-phase')).toHaveText('File changed');
      await expect(page.getByTestId('upload-message')).toContainText('has changed');
      await page.waitForTimeout(1_500);
      expect(patchesFromReopen(backend)).toHaveLength(0);
      expect(backend.apiCreates).toBe(1);
      expect(backend.tusCreates).toBe(1);
      expect(backend.received).toBe(INTERRUPTED_AT);
    },
  );

  test('falls back to a truthful reselect flow where persistent handles are unsupported', async ({
    page,
  }) => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'recantor-61-'));
    const fixture = path.join(dir, FIXTURE_NAME);
    writeFileSync(fixture, Buffer.alloc(FIXTURE_SIZE, 7));

    const backend = await installBackend(page);
    await installBrowserStubs(page, { picker: false });
    await page.reload();
    await openUpload(page);
    await interruptFirstSession(page, backend, async () => {
      await page.getByTestId('upload-file-input').setInputFiles(fixture);
      await expect(page.getByTestId('upload-selected-file')).toContainText(FIXTURE_NAME);
    });
    expect(await storedHandleCount(page)).toBe(0);

    await reopen(page, backend);

    await expect(page.getByTestId('upload-message')).toContainText(
      'Choose the same file to resume it.',
    );
    await expect(page.getByTestId('upload-choose')).toHaveText('Choose the same file to resume');
    expect(patchesFromReopen(backend)).toHaveLength(0);

    await page.getByTestId('upload-file-input').setInputFiles(fixture);
    await page.getByTestId('upload-start').click();

    await expect(page.getByTestId('upload-message')).toContainText('Durably uploaded', {
      timeout: 30_000,
    });
    expectContinuedSameUpload(backend);
  });
});
