import { beforeEach, describe, expect, it } from 'vitest';

import {
  clearUploadRecovery,
  createUploadRecovery,
  fileFingerprint,
  loadUploadRecovery,
  saveUploadRecovery,
} from './recovery';

function file(lastModified = 1_700_000_000_000): File {
  return new File(['durable-audio'], 'meeting.webm', {
    type: 'audio/webm',
    lastModified,
  });
}

describe('upload recovery capability', () => {
  beforeEach(() => window.localStorage.clear());

  it('generates a 256-bit URL-safe bearer capability and persists browser recovery state', () => {
    const selected = file();
    const recovery = createUploadRecovery(selected);

    expect(recovery.capabilityToken).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(recovery.clientRequestId).toMatch(/^[0-9a-f-]{36}$/i);
    expect(loadUploadRecovery(selected)).toEqual(recovery);
  });

  it('keys recovery to the same file identity and clears after durable ACK', () => {
    const selected = file();
    const recovery = createUploadRecovery(selected);
    const bound = {
      ...recovery,
      sessionId: crypto.randomUUID(),
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
    };
    saveUploadRecovery(bound);

    expect(loadUploadRecovery(selected)).toEqual(bound);
    expect(loadUploadRecovery(file(selected.lastModified + 1))).toBeNull();
    expect(fileFingerprint(selected)).toBe(recovery.fingerprint);

    clearUploadRecovery(bound);
    expect(loadUploadRecovery(selected)).toBeNull();
  });
});
