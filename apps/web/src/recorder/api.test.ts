import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchRecordingState } from './api';

function pendingFetch() {
  return vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    return new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      const rejectAbort = () => reject(new DOMException('Aborted', 'AbortError'));
      if (signal?.aborted) {
        rejectAbort();
        return;
      }
      signal?.addEventListener('abort', rejectAbort, { once: true });
    });
  });
}

describe('recorder HTTP request bounds', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('classifies a request that never settles as a retryable timeout', async () => {
    vi.stubGlobal('fetch', pendingFetch());

    await expect(fetchRecordingState('session-timeout', { timeoutMs: 10 })).rejects.toMatchObject({
      status: null,
      kind: 'timeout',
    });
  });

  it('keeps caller cancellation distinct from a timeout', async () => {
    vi.stubGlobal('fetch', pendingFetch());
    const controller = new AbortController();
    const request = fetchRecordingState('session-cancelled', {
      signal: controller.signal,
      timeoutMs: 1_000,
    });

    controller.abort();

    await expect(request).rejects.toMatchObject({
      status: null,
      kind: 'aborted',
    });
  });
});
