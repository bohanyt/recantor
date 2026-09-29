import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  discardUploadRecovery,
  getUploadHandleStore,
  setUploadHandleStoreForTests,
  type UploadHandleStore,
} from './handleStore';
import { createUploadRecovery, loadUploadRecovery, saveUploadRecovery } from './recovery';

// fake-indexeddb can only structured-clone plain data, so a data-only stand-in represents the
// browser handle here. Real FileSystemFileHandle persistence is proven in the Playwright spec.
const standIn = { kind: 'file', name: 'meeting.webm' } as unknown as FileSystemFileHandle;

describe('IndexedDB upload handle store', () => {
  beforeEach(() => setUploadHandleStoreForTests(null));
  afterEach(() => setUploadHandleStoreForTests(null));

  it('round-trips a handle by upload identity and forgets it on delete', async () => {
    const store = getUploadHandleStore();

    expect(await store.get('missing')).toBeNull();
    expect(await store.put('client-1', standIn)).toBe(true);
    expect(await store.get('client-1')).toEqual(standIn);
    expect(await store.get('client-2')).toBeNull();

    await store.delete('client-1');
    expect(await store.get('client-1')).toBeNull();
  });

  it('degrades to "no handle" instead of throwing when the value cannot be stored', async () => {
    const store = getUploadHandleStore();
    const uncloneable = {
      kind: 'file',
      getFile: () => undefined,
    } as unknown as FileSystemFileHandle;

    expect(await store.put('client-3', uncloneable)).toBe(false);
    expect(await store.get('client-3')).toBeNull();
  });

  it('ignores stored values that are not file handles', async () => {
    const store = getUploadHandleStore();
    await store.put('client-4', { kind: 'directory' } as unknown as FileSystemFileHandle);

    expect(await store.get('client-4')).toBeNull();
  });
});

describe('discardUploadRecovery', () => {
  afterEach(() => setUploadHandleStoreForTests(null));

  it('removes the saved recovery and its persisted handle together', () => {
    window.localStorage.clear();
    const remove = vi.fn(async () => undefined);
    setUploadHandleStoreForTests({ delete: remove } as unknown as UploadHandleStore);
    const selected = new File(['x'], 'a.wav', { type: 'audio/wav', lastModified: 1 });
    const recovery = createUploadRecovery(selected);
    saveUploadRecovery({ ...recovery, sessionId: crypto.randomUUID() });

    discardUploadRecovery(recovery);

    expect(loadUploadRecovery(selected)).toBeNull();
    expect(remove).toHaveBeenCalledWith(recovery.clientRequestId);
  });
});
