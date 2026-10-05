import { beforeEach, describe, expect, it } from 'vitest';

import {
  checkFileAgainstRecovery,
  clearUploadRecovery,
  computeContentEvidence,
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

const SAMPLE = 256 * 1024;

function sized(
  length: number,
  mutate?: (bytes: Uint8Array) => void,
  lastModified = 1_700_000_000_000,
) {
  const bytes = new Uint8Array(length).map((_, index) => index % 251);
  mutate?.(bytes);
  return new File([bytes], 'meeting.webm', { type: 'audio/webm', lastModified });
}

describe('bounded content evidence', () => {
  beforeEach(() => window.localStorage.clear());

  it('is deterministic and covers the byte length, the head, and the tail', async () => {
    const length = 3 * SAMPLE;
    const base = await computeContentEvidence(sized(length));

    expect(base.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(await computeContentEvidence(sized(length))).toEqual(base);
    expect(await computeContentEvidence(sized(length, (b) => (b[0] ^= 0xff)))).not.toEqual(base);
    expect(await computeContentEvidence(sized(length, (b) => (b[length - 1] ^= 0xff)))).not.toEqual(
      base,
    );
    expect(await computeContentEvidence(sized(length + 1))).not.toEqual(base);
  });

  it('does not read the middle of a large file (documented bounded coverage)', async () => {
    const length = 3 * SAMPLE;
    const base = await computeContentEvidence(sized(length));
    const middle = await computeContentEvidence(sized(length, (b) => (b[length / 2] ^= 0xff)));

    expect(middle).toEqual(base);
  });

  it('persists with a new recovery and normalizes legacy records to no evidence', async () => {
    const selected = sized(1024);
    const evidence = await computeContentEvidence(selected);
    const created = createUploadRecovery(selected, evidence);

    expect(loadUploadRecovery(selected)?.contentEvidence).toEqual(evidence);
    expect(created.contentEvidence).toEqual(evidence);

    const legacy = { ...created, contentEvidence: undefined };
    window.localStorage.setItem(
      'recantor:upload-recovery:v1',
      JSON.stringify({ [created.fingerprint]: legacy }),
    );
    expect(loadUploadRecovery(selected)?.contentEvidence).toBeNull();
  });

  it('classifies a candidate file against a saved recovery', async () => {
    const original = sized(2048);
    const recovery = createUploadRecovery(original, await computeContentEvidence(original));

    expect(await checkFileAgainstRecovery(sized(2048), recovery)).toBe('match');
    expect(await checkFileAgainstRecovery(sized(2048, undefined, 1), recovery)).toBe(
      'metadata_changed',
    );
    expect(await checkFileAgainstRecovery(sized(4096), recovery)).toBe('metadata_changed');
    // Same name, size, type and mtime but different bytes: only content evidence can catch this.
    const substituted = sized(2048, (b) => (b[10] ^= 0xff));
    expect(fileFingerprint(substituted)).toBe(recovery.fingerprint);
    expect(await checkFileAgainstRecovery(substituted, recovery)).toBe('content_changed');

    const legacy = createUploadRecovery(sized(2048, undefined, 5));
    expect(await checkFileAgainstRecovery(sized(2048, undefined, 5), legacy)).toBe('no_evidence');
  });
});
