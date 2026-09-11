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
  const response = await fetch(`${env.apiBaseUrl}/api/v1/uploads/${sessionId}`, {
    headers: { 'X-Recantor-Upload-Token': capabilityToken },
  });
  if (!response.ok) throw new UploadApiError(await parseError(response), response.status);
  return (await response.json()) as UploadSession;
}
