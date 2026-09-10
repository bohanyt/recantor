import { describe, expect, it } from 'vitest';

import { encodePcmPacket, realtimeWebSocketUrl } from './realtimeAudio';

describe('realtime PCM transport helpers', () => {
  it('maps the configured API origin to the realtime websocket route', () => {
    const url = new URL(realtimeWebSocketUrl('session id'));
    expect(['ws:', 'wss:']).toContain(url.protocol);
    expect(url.pathname).toBe('/api/v1/sessions/session%20id/realtime-audio');
  });

  it('encodes a uint64 sample offset followed by little-endian s16 PCM', () => {
    const offset = 2 ** 32 + 7;
    const packet = encodePcmPacket(offset, new Float32Array([-1, -0.5, 0, 0.5, 1]));
    const view = new DataView(packet);

    expect(view.getUint32(0, true)).toBe(7);
    expect(view.getUint32(4, true)).toBe(1);
    expect(view.getInt16(8, true)).toBe(-32768);
    expect(view.getInt16(10, true)).toBe(-16384);
    expect(view.getInt16(12, true)).toBe(0);
    expect(view.getInt16(14, true)).toBe(16384);
    expect(view.getInt16(16, true)).toBe(32767);
  });

  it('clamps input samples and rejects unsafe offsets', () => {
    const packet = encodePcmPacket(0, new Float32Array([-2, 2]));
    const view = new DataView(packet);
    expect(view.getInt16(8, true)).toBe(-32768);
    expect(view.getInt16(10, true)).toBe(32767);
    expect(() => encodePcmPacket(-1, new Float32Array([0]))).toThrow(/non-negative/);
    expect(() => encodePcmPacket(Number.MAX_SAFE_INTEGER + 1, new Float32Array([0]))).toThrow(
      /safe integer/,
    );
  });
});
