import { describe, expect, it } from 'vitest';

import type { TranscriptSegmentResponse } from '../api/generated/types.gen';
import { mergeCanonicalSegments } from './state';

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
