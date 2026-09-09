export type StorageSafety = {
  supported: boolean;
  persisted: boolean | null;
  usageBytes: number | null;
  quotaBytes: number | null;
  remainingBytes: number | null;
  level: 'unknown' | 'safe' | 'warning';
  detail: string;
};

const WARNING_REMAINING_BYTES = 64 * 1024 * 1024;

export async function inspectStorageSafety(requestPersistence: boolean): Promise<StorageSafety> {
  const manager = navigator.storage;
  if (!manager) {
    return {
      supported: false,
      persisted: null,
      usageBytes: null,
      quotaBytes: null,
      remainingBytes: null,
      level: 'unknown',
      detail: 'Browser storage persistence information is unavailable.',
    };
  }

  let persisted: boolean | null;
  try {
    const alreadyPersisted = await manager.persisted();
    persisted =
      requestPersistence && !alreadyPersisted ? await manager.persist() : alreadyPersisted;
  } catch {
    persisted = null;
  }

  let usageBytes: number | null = null;
  let quotaBytes: number | null = null;
  try {
    const estimate = await manager.estimate();
    usageBytes = estimate.usage ?? null;
    quotaBytes = estimate.quota ?? null;
  } catch {
    // Capability reporting is advisory; a failed estimate is represented as unknown.
  }

  const remainingBytes =
    usageBytes !== null && quotaBytes !== null ? Math.max(0, quotaBytes - usageBytes) : null;
  const lowRemaining = remainingBytes !== null && remainingBytes < WARNING_REMAINING_BYTES;
  const level = persisted === true && !lowRemaining ? 'safe' : 'warning';

  let detail = persisted
    ? 'Browser recovery storage is marked persistent.'
    : 'Browser recovery storage may still be evicted by the browser.';
  if (lowRemaining) {
    detail = 'Browser recovery storage is running low on estimated free space.';
  }

  return {
    supported: true,
    persisted,
    usageBytes,
    quotaBytes,
    remainingBytes,
    level,
    detail,
  };
}
