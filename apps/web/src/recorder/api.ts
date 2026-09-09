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

export type RecordingApiFailureKind = 'http' | 'network' | 'timeout' | 'aborted';

export type RecordingRequestOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
};

export class RecordingApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
    public readonly kind: RecordingApiFailureKind = status === null ? 'network' : 'http',
  ) {
    super(message);
    this.name = 'RecordingApiError';
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

async function jsonRequest<T>(
  path: string,
  init?: RequestInit,
  options?: RecordingRequestOptions,
): Promise<T> {
  const timeoutMs = options?.timeoutMs ?? env.recordingRequestTimeoutMs;
  const controller = new AbortController();
  let timedOut = false;

  const abortFromCaller = () => controller.abort();
  if (options?.signal?.aborted) {
    controller.abort();
  } else {
    options?.signal?.addEventListener('abort', abortFromCaller, { once: true });
  }

  const timeoutId = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(`${env.apiBaseUrl}${path}`, {
      ...init,
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new RecordingApiError(await parseError(response), response.status, 'http');
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof RecordingApiError) throw error;
    if (options?.signal?.aborted) {
      throw new RecordingApiError('recording request was cancelled', null, 'aborted');
    }
    if (timedOut) {
      throw new RecordingApiError(
        `recording request timed out after ${timeoutMs} ms`,
        null,
        'timeout',
      );
    }
    throw new RecordingApiError(
      error instanceof Error ? error.message : 'network request failed',
      null,
      'network',
    );
  } finally {
    globalThis.clearTimeout(timeoutId);
    options?.signal?.removeEventListener('abort', abortFromCaller);
  }
}

export function createLiveSession(
  body: CreateLiveSessionRequest,
  options?: RecordingRequestOptions,
): Promise<RecordingSessionResponse> {
  return jsonRequest(
    '/api/v1/sessions/live',
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    },
    options,
  );
}

export function claimCapture(
  sessionId: string,
  body: CaptureClaimRequest,
  options?: RecordingRequestOptions,
): Promise<RecordingSessionResponse> {
  return jsonRequest(
    `/api/v1/sessions/${sessionId}/capture/claim`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    },
    options,
  );
}

export function heartbeatCapture(
  sessionId: string,
  writerId: string,
  captureEpoch: number,
  options?: RecordingRequestOptions,
): Promise<RecordingSessionResponse> {
  return jsonRequest(
    `/api/v1/sessions/${sessionId}/heartbeat`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ writer_id: writerId, capture_epoch: captureEpoch }),
    },
    options,
  );
}

export function fetchRecordingState(
  sessionId: string,
  options?: RecordingRequestOptions,
): Promise<RecordingStateResponse> {
  return jsonRequest(`/api/v1/sessions/${sessionId}/recording-state`, undefined, options);
}

export async function uploadRecordingChunk(
  chunk: SpoolChunk,
  options?: RecordingRequestOptions,
): Promise<ChunkAckResponse> {
  const params = new URLSearchParams({
    writer_id: chunk.writerId,
    capture_epoch: String(chunk.captureEpoch),
    monotonic_start_ms: String(chunk.monotonicStartMs),
    monotonic_end_ms: String(chunk.monotonicEndMs),
    sha256: chunk.sha256,
    content_type: chunk.contentType,
  });
  return jsonRequest(
    `/api/v1/sessions/${chunk.sessionId}/chunks/${chunk.sequence}?${params}`,
    {
      method: 'PUT',
      headers: { 'content-type': 'application/octet-stream' },
      body: chunk.blob,
    },
    options,
  );
}

export function declareRecordingGap(
  sessionId: string,
  body: GapDeclarationRequest,
  options?: RecordingRequestOptions,
): Promise<RecordingGapResponse> {
  return jsonRequest(
    `/api/v1/sessions/${sessionId}/gaps`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    },
    options,
  );
}

export function finalizeRecording(
  sessionId: string,
  body: FinalizeSessionRequest,
  options?: RecordingRequestOptions,
): Promise<FinalizeSessionResponse> {
  return jsonRequest(
    `/api/v1/sessions/${sessionId}/finalize`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    },
    options,
  );
}
