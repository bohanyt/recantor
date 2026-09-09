import { beforeEach, describe, expect, it } from 'vitest';

import type { RecordingStateResponse } from '../api/generated/types.gen';
import { chunkKey, listSpoolChunks, putSpoolChunk, recorderDb, type SpoolChunk } from './db';
import {
  acceptedBySameCaptureGeneration,
  reconcileLocalSpool,
  sequenceAccepted,
} from './reconcile';

function recordingState(captureEpoch: number): RecordingStateResponse {
  return {
    session: { capture_epoch: captureEpoch },
    accepted_ranges: [
      { start: 1, end: 2 },
      { start: 4, end: 4 },
    ],
    accepted_count: 3,
    highest_contiguous_sequence: 2,
    gaps: [],
  } as unknown as RecordingStateResponse;
}

function chunk(sequence: number, captureEpoch: number): SpoolChunk {
  const blob = new Blob([`${captureEpoch}:${sequence}`], { type: 'audio/webm' });
  return {
    key: chunkKey('session-r', sequence),
    sessionId: 'session-r',
    writerId: `writer-${captureEpoch}`,
    captureEpoch,
    sequence,
    monotonicStartMs: sequence - 1,
    monotonicEndMs: sequence,
    wallEndMs: sequence,
    contentType: blob.type,
    sha256: `${captureEpoch}${sequence}`.padStart(64, '0'),
    byteLength: blob.size,
    blob,
    createdAt: sequence,
  };
}

describe('ACK reconciliation', () => {
  beforeEach(async () => {
    await recorderDb.open();
    await recorderDb.chunks.clear();
  });

  it('matches sequences against compact accepted ranges', () => {
    const state = recordingState(1);
    expect(sequenceAccepted(1, state.accepted_ranges)).toBe(true);
    expect(sequenceAccepted(3, state.accepted_ranges)).toBe(false);
    expect(sequenceAccepted(4, state.accepted_ranges)).toBe(true);
  });

  it('attributes accepted sequence membership only to the same capture epoch', () => {
    const state = recordingState(2);
    expect(acceptedBySameCaptureGeneration(chunk(1, 2), state)).toBe(true);
    expect(acceptedBySameCaptureGeneration(chunk(1, 1), state)).toBe(false);
  });

  it('deletes same-epoch accepted fragments and retains different-epoch evidence', async () => {
    const state = recordingState(2);
    await putSpoolChunk(chunk(1, 2));
    await putSpoolChunk(chunk(2, 1));
    await putSpoolChunk(chunk(3, 2));
    await putSpoolChunk(chunk(4, 2));

    const remaining = await reconcileLocalSpool('session-r', state);
    expect(remaining.map((item) => [item.sequence, item.captureEpoch])).toEqual([
      [2, 1],
      [3, 2],
    ]);
    expect(
      (await listSpoolChunks('session-r')).map((item) => [item.sequence, item.captureEpoch]),
    ).toEqual([
      [2, 1],
      [3, 2],
    ]);
  });
});
