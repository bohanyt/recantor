import { env } from '../env';

export type UploadSession = {
  session_id: string;
  client_request_id: string;
  kind: 'upload';
  state: 'uploading' | 'uploaded' | 'failed';
  original_filename: string;
  content_type: string;
  declared_byte_length: number;
  declared_duration_ms: number | null;
  received_bytes: number;
  expires_at: string;
  completed_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
  upload_endpoint: string;
};

export type CreateUploadRequest = {
  client_request_id: string;
  capability_token: string;
  original_filename: string;
  content_type: string;
  byte_length: number;
  duration_ms?: number;
};

export type UploadResultState =
  | 'uploading'
  | 'preparing'
  | 'transcribing'
  | 'complete'
  | 'no_speech'
  | 'failed';

export type UploadResultStatus = {
  session_id: string;
  state: UploadResultState;
  transcript_segment_count: number;
  exports_available: boolean;
  completed_at: string | null;
  expires_at: string;
  failure_message: string | null;
};

export type UploadTranscriptSegment = {
  id: string;
  sequence: number;
  start_ms: number;
  end_ms: number;
  text: string;
  language: string | null;
  created_at: string;
};

export type UploadTranscriptPage = {
  session_id: string;
  after_sequence: number;
  next_after_sequence: number;
  has_more: boolean;
  segments: UploadTranscriptSegment[];
};

export type UploadExportFormat = 'txt' | 'json' | 'vtt' | 'srt';

export class UploadApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'UploadApiError';
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    return typeof payload.detail === 'string' ? payload.detail : `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function authorizedGet(
  path: string,
  capabilityToken: string,
): Promise<Response> {
  const response = await fetch(`${env.apiBaseUrl}${path}`, {
    headers: { 'X-Recantor-Upload-Token': capabilityToken },
  });
  if (!response.ok) throw new UploadApiError(await parseError(response), response.status);
  return response;
}

export async function createUploadSession(body: CreateUploadRequest): Promise<UploadSession> {
  const response = await fetch(`${env.apiBaseUrl}/api/v1/uploads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new UploadApiError(await parseError(response), response.status);
  return (await response.json()) as UploadSession;
}

export async function getUploadSession(
  sessionId: string,
  capabilityToken: string,
): Promise<UploadSession> {
  const response = await authorizedGet(`/api/v1/uploads/${sessionId}`, capabilityToken);
  return (await response.json()) as UploadSession;
}

export async function getUploadResultStatus(
  sessionId: string,
  capabilityToken: string,
): Promise<UploadResultStatus> {
  const response = await authorizedGet(`/api/v1/uploads/${sessionId}/result`, capabilityToken);
  return (await response.json()) as UploadResultStatus;
}

export async function getUploadTranscript(
  sessionId: string,
  capabilityToken: string,
  afterSequence = 0,
  limit = 200,
): Promise<UploadTranscriptPage> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: String(limit),
  });
  const response = await authorizedGet(
    `/api/v1/uploads/${sessionId}/transcript?${query.toString()}`,
    capabilityToken,
  );
  return (await response.json()) as UploadTranscriptPage;
}

export async function downloadUploadExport(
  sessionId: string,
  capabilityToken: string,
  exportFormat: UploadExportFormat,
): Promise<Blob> {
  const response = await authorizedGet(
    `/api/v1/uploads/${sessionId}/exports/${exportFormat}`,
    capabilityToken,
  );
  return response.blob();
}
