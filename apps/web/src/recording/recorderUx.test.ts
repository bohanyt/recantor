import { describe, expect, it } from 'vitest';

import type { FencedRecorderSnapshot } from '../recorder/fencedController';
import { deriveRecorderUx } from './recorderUx';

function snapshot(overrides: Partial<FencedRecorderSnapshot> = {}): FencedRecorderSnapshot {
  return {
    phase: 'idle',
    sessionId: null,
    pendingChunks: 0,
    highestAckedSequence: 0,
    elapsedMs: 0,
    online: true,
    storage: null,
    localWriteFailed: false,
    gapCount: 0,
    missingSequences: [],
    lockKind: null,
    realtimeStatus: 'inactive',
    realtimeUtterances: 0,
    realtimeError: null,
    message: 'Ready to record.',
    error: null,
    captureFenced: false,
    ...overrides,
  };
}

describe('deriveRecorderUx', () => {
  it('keeps one obvious primary action across the normal lifecycle', () => {
    expect(deriveRecorderUx(snapshot()).actions).toEqual({
      primary: 'start',
      secondary: [],
      danger: [],
    });
    expect(
      deriveRecorderUx(snapshot({ phase: 'recording', sessionId: 's1', pendingChunks: 1 })).actions,
    ).toEqual({
      primary: 'stop',
      secondary: ['sync'],
      danger: [],
    });
    expect(
      deriveRecorderUx(snapshot({ phase: 'finalizing', sessionId: 's1', pendingChunks: 1 }))
        .actions,
    ).toEqual({
      primary: 'defer-finalization',
      secondary: [],
      danger: [],
    });
  });

  it('preserves recoverable actions and never offers resume while audio is known missing', () => {
    expect(
      deriveRecorderUx(snapshot({ phase: 'recoverable', sessionId: 's1', pendingChunks: 2 }))
        .actions,
    ).toEqual({
      primary: 'resume',
      secondary: ['finish-recovered', 'sync'],
      danger: [],
    });

    const missing = deriveRecorderUx(
      snapshot({
        phase: 'recoverable',
        sessionId: 's1',
        pendingChunks: 1,
        missingSequences: [2, 4],
      }),
    );
    expect(missing.actions.primary).toBe('finish-recovered');
    expect(missing.actions.secondary).toContain('sync');
    expect(missing.actions.danger).toEqual(['declare-missing-gaps']);
    expect([
      missing.actions.primary,
      ...missing.actions.secondary,
      ...missing.actions.danger,
    ]).not.toContain('resume');
    expect(missing.audio.label).toMatch(/Audio missing/);
  });

  it('withholds every mutation action when capture ownership is fenced', () => {
    const fenced = deriveRecorderUx(
      snapshot({
        phase: 'recoverable',
        sessionId: 's1',
        pendingChunks: 3,
        captureFenced: true,
      }),
    );
    expect(fenced.actions).toEqual({ primary: null, secondary: [], danger: [] });
    expect(fenced.audio.label).toMatch(/Orphaned local evidence/);
    expect(fenced.audio.tone).toBe('danger');
  });

  it('keeps archive-audio safety independent from transcription state', () => {
    const delayed = deriveRecorderUx(
      snapshot({
        phase: 'recording',
        sessionId: 's1',
        pendingChunks: 0,
        realtimeStatus: 'degraded',
        realtimeError: 'socket backpressure',
      }),
    );
    expect(delayed.audio.label).toBe('Audio synced');
    expect(delayed.audio.tone).toBe('positive');
    expect(delayed.transcription.label).toBe('Transcription delayed');
    expect(delayed.transcription.tone).toBe('warning');
  });

  it('surfaces local spool failure and offline retained audio as explicit audio states', () => {
    const unsafe = deriveRecorderUx(
      snapshot({ phase: 'recoverable', sessionId: 's1', localWriteFailed: true }),
    );
    expect(unsafe.audio.label).toBe('Unsafe audio storage');
    expect(unsafe.audio.tone).toBe('danger');

    const offline = deriveRecorderUx(
      snapshot({
        phase: 'recording',
        sessionId: 's1',
        pendingChunks: 2,
        online: false,
      }),
    );
    expect(offline.audio.label).toBe('Audio saved locally');
    expect(offline.audio.detail).toMatch(/best-effort basis/);
  });

  it('does not claim downstream transcription completion from recorder completion alone', () => {
    const complete = deriveRecorderUx(
      snapshot({ phase: 'complete', sessionId: 's1', realtimeStatus: 'inactive' }),
    );
    expect(complete.transcription.label).toBe('Processing may continue');
    expect(complete.transcription.detail).toMatch(
      /does not claim downstream transcription is complete/i,
    );
  });
});
