import type {
  CaptureClaimRequest,
  ChunkAckResponse,
  CreateLiveSessionRequest,
  FinalizeSessionRequest,
  FinalizeSessionResponse,
  GapDeclarationRequest,
  RecordingGapResponse,
  RecordingSessionResponse,
  RecordingStateResponse,
} from '../api/generated/types.gen';
import { env } from '../env';
import type { SpoolChunk } from './db';

export class RecordingApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
  ) {
    super(message);
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
  } catch {
    // Fall through to status text.
  }
  return response.statusText || `HTTP ${response.status}`;
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${env.apiBaseUrl}${path}`, init);
  } catch (error) {
    throw new RecordingApiError(
      error instanceof Error ? error.message : 'network request failed',
      null,
    );
  }
  if (!response.ok) throw new RecordingApiError(await parseError(response), response.status);
  return (await response.json()) as T;
}

export function createLiveSession(
  body: CreateLiveSessionRequest,
): Promise<RecordingSessionResponse> {
  return jsonRequest('/api/v1/sessions/live', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function claimCapture(
  sessionId: string,
  body: CaptureClaimRequest,
): Promise<RecordingSessionResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/capture/claim`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function heartbeatCapture(
  sessionId: string,
  writerId: string,
  captureEpoch: number,
): Promise<RecordingSessionResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/heartbeat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ writer_id: writerId, capture_epoch: captureEpoch }),
  });
}

export function fetchRecordingState(sessionId: string): Promise<RecordingStateResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/recording-state`);
}

export async function uploadRecordingChunk(chunk: SpoolChunk): Promise<ChunkAckResponse> {
  const params = new URLSearchParams({
    writer_id: chunk.writerId,
    capture_epoch: String(chunk.captureEpoch),
    monotonic_start_ms: String(chunk.monotonicStartMs),
    monotonic_end_ms: String(chunk.monotonicEndMs),
    sha256: chunk.sha256,
    content_type: chunk.contentType,
  });
  return jsonRequest(`/api/v1/sessions/${chunk.sessionId}/chunks/${chunk.sequence}?${params}`, {
    method: 'PUT',
    headers: { 'content-type': 'application/octet-stream' },
    body: chunk.blob,
  });
}

export function declareRecordingGap(
  sessionId: string,
  body: GapDeclarationRequest,
): Promise<RecordingGapResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/gaps`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function finalizeRecording(
  sessionId: string,
  body: FinalizeSessionRequest,
): Promise<FinalizeSessionResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/finalize`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}
