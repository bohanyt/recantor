import { beforeEach, describe, expect, it } from 'vitest';

import type { RecordingStateResponse } from '../api/generated/types.gen';
import { chunkKey, listSpoolChunks, putSpoolChunk, recorderDb } from './db';
import { reconcileLocalSpool, sequenceAccepted } from './reconcile';

const state = {
  accepted_ranges: [
    { start: 1, end: 2 },
    { start: 4, end: 4 },
  ],
  accepted_count: 3,
  highest_contiguous_sequence: 2,
  gaps: [],
} as unknown as RecordingStateResponse;

describe('ACK reconciliation', () => {
  beforeEach(async () => {
    await recorderDb.open();
    await recorderDb.chunks.clear();
  });

  it('matches sequences against compact accepted ranges', () => {
    expect(sequenceAccepted(1, state.accepted_ranges)).toBe(true);
    expect(sequenceAccepted(3, state.accepted_ranges)).toBe(false);
    expect(sequenceAccepted(4, state.accepted_ranges)).toBe(true);
  });

  it('deletes only server-acknowledged local fragments', async () => {
    for (const sequence of [1, 2, 3, 4]) {
      const blob = new Blob([String(sequence)], { type: 'audio/webm' });
      await putSpoolChunk({
        key: chunkKey('session-r', sequence),
        sessionId: 'session-r',
        writerId: 'writer-r',
        captureEpoch: 1,
        sequence,
        monotonicStartMs: sequence - 1,
        monotonicEndMs: sequence,
        wallEndMs: sequence,
        contentType: blob.type,
        sha256: String(sequence).padStart(64, '0'),
        byteLength: blob.size,
        blob,
        createdAt: sequence,
      });
    }

    const remaining = await reconcileLocalSpool('session-r', state);
    expect(remaining.map((chunk) => chunk.sequence)).toEqual([3]);
    expect((await listSpoolChunks('session-r')).map((chunk) => chunk.sequence)).toEqual([3]);
  });
});
