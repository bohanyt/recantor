import { describe, expect, it } from 'vitest';

import type { TranscriptSegmentResponse } from '../api/generated/types.gen';
import {
  mergeCanonicalSegments,
  TranscriptReconnectBackoff,
  TRANSCRIPT_RECONNECT_MAX_DELAY_MS,
  transcriptReconnectDelayMs,
} from './state';

function segment(sequence: number, text: string): TranscriptSegmentResponse {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`,
    sequence,
    start_ms: (sequence - 1) * 1_000,
    end_ms: sequence * 1_000,
    text,
    language: 'id',
    created_at: '2026-09-11T00:00:00Z',
  };
}

describe('mergeCanonicalSegments', () => {
  it('deduplicates by sequence and returns canonical order', () => {
    const merged = mergeCanonicalSegments(
      [segment(2, 'dua'), segment(1, 'satu')],
      [segment(2, 'dua'), segment(3, 'tiga')],
    );

    expect(merged.map((item) => item.sequence)).toEqual([1, 2, 3]);
    expect(merged.map((item) => item.text)).toEqual(['satu', 'dua', 'tiga']);
  });
});

describe('transcript reconnect backoff', () => {
  it('uses bounded jitter inside a capped exponential ceiling', () => {
    expect(transcriptReconnectDelayMs(0, 0)).toBe(250);
    expect(transcriptReconnectDelayMs(0, 1)).toBe(500);
    expect(transcriptReconnectDelayMs(1, 1)).toBe(1_000);
    expect(transcriptReconnectDelayMs(30, 1)).toBe(TRANSCRIPT_RECONNECT_MAX_DELAY_MS);
    expect(transcriptReconnectDelayMs(30, 0)).toBe(TRANSCRIPT_RECONNECT_MAX_DELAY_MS / 2);
  });

  it('resets the exponential attempt after a ready event', () => {
    const backoff = new TranscriptReconnectBackoff();
    expect(backoff.nextDelay(1)).toBe(500);
    expect(backoff.nextDelay(1)).toBe(1_000);
    backoff.reset();
    expect(backoff.nextDelay(1)).toBe(500);
  });
});
