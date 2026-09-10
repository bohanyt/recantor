import { describe, expect, it } from 'vitest';

import { recorderMessage } from './RecorderPanel';
import type { FencedRecorderSnapshot } from './recorder/fencedController';

function snapshot(overrides: Partial<FencedRecorderSnapshot> = {}): FencedRecorderSnapshot {
  return {
    phase: 'complete',
    sessionId: 'session-1',
    pendingChunks: 0,
    highestAckedSequence: 1,
    elapsedMs: 1_000,
    online: true,
    storage: null,
    localWriteFailed: false,
    gapCount: 0,
    missingSequences: [],
    lockKind: null,
    realtimeStatus: 'inactive',
    realtimeUtterances: 0,
    realtimeError: null,
    message: 'Recording finalized with every expected audio sequence durably acknowledged.',
    error: null,
    captureFenced: false,
    ...overrides,
  };
}

describe('recorderMessage', () => {
  it('does not claim durable audio acknowledgement for an empty completion', () => {
    expect(recorderMessage(snapshot({ highestAckedSequence: 0 }))).toBe(
      'Recording finalized with no durable audio captured.',
    );
  });

  it('preserves the normal full-audio completion message when audio was acknowledged', () => {
    const value = snapshot();
    expect(recorderMessage(value)).toBe(value.message);
  });

  it('preserves explicit gap messaging instead of describing a gap-covered result as empty', () => {
    const value = snapshot({
      highestAckedSequence: 0,
      gapCount: 1,
      message: 'Recording finalized. Explicit interruption evidence is attached.',
    });
    expect(recorderMessage(value)).toBe(value.message);
  });
});
