import type { TranscriptPageResponse } from '../api/generated/types.gen';
import { env } from '../env';

async function responseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
  } catch {
    // Fall through to status text.
  }
  return response.statusText || `HTTP ${response.status}`;
}

export async function fetchTranscriptPage(
  sessionId: string,
  afterSequence: number,
  signal?: AbortSignal,
): Promise<TranscriptPageResponse> {
  const params = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: '200',
  });
  const response = await fetch(
    `${env.apiBaseUrl}/api/v1/sessions/${encodeURIComponent(sessionId)}/transcript?${params}`,
    { signal },
  );
  if (!response.ok) {
    throw new Error(await responseError(response));
  }
  return (await response.json()) as TranscriptPageResponse;
}

export function transcriptSocketUrl(sessionId: string): string {
  const url = new URL(env.apiBaseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `/api/v1/sessions/${encodeURIComponent(sessionId)}/transcript/live`;
  url.search = '';
  url.hash = '';
  return url.toString();
}
