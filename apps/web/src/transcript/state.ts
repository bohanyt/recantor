import type { TranscriptSegmentResponse } from '../api/generated/types.gen';

const RECONNECT_INITIAL_CEILING_MS = 500;
export const TRANSCRIPT_RECONNECT_MAX_DELAY_MS = 15_000;

export function mergeCanonicalSegments(
  current: readonly TranscriptSegmentResponse[],
  incoming: readonly TranscriptSegmentResponse[],
): TranscriptSegmentResponse[] {
  const bySequence = new Map<number, TranscriptSegmentResponse>();
  for (const segment of current) bySequence.set(segment.sequence, segment);
  for (const segment of incoming) bySequence.set(segment.sequence, segment);
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

export function transcriptReconnectDelayMs(attempt: number, randomUnit = Math.random()): number {
  const safeAttempt = Math.max(0, Math.min(30, Math.floor(attempt)));
  const ceiling = Math.min(
    RECONNECT_INITIAL_CEILING_MS * 2 ** safeAttempt,
    TRANSCRIPT_RECONNECT_MAX_DELAY_MS,
  );
  const boundedRandom = Math.max(0, Math.min(1, randomUnit));
  return Math.round(ceiling / 2 + (ceiling / 2) * boundedRandom);
}
