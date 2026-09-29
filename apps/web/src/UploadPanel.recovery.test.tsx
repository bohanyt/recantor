import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UploadPanel } from './UploadPanel';
import {
  createUploadSession,
  getUploadResultStatus,
  getUploadSession,
  UploadApiError,
  type UploadSession,
} from './upload/api';
import { setUploadHandleStoreForTests, type UploadHandleStore } from './upload/handleStore';
import {
  computeContentEvidence,
  createUploadRecovery,
  loadUploadRecovery,
  saveUploadRecovery,
  type UploadRecovery,
} from './upload/recovery';

type FakeUppy = {
  tusOptions: { endpoint: string; headers: Record<string, string> };
  files: Array<{ name: string; data: File; meta: Record<string, string> }>;
  uploadCalled: boolean;
};

const uppy = vi.hoisted(() => ({ instances: [] as unknown[] }));

vi.mock('./vendor/uppy.min.mjs', () => {
  class Uppy {
    tusOptions: unknown = null;
    files: unknown[] = [];
    uploadCalled = false;
    constructor() {
      uppy.instances.push(this);
    }
    use(_plugin: unknown, options: unknown) {
      this.tusOptions = options;
      return this;
    }
    on() {
      return this;
    }
    addFile(file: unknown) {
      this.files.push(file);
      return 'file-1';
    }
    async upload() {
      this.uploadCalled = true;
    }
    pauseAll() {}
    resumeAll() {}
    async retryAll() {}
    destroy() {}
  }
  return { Uppy, Tus: {} };
});

vi.mock('./upload/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./upload/api')>();
  return {
    ...actual,
    createUploadSession: vi.fn(),
    getUploadSession: vi.fn(),
    getUploadResultStatus: vi.fn(),
  };
});

const SESSION_ID = '11111111-2222-4333-8444-555555555555';
const TUS_ENDPOINT = 'http://tus.test/files/';
const MODIFIED = 1_700_000_000_000;

function recording(fill = 7, lastModified = MODIFIED, length = 4096): File {
  return new File([new Uint8Array(length).fill(fill)], 'meeting.webm', {
    type: 'audio/webm',
    lastModified,
  });
}

function session(overrides: Partial<UploadSession> = {}): UploadSession {
  return {
    session_id: SESSION_ID,
    client_request_id: 'client',
    kind: 'upload',
    state: 'uploading',
    original_filename: 'meeting.webm',
    content_type: 'audio/webm',
    declared_byte_length: 4096,
    declared_duration_ms: null,
    received_bytes: 1024,
    expires_at: '2099-01-01T00:00:00Z',
    completed_at: null,
    failure_code: null,
    failure_message: null,
    upload_endpoint: TUS_ENDPOINT,
    ...overrides,
  };
}

function fakeHandle(options: {
  query?: PermissionState;
  request?: PermissionState;
  file?: File | Error;
}) {
  const queryPermission = vi.fn(async () => options.query ?? 'granted');
  const requestPermission = vi.fn(async () => options.request ?? 'granted');
  const getFile = vi.fn(async () => {
    if (options.file instanceof Error) throw options.file;
    return options.file ?? recording();
  });
  return {
    handle: {
      kind: 'file',
      name: 'meeting.webm',
      queryPermission,
      requestPermission,
      getFile,
    } as unknown as FileSystemFileHandle,
    queryPermission,
    requestPermission,
    getFile,
  };
}

function memoryStore() {
  const rows = new Map<string, FileSystemFileHandle>();
  const store: UploadHandleStore = {
    put: vi.fn(async (id, handle) => {
      rows.set(id, handle);
      return true;
    }),
    get: vi.fn(async (id) => rows.get(id) ?? null),
    delete: vi.fn(async (id) => {
      rows.delete(id);
    }),
  };
  return { store, rows };
}

async function seedSavedUpload(original: File): Promise<UploadRecovery> {
  const created = createUploadRecovery(original, await computeContentEvidence(original));
  const recovery = { ...created, sessionId: SESSION_ID, expiresAt: '2099-01-01T00:00:00Z' };
  saveUploadRecovery(recovery);
  return recovery;
}

function phase(): string {
  return screen.getByTestId('upload-phase').textContent ?? '';
}

function message(): string {
  return screen.getByTestId('upload-message').textContent ?? '';
}

function instances(): FakeUppy[] {
  return uppy.instances as FakeUppy[];
}

describe('UploadPanel automatic recovery after browser reopen', () => {
  let rows: Map<string, FileSystemFileHandle>;
  let store: UploadHandleStore;

  beforeEach(() => {
    window.localStorage.clear();
    uppy.instances.length = 0;
    vi.mocked(createUploadSession).mockReset();
    vi.mocked(getUploadResultStatus).mockReset();
    vi.mocked(getUploadSession).mockReset().mockResolvedValue(session());
    ({ store, rows } = memoryStore());
    setUploadHandleStoreForTests(store);
  });

  afterEach(() => {
    cleanup();
    setUploadHandleStoreForTests(null);
    Reflect.deleteProperty(window, 'showOpenFilePicker');
  });

  it('resumes the same upload automatically when permission is already granted', async () => {
    const recovery = await seedSavedUpload(recording());
    const fake = fakeHandle({ query: 'granted', file: recording() });
    rows.set(recovery.clientRequestId, fake.handle);

    render(<UploadPanel />);

    await waitFor(() => expect(instances()).toHaveLength(1));
    const transfer = instances()[0]!;
    expect(transfer.uploadCalled).toBe(true);
    expect(transfer.tusOptions.endpoint).toBe(TUS_ENDPOINT);
    expect(transfer.tusOptions.headers['X-Recantor-Upload-Token']).toBe(recovery.capabilityToken);
    expect(transfer.files[0]!.meta.recantor_session_id).toBe(SESSION_ID);
    expect(transfer.files[0]!.data.name).toBe('meeting.webm');
    expect(phase()).toBe('Uploading');
    expect(message()).toBe('Resuming from durable server progress…');
    expect(fake.requestPermission).not.toHaveBeenCalled();
    expect(createUploadSession).not.toHaveBeenCalled();
    expect(loadUploadRecovery(recording())?.clientRequestId).toBe(recovery.clientRequestId);
  });

  it('shows one gesture-gated CTA when permission needs a prompt, then resumes', async () => {
    const recovery = await seedSavedUpload(recording());
    const fake = fakeHandle({ query: 'prompt', request: 'granted' });
    rows.set(recovery.clientRequestId, fake.handle);

    render(<UploadPanel />);

    const cta = await screen.findByTestId('upload-allow-access');
    expect(cta.textContent).toBe('Allow access and resume');
    expect(phase()).toBe('Permission needed');
    expect(fake.requestPermission).not.toHaveBeenCalled();
    expect(instances()).toHaveLength(0);

    fireEvent.click(cta);

    await waitFor(() => expect(instances()).toHaveLength(1));
    expect(fake.requestPermission).toHaveBeenCalledTimes(1);
    expect(createUploadSession).not.toHaveBeenCalled();
    expect(screen.queryByTestId('upload-allow-access')).toBeNull();
  });

  it('falls back to a truthful same-file reselect when the user refuses access', async () => {
    const recovery = await seedSavedUpload(recording());
    rows.set(recovery.clientRequestId, fakeHandle({ query: 'prompt', request: 'denied' }).handle);

    render(<UploadPanel />);
    fireEvent.click(await screen.findByTestId('upload-allow-access'));

    await waitFor(() => expect(message()).toContain('Access to the original file was not granted'));
    expect(message()).toContain('Choose the same file to resume it.');
    expect(screen.getByTestId('upload-choose').textContent).toBe('Choose the same file to resume');
    expect(screen.queryByTestId('upload-allow-access')).toBeNull();
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
  });

  it('makes revoked permission visible instead of failing silently', async () => {
    const recovery = await seedSavedUpload(recording());
    rows.set(recovery.clientRequestId, fakeHandle({ query: 'denied' }).handle);

    render(<UploadPanel />);

    await waitFor(() => expect(message()).toContain('Access to the original file was not granted'));
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
  });

  it('reports a moved or deleted original file', async () => {
    const recovery = await seedSavedUpload(recording());
    const missing = Object.assign(new Error('gone'), { name: 'NotFoundError' });
    rows.set(recovery.clientRequestId, fakeHandle({ file: missing }).handle);

    render(<UploadPanel />);

    await waitFor(() => expect(message()).toContain('could not be found'));
    expect(instances()).toHaveLength(0);
  });

  it('never resumes against a file whose metadata changed', async () => {
    const recovery = await seedSavedUpload(recording());
    rows.set(recovery.clientRequestId, fakeHandle({ file: recording(7, MODIFIED + 1) }).handle);

    render(<UploadPanel />);

    await waitFor(() => expect(phase()).toBe('File changed'));
    expect(message()).toContain('has changed');
    expect(message()).toContain('saved server progress is kept');
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
    expect(loadUploadRecovery(recording())?.sessionId).toBe(SESSION_ID);
  });

  it('never resumes against a file with identical metadata but different content', async () => {
    const recovery = await seedSavedUpload(recording(7));
    rows.set(recovery.clientRequestId, fakeHandle({ file: recording(9) }).handle);

    render(<UploadPanel />);

    await waitFor(() => expect(phase()).toBe('File changed'));
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
  });

  it("keeps today's reselect flow when no handle was persisted", async () => {
    await seedSavedUpload(recording());

    render(<UploadPanel />);

    await waitFor(() => expect(message()).toContain('Choose the same file to resume it.'));
    expect(phase()).toBe('Ready');
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
  });

  it('does not create a replacement upload when the server no longer knows the session', async () => {
    const recovery = await seedSavedUpload(recording());
    rows.set(recovery.clientRequestId, fakeHandle({}).handle);
    vi.mocked(getUploadSession).mockRejectedValue(new UploadApiError('not found', 404));

    render(<UploadPanel />);

    await waitFor(() => expect(message()).toContain('no longer valid'));
    expect(createUploadSession).not.toHaveBeenCalled();
    expect(instances()).toHaveLength(0);
    expect(loadUploadRecovery(recording())).toBeNull();
    expect(store.delete).toHaveBeenCalledWith(recovery.clientRequestId);
  });

  it('refuses to append a same-metadata different file chosen manually, until the user asks', async () => {
    const recovery = await seedSavedUpload(recording(7));
    vi.mocked(createUploadSession).mockResolvedValue(session({ session_id: 'fresh-session' }));

    render(<UploadPanel />);
    await waitFor(() => expect(message()).toContain('Choose the same file to resume it.'));
    fireEvent.change(screen.getByTestId('upload-file-input'), {
      target: { files: [recording(9)] },
    });
    fireEvent.click(screen.getByTestId('upload-start'));

    await waitFor(() => expect(phase()).toBe('File changed'));
    expect(message()).toContain('different content');
    expect(createUploadSession).not.toHaveBeenCalled();
    expect(instances()).toHaveLength(0);
    expect(screen.getByTestId('upload-start').textContent).toBe('Start fresh upload');

    fireEvent.click(screen.getByTestId('upload-start'));

    await waitFor(() => expect(createUploadSession).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createUploadSession).mock.calls[0]![0].client_request_id).not.toBe(
      recovery.clientRequestId,
    );
  });

  it('persists the picked file handle against the upload identity for later recovery', async () => {
    const original = recording();
    const fake = fakeHandle({ file: original });
    Object.defineProperty(window, 'showOpenFilePicker', {
      configurable: true,
      writable: true,
      value: vi.fn(async () => [fake.handle]),
    });
    vi.mocked(createUploadSession).mockResolvedValue(session({ received_bytes: 0 }));

    render(<UploadPanel />);
    fireEvent.click(screen.getByTestId('upload-choose'));
    await waitFor(() => expect(screen.getByTestId('upload-selected-file')).toBeTruthy());
    fireEvent.click(screen.getByTestId('upload-start'));

    await waitFor(() => expect(instances()).toHaveLength(1));
    const saved = loadUploadRecovery(original)!;
    expect(saved.sessionId).toBe(SESSION_ID);
    expect(saved.contentEvidence).toEqual(await computeContentEvidence(original));
    expect(store.put).toHaveBeenCalledWith(saved.clientRequestId, fake.handle);
    expect(rows.get(saved.clientRequestId)).toBe(fake.handle);
    expect(createUploadSession).toHaveBeenCalledTimes(1);
  });

  it('releases the persisted handle once the upload is durably complete', async () => {
    const recovery = await seedSavedUpload(recording());
    rows.set(recovery.clientRequestId, fakeHandle({}).handle);
    vi.mocked(getUploadSession).mockResolvedValue(
      session({ state: 'uploaded', completed_at: '2026-09-29T00:00:00Z' }),
    );
    vi.mocked(getUploadResultStatus).mockResolvedValue({
      session_id: SESSION_ID,
      state: 'preparing',
      transcript_segment_count: 0,
      exports_available: false,
      completed_at: null,
      expires_at: '2099-01-01T00:00:00Z',
      failure_message: null,
    });

    render(<UploadPanel />);

    await waitFor(() => expect(store.delete).toHaveBeenCalledWith(recovery.clientRequestId));
    expect(instances()).toHaveLength(0);
    expect(createUploadSession).not.toHaveBeenCalled();
  });
});
