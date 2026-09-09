import { afterEach, describe, expect, it, vi } from 'vitest';

import { inspectStorageSafety } from './storageSafety';

const originalStorage = Object.getOwnPropertyDescriptor(navigator, 'storage');

function installStorage(manager: Partial<StorageManager>): void {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: manager,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  if (originalStorage) Object.defineProperty(navigator, 'storage', originalStorage);
  else Reflect.deleteProperty(navigator, 'storage');
});

describe('browser storage safety', () => {
  it('requests persistence and reports a safe recovery spool when persistence is granted', async () => {
    const persist = vi.fn().mockResolvedValue(true);
    installStorage({
      persisted: vi.fn().mockResolvedValue(false),
      persist,
      estimate: vi.fn().mockResolvedValue({ usage: 10_000, quota: 512 * 1024 * 1024 }),
    });

    const safety = await inspectStorageSafety(true);

    expect(persist).toHaveBeenCalledOnce();
    expect(safety.persisted).toBe(true);
    expect(safety.level).toBe('safe');
    expect(safety.remainingBytes).toBeGreaterThan(64 * 1024 * 1024);
  });

  it('never presents denied persistence or low remaining quota as fully safe', async () => {
    installStorage({
      persisted: vi.fn().mockResolvedValue(false),
      persist: vi.fn().mockResolvedValue(false),
      estimate: vi.fn().mockResolvedValue({
        usage: 95 * 1024 * 1024,
        quota: 100 * 1024 * 1024,
      }),
    });

    const safety = await inspectStorageSafety(true);

    expect(safety.persisted).toBe(false);
    expect(safety.level).toBe('warning');
    expect(safety.detail).toMatch(/running low/i);
  });
});
