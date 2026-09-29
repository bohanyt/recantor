import Dexie, { type EntityTable } from 'dexie';

import { clearUploadRecovery, type UploadRecovery } from './recovery';

// Only the browser's file *reference* is stored, never the media bytes. It is convenience state:
// the server-side session and tus offset stay authoritative, and every failure here degrades to the
// same-file reselect flow.

type StoredHandle = {
  clientRequestId: string;
  handle: FileSystemFileHandle;
  savedAt: number;
};

export type UploadHandleStore = {
  put(clientRequestId: string, handle: FileSystemFileHandle): Promise<boolean>;
  get(clientRequestId: string): Promise<FileSystemFileHandle | null>;
  delete(clientRequestId: string): Promise<void>;
};

class UploadHandleDatabase extends Dexie {
  handles!: EntityTable<StoredHandle, 'clientRequestId'>;

  constructor() {
    super('recantor-upload-handles');
    this.version(1).stores({ handles: '&clientRequestId' });
  }
}

function createIndexedDbStore(): UploadHandleStore {
  const db = new UploadHandleDatabase();
  return {
    async put(clientRequestId, handle) {
      try {
        await db.handles.put({ clientRequestId, handle, savedAt: Date.now() });
        return true;
      } catch {
        return false;
      }
    },
    async get(clientRequestId) {
      try {
        const row = await db.handles.get(clientRequestId);
        return row?.handle?.kind === 'file' ? row.handle : null;
      } catch {
        return null;
      }
    },
    async delete(clientRequestId) {
      try {
        await db.handles.delete(clientRequestId);
      } catch {
        // Best effort: a stale handle is harmless without its recovery record.
      }
    },
  };
}

let store: UploadHandleStore | null = null;

export function getUploadHandleStore(): UploadHandleStore {
  store ??= createIndexedDbStore();
  return store;
}

export function setUploadHandleStoreForTests(next: UploadHandleStore | null): void {
  store = next;
}

export function discardUploadRecovery(recovery: UploadRecovery): void {
  clearUploadRecovery(recovery);
  void getUploadHandleStore().delete(recovery.clientRequestId);
}
