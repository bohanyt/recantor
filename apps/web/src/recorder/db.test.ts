import { beforeEach, describe, expect, it } from 'vitest';

import {
  chunkKey,
  deleteSpoolChunk,
  getLatestLocalSession,
  listSpoolChunks,
  putLocalSession,
  putSpoolChunk,
  recorderDb,
  type LocalRecordingSession,
} from './db';

const session: LocalRecordingSession = {
  sessionId: 'session-a',
  clientRequestId: 'request-a',
  writerId: 'writer-a',
  captureEpoch: 1,
  state: 'recording',
  mimeType: 'audio/webm',
  lastSequence: 1,
  lastMonotonicEndMs: 2000,
  lastWallEndMs: 2_000,
  recordingStartedWallMs: 0,
  createdAt: 1,
  updatedAt: 2,
};

describe('recorder recovery database', () => {
  beforeEach(async () => {
    await recorderDb.open();
    await recorderDb.transaction('rw', recorderDb.sessions, recorderDb.chunks, async () => {
      await recorderDb.chunks.clear();
      await recorderDb.sessions.clear();
    });
  });

  it('keeps ordered audio fragments until explicit deletion', async () => {
    await putLocalSession(session);
    for (const sequence of [2, 1]) {
      const blob = new Blob([`chunk-${sequence}`], { type: 'audio/webm' });
      await putSpoolChunk({
        key: chunkKey(session.sessionId, sequence),
        sessionId: session.sessionId,
        writerId: session.writerId,
        captureEpoch: 1,
        sequence,
        monotonicStartMs: (sequence - 1) * 2000,
        monotonicEndMs: sequence * 2000,
        wallEndMs: sequence * 2000,
        contentType: blob.type,
        sha256: `hash-${sequence}`,
        byteLength: blob.size,
        blob,
        createdAt: sequence,
      });
    }

    expect((await listSpoolChunks(session.sessionId)).map((chunk) => chunk.sequence)).toEqual([1, 2]);
    expect((await getLatestLocalSession())?.writerId).toBe('writer-a');

    await deleteSpoolChunk(session.sessionId, 1);
    expect((await listSpoolChunks(session.sessionId)).map((chunk) => chunk.sequence)).toEqual([2]);
  });
});
