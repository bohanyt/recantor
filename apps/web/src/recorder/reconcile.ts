import type { RecordingStateResponse, SequenceRange } from '../api/generated/types.gen';
import { deleteSpoolChunk, listSpoolChunks, type SpoolChunk } from './db';

export function sequenceAccepted(sequence: number, ranges: SequenceRange[]): boolean {
  return ranges.some((range) => sequence >= range.start && sequence <= range.end);
}

export async function reconcileLocalSpool(
  sessionId: string,
  state: RecordingStateResponse,
): Promise<SpoolChunk[]> {
  const chunks = await listSpoolChunks(sessionId);
  for (const chunk of chunks) {
    if (sequenceAccepted(chunk.sequence, state.accepted_ranges)) {
      await deleteSpoolChunk(sessionId, chunk.sequence);
    }
  }
  return listSpoolChunks(sessionId);
}
