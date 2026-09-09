import type { RecordingStateResponse, SequenceRange } from '../api/generated/types.gen';
import { deleteSpoolChunk, listSpoolChunks, type SpoolChunk } from './db';

export function sequenceAccepted(sequence: number, ranges: SequenceRange[]): boolean {
  return ranges.some((range) => sequence >= range.start && sequence <= range.end);
}

export function acceptedBySameCaptureGeneration(
  chunk: SpoolChunk,
  state: RecordingStateResponse,
): boolean {
  // Phase 1 interim safety rule only: compact accepted ranges do not carry per-sequence
  // identity, so never use sequence membership to delete evidence from another epoch.
  // The proper multi-client reconciliation identity contract remains ADR-gated (#12).
  return (
    chunk.captureEpoch === state.session.capture_epoch &&
    sequenceAccepted(chunk.sequence, state.accepted_ranges)
  );
}

export async function reconcileLocalSpool(
  sessionId: string,
  state: RecordingStateResponse,
): Promise<SpoolChunk[]> {
  const chunks = await listSpoolChunks(sessionId);
  for (const chunk of chunks) {
    if (acceptedBySameCaptureGeneration(chunk, state)) {
      await deleteSpoolChunk(sessionId, chunk.sequence);
    }
  }
  return listSpoolChunks(sessionId);
}
