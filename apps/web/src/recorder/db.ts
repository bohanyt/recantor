import Dexie, { type EntityTable } from 'dexie';

export type LocalSessionState = 'recording' | 'interrupted' | 'finalizing';

export type LocalRecordingSession = {
  sessionId: string;
  clientRequestId: string;
  writerId: string;
  captureEpoch: number;
  recoveryToken?: string | null;
  pendingWriterId?: string | null;
  state: LocalSessionState;
  mimeType: string;
  lastSequence: number;
  lastMonotonicEndMs: number;
  lastWallEndMs: number | null;
  recordingStartedWallMs: number;
  createdAt: number;
  updatedAt: number;
};

export type PendingStart = {
  key: 'pending-start';
  clientRequestId: string;
  writerId: string;
  recoveryToken: string;
  createdAt: number;
};

export type SpoolChunk = {
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
  blob: Blob;
  createdAt: number;
};

class RecorderDatabase extends Dexie {
  sessions!: EntityTable<LocalRecordingSession, 'sessionId'>;
  chunks!: EntityTable<SpoolChunk, 'key'>;
  pendingStarts!: EntityTable<PendingStart, 'key'>;

  constructor() {
    super('recantor-recorder');
    this.version(1).stores({
      sessions: '&sessionId, updatedAt',
      chunks: '&key, sessionId, sequence, [sessionId+sequence], createdAt',
    });
    this.version(2).stores({
      sessions: '&sessionId, updatedAt',
      chunks: '&key, sessionId, sequence, [sessionId+sequence], createdAt',
      pendingStarts: '&key',
    });
  }
}

export const recorderDb = new RecorderDatabase();

export function chunkKey(sessionId: string, sequence: number): string {
  return `${sessionId}:${sequence}`;
}

export async function putLocalSession(session: LocalRecordingSession): Promise<void> {
  await recorderDb.sessions.put(session);
}

export async function getLocalSession(
  sessionId: string,
): Promise<LocalRecordingSession | undefined> {
  return recorderDb.sessions.get(sessionId);
}

export async function getLatestLocalSession(): Promise<LocalRecordingSession | undefined> {
  const sessions = await recorderDb.sessions.orderBy('updatedAt').reverse().toArray();
  return sessions[0];
}

export async function deleteLocalSession(sessionId: string): Promise<void> {
  await recorderDb.transaction('rw', recorderDb.sessions, recorderDb.chunks, async () => {
    await recorderDb.chunks.where('sessionId').equals(sessionId).delete();
    await recorderDb.sessions.delete(sessionId);
  });
}

export async function getOrCreatePendingStart(): Promise<PendingStart> {
  return recorderDb.transaction('rw', recorderDb.pendingStarts, async () => {
    const existing = await recorderDb.pendingStarts.get('pending-start');
    if (existing) return existing;
    const pending: PendingStart = {
      key: 'pending-start',
      clientRequestId: crypto.randomUUID(),
      writerId: crypto.randomUUID(),
      recoveryToken: crypto.randomUUID(),
      createdAt: Date.now(),
    };
    await recorderDb.pendingStarts.add(pending);
    return pending;
  });
}

export async function commitStartedSession(
  pending: PendingStart,
  session: LocalRecordingSession,
): Promise<void> {
  await recorderDb.transaction('rw', recorderDb.pendingStarts, recorderDb.sessions, async () => {
    const storedPending = await recorderDb.pendingStarts.get('pending-start');
    if (!storedPending || storedPending.clientRequestId !== pending.clientRequestId) {
      throw new Error('Pending recording start identity changed before session commit.');
    }
    if (
      session.clientRequestId !== pending.clientRequestId ||
      session.writerId !== pending.writerId ||
      session.recoveryToken !== pending.recoveryToken
    ) {
      throw new Error('Server session does not match the persisted recording start identity.');
    }
    await recorderDb.sessions.put(session);
    await recorderDb.pendingStarts.delete('pending-start');
  });
}

export async function putSpoolChunk(chunk: SpoolChunk): Promise<void> {
  await recorderDb.chunks.put(chunk);
}

export async function appendCapturedChunk(
  chunk: SpoolChunk,
  nextSession: LocalRecordingSession,
): Promise<void> {
  await recorderDb.transaction('rw', recorderDb.sessions, recorderDb.chunks, async () => {
    const storedSession = await recorderDb.sessions.get(chunk.sessionId);
    if (!storedSession) throw new Error('Local recording session is missing.');
    if (
      storedSession.writerId !== chunk.writerId ||
      storedSession.captureEpoch !== chunk.captureEpoch
    ) {
      throw new Error(
        'Local capture ownership changed before the audio fragment could be committed.',
      );
    }

    const expectedSequence = storedSession.lastSequence + 1;
    if (chunk.sequence !== expectedSequence) {
      throw new Error(
        `Refusing stale local sequence ${chunk.sequence}; expected ${expectedSequence}.`,
      );
    }
    if (
      nextSession.sessionId !== storedSession.sessionId ||
      nextSession.writerId !== storedSession.writerId ||
      nextSession.captureEpoch !== storedSession.captureEpoch ||
      nextSession.lastSequence !== chunk.sequence ||
      nextSession.lastMonotonicEndMs !== chunk.monotonicEndMs ||
      nextSession.lastWallEndMs !== chunk.wallEndMs
    ) {
      throw new Error('Local recording high-water metadata does not match the captured fragment.');
    }

    const existing = await recorderDb.chunks.get(chunk.key);
    if (existing) {
      throw new Error(`Refusing to overwrite unacknowledged local sequence ${chunk.sequence}.`);
    }

    await recorderDb.chunks.add(chunk);
    await recorderDb.sessions.put(nextSession);
  });
}

export async function listSpoolChunks(sessionId: string): Promise<SpoolChunk[]> {
  const chunks = await recorderDb.chunks.where('sessionId').equals(sessionId).toArray();
  return chunks.sort((left, right) => left.sequence - right.sequence);
}

export async function deleteSpoolChunk(sessionId: string, sequence: number): Promise<void> {
  await recorderDb.chunks.delete(chunkKey(sessionId, sequence));
}

export async function countSpoolChunks(sessionId: string): Promise<number> {
  return recorderDb.chunks.where('sessionId').equals(sessionId).count();
}
