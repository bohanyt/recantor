import type { TranscriptSegmentResponse } from '../api/generated/types.gen';

export function mergeCanonicalSegments(
  current: readonly TranscriptSegmentResponse[],
  incoming: readonly TranscriptSegmentResponse[],
): TranscriptSegmentResponse[] {
  const bySequence = new Map<number, TranscriptSegmentResponse>();
  for (const segment of current) bySequence.set(segment.sequence, segment);
  for (const segment of incoming) bySequence.set(segment.sequence, segment);
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}
