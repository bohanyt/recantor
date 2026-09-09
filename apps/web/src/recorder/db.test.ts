import { beforeEach, describe, expect, it } from 'vitest';

import {
  appendCapturedChunk,
  chunkKey,
  deleteSpoolChunk,
  getLatestLocalSession,
  getLocalSession,
  listSpoolChunks,
  putLocalSession,
  putSpoolChunk,
  recorderDb,
  type LocalRecordingSession,
  type SpoolChunk,
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

function makeChunk(sequence: number, value = `chunk-${sequence}`): SpoolChunk {
  const blob = new Blob([value], { type: 'audio/webm' });
  return {
    key: chunkKey(session.sessionId, sequence),
    sessionId: session.sessionId,
    writerId: session.writerId,
    captureEpoch: 1,
    sequence,
    monotonicStartMs: (sequence - 1) * 2000,
    monotonicEndMs: sequence * 2000,
    wallEndMs: sequence * 2000,
    contentType: blob.type,
    sha256: `hash-${value}`,
    byteLength: blob.size,
    blob,
    createdAt: sequence,
  };
}

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
      await putSpoolChunk(makeChunk(sequence));
    }

    expect((await listSpoolChunks(session.sessionId)).map((chunk) => chunk.sequence)).toEqual([
      1, 2,
    ]);
    expect((await getLatestLocalSession())?.writerId).toBe('writer-a');

    await deleteSpoolChunk(session.sessionId, 1);
    expect((await listSpoolChunks(session.sessionId)).map((chunk) => chunk.sequence)).toEqual([2]);
  });

  it('atomically advances the local high-water and refuses a stale overwrite', async () => {
    const initial: LocalRecordingSession = {
      ...session,
      lastSequence: 0,
      lastMonotonicEndMs: 0,
      lastWallEndMs: null,
    };
    await putLocalSession(initial);

    const first = makeChunk(1, 'first-audio');
    const advanced: LocalRecordingSession = {
      ...initial,
      lastSequence: 1,
      lastMonotonicEndMs: first.monotonicEndMs,
      lastWallEndMs: first.wallEndMs,
      updatedAt: 3,
    };
    await appendCapturedChunk(first, advanced);

    expect((await getLocalSession(session.sessionId))?.lastSequence).toBe(1);
    expect(await (await listSpoolChunks(session.sessionId))[0].blob.text()).toBe('first-audio');

    const stale = makeChunk(1, 'second-tab-audio');
    await expect(appendCapturedChunk(stale, advanced)).rejects.toThrow(/expected 2/i);

    expect((await getLocalSession(session.sessionId))?.lastSequence).toBe(1);
    expect(await (await listSpoolChunks(session.sessionId))[0].blob.text()).toBe('first-audio');
  });
});
