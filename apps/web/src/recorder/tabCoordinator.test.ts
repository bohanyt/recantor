import { describe, expect, it } from 'vitest';

import { acquireCaptureTabLock } from './tabCoordinator';

describe('same-origin capture coordination', () => {
  it('prevents a second local-storage fallback owner and releases cleanly', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(navigator, 'locks');
    Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
    localStorage.clear();
    try {
      const first = await acquireCaptureTabLock('session-lock', 'writer-first');
      expect(first?.kind).toBe('local-storage');
      expect(await acquireCaptureTabLock('session-lock', 'writer-second')).toBeNull();
      first?.release();
      const second = await acquireCaptureTabLock('session-lock', 'writer-second');
      expect(second?.kind).toBe('local-storage');
      second?.release();
    } finally {
      if (descriptor) Object.defineProperty(navigator, 'locks', descriptor);
      else delete (navigator as Navigator & { locks?: unknown }).locks;
    }
  });
});
