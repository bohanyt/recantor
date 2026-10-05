import { sha256Blob } from '../recorder/hash';

const STORE_KEY = 'recantor:upload-recovery:v1';

export const CONTENT_EVIDENCE_ALGORITHM = 'sha256-size-head-tail-256k-v1';
const CONTENT_SAMPLE_BYTES = 256 * 1024;

/**
 * Bounded content evidence: SHA-256 over the byte length plus the first and last 256 KiB.
 * It complements the metadata fingerprint without hashing a potentially huge media file.
 * Bytes between the two samples are not covered.
 */
export type ContentEvidence = {
  algorithm: string;
  digest: string;
};

export type UploadRecovery = {
  fingerprint: string;
  clientRequestId: string;
  capabilityToken: string;
  sessionId: string | null;
  expiresAt: string | null;
  updatedAt: string;
  durableCompletedAt: string | null;
  contentEvidence?: ContentEvidence | null;
};

export type RecoveryFileMatch = 'match' | 'metadata_changed' | 'content_changed' | 'no_evidence';

export class UploadFileChangedError extends Error {
  constructor() {
    super('The selected file no longer matches the saved upload.');
    this.name = 'UploadFileChangedError';
  }
}

type RecoveryMap = Record<string, UploadRecovery>;

function readMap(): RecoveryMap {
  const raw = window.localStorage.getItem(STORE_KEY);
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return parsed as RecoveryMap;
  } catch {
    return {};
  }
}

function writeMap(value: RecoveryMap): void {
  window.localStorage.setItem(STORE_KEY, JSON.stringify(value));
}

function normalizeEvidence(candidate: unknown): ContentEvidence | null {
  if (!candidate || typeof candidate !== 'object') return null;
  const { algorithm, digest } = candidate as Partial<ContentEvidence>;
  return typeof algorithm === 'string' && typeof digest === 'string' ? { algorithm, digest } : null;
}

function normalizeRecovery(candidate: UploadRecovery | undefined): UploadRecovery | null {
  if (!candidate || typeof candidate.fingerprint !== 'string') return null;
  if (
    typeof candidate.clientRequestId !== 'string' ||
    typeof candidate.capabilityToken !== 'string' ||
    (candidate.sessionId !== null && typeof candidate.sessionId !== 'string')
  ) {
    return null;
  }
  return {
    ...candidate,
    expiresAt: typeof candidate.expiresAt === 'string' ? candidate.expiresAt : null,
    updatedAt:
      typeof candidate.updatedAt === 'string'
        ? candidate.updatedAt
        : candidate.expiresAt || new Date(0).toISOString(),
    durableCompletedAt:
      typeof candidate.durableCompletedAt === 'string' ? candidate.durableCompletedAt : null,
    contentEvidence: normalizeEvidence(candidate.contentEvidence),
  };
}

export function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type || 'application/octet-stream'].join(
    ':',
  );
}

export async function computeContentEvidence(file: Blob): Promise<ContentEvidence> {
  const size = new DataView(new ArrayBuffer(8));
  size.setBigUint64(0, BigInt(file.size));
  const head = file.slice(0, CONTENT_SAMPLE_BYTES);
  const tail = file.slice(Math.max(0, file.size - CONTENT_SAMPLE_BYTES), file.size);
  return {
    algorithm: CONTENT_EVIDENCE_ALGORITHM,
    digest: await sha256Blob(new Blob([size.buffer, head, tail])),
  };
}

export function contentEvidenceEquals(
  left: ContentEvidence | null | undefined,
  right: ContentEvidence | null | undefined,
): boolean {
  return !!left && !!right && left.algorithm === right.algorithm && left.digest === right.digest;
}

export async function checkFileAgainstRecovery(
  file: File,
  recovery: UploadRecovery,
): Promise<RecoveryFileMatch> {
  if (fileFingerprint(file) !== recovery.fingerprint) return 'metadata_changed';
  if (!recovery.contentEvidence) return 'no_evidence';
  const evidence = await computeContentEvidence(file);
  return contentEvidenceEquals(recovery.contentEvidence, evidence) ? 'match' : 'content_changed';
}

export function loadUploadRecovery(file: File): UploadRecovery | null {
  const fingerprint = fileFingerprint(file);
  const candidate = normalizeRecovery(readMap()[fingerprint]);
  if (!candidate || candidate.fingerprint !== fingerprint) return null;
  return candidate;
}

export function loadLatestUploadRecovery(): UploadRecovery | null {
  return (
    Object.values(readMap())
      .map((candidate) => normalizeRecovery(candidate))
      .filter(
        (candidate): candidate is UploadRecovery =>
          candidate !== null && candidate.sessionId !== null,
      )
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))[0] ?? null
  );
}

export function createUploadRecovery(
  file: File,
  contentEvidence: ContentEvidence | null = null,
): UploadRecovery {
  const bytes = window.crypto.getRandomValues(new Uint8Array(32));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const capabilityToken = window
    .btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '');
  const recovery: UploadRecovery = {
    fingerprint: fileFingerprint(file),
    clientRequestId: window.crypto.randomUUID(),
    capabilityToken,
    sessionId: null,
    expiresAt: null,
    updatedAt: new Date().toISOString(),
    durableCompletedAt: null,
    contentEvidence,
  };
  saveUploadRecovery(recovery);
  return recovery;
}

export function saveUploadRecovery(recovery: UploadRecovery): void {
  const map = readMap();
  map[recovery.fingerprint] = recovery;
  writeMap(map);
}

export function markUploadDurablyComplete(
  recovery: UploadRecovery,
  completedAt: string,
  expiresAt: string,
): UploadRecovery {
  const completed = {
    ...recovery,
    expiresAt,
    updatedAt: new Date().toISOString(),
    durableCompletedAt: completedAt,
  };
  saveUploadRecovery(completed);
  return completed;
}

export function clearUploadRecovery(recovery: UploadRecovery): void {
  const map = readMap();
  delete map[recovery.fingerprint];
  writeMap(map);
}
