import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  pickRecordingWithHandle,
  reacquireFile,
  supportsPersistentFileHandles,
} from './fileHandle';

const file = new File(['audio'], 'meeting.webm', { type: 'audio/webm' });

function domError(name: string): Error {
  return Object.assign(new Error(name), { name });
}

function fakeHandle(options: {
  query?: PermissionState | Error;
  request?: PermissionState | Error;
  file?: File | Error;
}) {
  const settle = <T>(value: T | Error) =>
    value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  const queryPermission = vi.fn(() => settle(options.query ?? 'granted'));
  const requestPermission = vi.fn(() => settle(options.request ?? 'granted'));
  const getFile = vi.fn(() => settle(options.file ?? file));
  const handle = { kind: 'file', name: file.name, queryPermission, requestPermission, getFile };
  return {
    handle: handle as unknown as FileSystemFileHandle,
    queryPermission,
    requestPermission,
    getFile,
  };
}

describe('reacquireFile', () => {
  it('returns a fresh File without prompting when permission is already granted', async () => {
    const fake = fakeHandle({ query: 'granted' });

    expect(await reacquireFile(fake.handle, { requestPermission: false })).toEqual({
      status: 'ready',
      file,
    });
    expect(fake.requestPermission).not.toHaveBeenCalled();
  });

  it('never requests permission unless the caller is inside a user gesture', async () => {
    const fake = fakeHandle({ query: 'prompt' });

    expect(await reacquireFile(fake.handle, { requestPermission: false })).toEqual({
      status: 'needs_permission',
    });
    expect(fake.requestPermission).not.toHaveBeenCalled();
    expect(fake.getFile).not.toHaveBeenCalled();
  });

  it('requests permission on a gesture and reads the file when it is granted', async () => {
    const fake = fakeHandle({ query: 'prompt', request: 'granted' });

    expect(await reacquireFile(fake.handle, { requestPermission: true })).toEqual({
      status: 'ready',
      file,
    });
    expect(fake.requestPermission).toHaveBeenCalledTimes(1);
  });

  it('reports a refused or dismissed permission request truthfully', async () => {
    expect(
      await reacquireFile(fakeHandle({ query: 'prompt', request: 'denied' }).handle, {
        requestPermission: true,
      }),
    ).toEqual({ status: 'denied' });
    expect(
      await reacquireFile(fakeHandle({ query: 'prompt', request: 'prompt' }).handle, {
        requestPermission: true,
      }),
    ).toEqual({ status: 'needs_permission' });
  });

  it('treats revoked permission as denied and never reads the file', async () => {
    const fake = fakeHandle({ query: 'denied' });

    expect(await reacquireFile(fake.handle, { requestPermission: true })).toEqual({
      status: 'denied',
    });
    expect(fake.getFile).not.toHaveBeenCalled();
  });

  it('maps file system failures to visible states', async () => {
    expect(
      await reacquireFile(fakeHandle({ file: domError('NotFoundError') }).handle, {
        requestPermission: false,
      }),
    ).toEqual({ status: 'missing' });
    expect(
      await reacquireFile(fakeHandle({ file: domError('NotAllowedError') }).handle, {
        requestPermission: false,
      }),
    ).toEqual({ status: 'denied' });
    expect(
      await reacquireFile(fakeHandle({ file: domError('NotReadableError') }).handle, {
        requestPermission: false,
      }),
    ).toEqual({ status: 'error', message: 'NotReadableError' });
  });

  it('reports an unsupported handle instead of guessing', async () => {
    const bare = { kind: 'file', getFile: vi.fn() } as unknown as FileSystemFileHandle;

    expect(await reacquireFile(bare, { requestPermission: true })).toEqual({
      status: 'unsupported',
    });
  });
});

describe('pickRecordingWithHandle', () => {
  afterEach(() => {
    Reflect.deleteProperty(window, 'showOpenFilePicker');
  });

  function stubPicker(implementation: () => Promise<FileSystemFileHandle[]>) {
    Object.defineProperty(window, 'showOpenFilePicker', {
      configurable: true,
      writable: true,
      value: vi.fn(implementation),
    });
  }

  it('reports unsupported when the browser has no persistent-handle picker', async () => {
    expect(supportsPersistentFileHandles()).toBe(false);
    expect(await pickRecordingWithHandle()).toEqual({ kind: 'unsupported' });
  });

  it('returns the picked handle together with a fresh File', async () => {
    const fake = fakeHandle({});
    stubPicker(async () => [fake.handle]);

    expect(supportsPersistentFileHandles()).toBe(true);
    expect(await pickRecordingWithHandle()).toEqual({
      kind: 'picked',
      handle: fake.handle,
      file,
    });
  });

  it('treats a dismissed picker as a cancel and other failures as visible errors', async () => {
    stubPicker(() => Promise.reject(domError('AbortError')));
    expect(await pickRecordingWithHandle()).toEqual({ kind: 'cancelled' });

    stubPicker(() => Promise.reject(domError('SecurityError')));
    expect(await pickRecordingWithHandle()).toEqual({ kind: 'error', message: 'SecurityError' });
  });
});
