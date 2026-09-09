type CaptureLockKind = 'web-locks' | 'local-storage' | 'server-only';

export type CaptureTabLock = {
  kind: CaptureLockKind;
  release: () => void;
};

type LockManagerLike = {
  request: (
    name: string,
    options: { mode: 'exclusive'; ifAvailable: true },
    callback: (lock: object | null) => Promise<void>,
  ) => Promise<void>;
};

type NavigatorWithLocks = Navigator & { locks?: LockManagerLike };

type Lease = { ownerId: string; expiresAt: number };

const FALLBACK_LEASE_MS = 10_000;
const FALLBACK_REFRESH_MS = 3_000;

function parseLease(raw: string | null): Lease | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Lease;
    if (typeof value.ownerId !== 'string' || typeof value.expiresAt !== 'number') return null;
    return value;
  } catch {
    return null;
  }
}

async function acquireWebLock(sessionId: string): Promise<CaptureTabLock | null | undefined> {
  const manager = (navigator as NavigatorWithLocks).locks;
  if (!manager) return undefined;

  let releaseHold: (() => void) | null = null;
  let resolveAcquired: ((value: boolean) => void) | null = null;
  const acquired = new Promise<boolean>((resolve) => {
    resolveAcquired = resolve;
  });
  const hold = new Promise<void>((resolve) => {
    releaseHold = resolve;
  });

  void manager.request(
    `recantor:capture:${sessionId}`,
    { mode: 'exclusive', ifAvailable: true },
    async (lock) => {
      resolveAcquired?.(Boolean(lock));
      if (lock) await hold;
    },
  );

  if (!(await acquired)) return null;
  return {
    kind: 'web-locks',
    release: () => releaseHold?.(),
  };
}

function acquireLocalStorageLock(sessionId: string, ownerId: string): CaptureTabLock | null | undefined {
  const key = `recantor:capture:${sessionId}`;
  try {
    const now = Date.now();
    const existing = parseLease(localStorage.getItem(key));
    if (existing && existing.ownerId !== ownerId && existing.expiresAt > now) return null;

    const writeLease = () => {
      const lease: Lease = { ownerId, expiresAt: Date.now() + FALLBACK_LEASE_MS };
      localStorage.setItem(key, JSON.stringify(lease));
    };
    writeLease();
    const verified = parseLease(localStorage.getItem(key));
    if (!verified || verified.ownerId !== ownerId) return null;

    const refresh = window.setInterval(writeLease, FALLBACK_REFRESH_MS);
    return {
      kind: 'local-storage',
      release: () => {
        window.clearInterval(refresh);
        const current = parseLease(localStorage.getItem(key));
        if (current?.ownerId === ownerId) localStorage.removeItem(key);
      },
    };
  } catch {
    return undefined;
  }
}

export async function acquireCaptureTabLock(
  sessionId: string,
  ownerId: string,
): Promise<CaptureTabLock | null> {
  const webLock = await acquireWebLock(sessionId);
  if (webLock !== undefined) return webLock;

  const localLock = acquireLocalStorageLock(sessionId, ownerId);
  if (localLock !== undefined) return localLock;

  // The server capture epoch remains authoritative when browser coordination APIs are unavailable.
  return { kind: 'server-only', release: () => undefined };
}
