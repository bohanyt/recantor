import { beforeEach, describe, expect, it } from 'vitest';

import {
  clearUploadRecovery,
  createUploadRecovery,
  fileFingerprint,
  loadLatestUploadRecovery,
  loadUploadRecovery,
  markUploadDurablyComplete,
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

  it('keeps the capability after durable completion so reload can resume result tracking', () => {
    const selected = file();
    const recovery = createUploadRecovery(selected);
    const bound = {
      ...recovery,
      sessionId: crypto.randomUUID(),
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
      updatedAt: new Date(Date.now() + 1_000).toISOString(),
    };
    saveUploadRecovery(bound);
    const completedAt = new Date().toISOString();
    const completed = markUploadDurablyComplete(bound, completedAt, bound.expiresAt!);

    expect(loadUploadRecovery(selected)).toEqual(completed);
    expect(loadLatestUploadRecovery()).toEqual(completed);
    expect(completed.durableCompletedAt).toBe(completedAt);
    expect(fileFingerprint(selected)).toBe(recovery.fingerprint);
  });

  it('isolates file identities and clears a capability only when explicitly invalidated', () => {
    const selected = file();
    const recovery = createUploadRecovery(selected);
    const bound = {
      ...recovery,
      sessionId: crypto.randomUUID(),
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
      updatedAt: new Date().toISOString(),
    };
    saveUploadRecovery(bound);

    expect(loadUploadRecovery(file(selected.lastModified + 1))).toBeNull();
    clearUploadRecovery(bound);
    expect(loadUploadRecovery(selected)).toBeNull();
    expect(loadLatestUploadRecovery()).toBeNull();
  });
});
