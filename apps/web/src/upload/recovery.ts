const STORE_KEY = 'recantor:upload-recovery:v1';

export type UploadRecovery = {
  fingerprint: string;
  clientRequestId: string;
  capabilityToken: string;
  sessionId: string | null;
  expiresAt: string | null;
};

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

export function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type || 'application/octet-stream'].join(':');
}

export function loadUploadRecovery(file: File): UploadRecovery | null {
  const fingerprint = fileFingerprint(file);
  const candidate = readMap()[fingerprint];
  if (!candidate || candidate.fingerprint !== fingerprint) return null;
  if (
    typeof candidate.clientRequestId !== 'string' ||
    typeof candidate.capabilityToken !== 'string' ||
    (candidate.sessionId !== null && typeof candidate.sessionId !== 'string')
  ) {
    return null;
  }
  return candidate;
}

export function createUploadRecovery(file: File): UploadRecovery {
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
  };
  saveUploadRecovery(recovery);
  return recovery;
}

export function saveUploadRecovery(recovery: UploadRecovery): void {
  const map = readMap();
  map[recovery.fingerprint] = recovery;
  writeMap(map);
}

export function clearUploadRecovery(recovery: UploadRecovery): void {
  const map = readMap();
  delete map[recovery.fingerprint];
  writeMap(map);
}
