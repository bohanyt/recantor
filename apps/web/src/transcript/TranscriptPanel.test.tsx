import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TranscriptPageResponse, TranscriptSegmentResponse } from '../api/generated/types.gen';
import { fetchTranscriptPage } from './api';
import { TranscriptPanel } from './TranscriptPanel';
import { mergeCanonicalSegments } from './state';

vi.mock('./api', () => ({
  fetchTranscriptPage: vi.fn(),
  transcriptSocketUrl: vi.fn(() => 'ws://example.test/api/v1/sessions/session-1/transcript/live'),
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  close() {}

  emit(payload: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }));
  }
}

function segment(sequence: number, text: string): TranscriptSegmentResponse {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`,
    sequence,
    start_ms: (sequence - 1) * 1_000,
    end_ms: sequence * 1_000,
    text,
    language: 'id',
    created_at: '2026-09-11T00:00:00Z',
  };
}

function page(afterSequence: number, segments: TranscriptSegmentResponse[]): TranscriptPageResponse {
  const next = segments.reduce(
    (highest, item) => Math.max(highest, item.sequence),
    afterSequence,
  );
  return {
    session_id: '00000000-0000-4000-8000-000000000001',
    after_sequence: afterSequence,
    next_after_sequence: next,
    has_more: false,
    segments,
  };
}

describe('TranscriptPanel', () => {
  const originalWebSocket = globalThis.WebSocket;
  let canonical: TranscriptSegmentResponse[];

  beforeEach(() => {
    FakeWebSocket.instances = [];
    canonical = [segment(1, 'pertama')];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    vi.mocked(fetchTranscriptPage).mockImplementation(async (_sessionId, afterSequence) => {
      return page(
        afterSequence,
        canonical.filter((item) => item.sequence > afterSequence),
      );
    });
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
    vi.clearAllMocks();
  });

  it('rebuilds from sequence zero and treats duplicate or out-of-order realtime events as hints', async () => {
    const view = render(<TranscriptPanel sessionId="session-1" />);

    await waitFor(() => {
      expect(fetchTranscriptPage).toHaveBeenCalledWith('session-1', 0, expect.any(AbortSignal));
    });
    expect(await screen.findByText('pertama')).toBeInTheDocument();

    canonical = [segment(3, 'ketiga'), segment(1, 'pertama'), segment(2, 'kedua')];
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.emit({ type: 'transcript_available', sequence: 3 });
      socket.emit({ type: 'transcript_available', sequence: 2 });
      socket.emit({ type: 'transcript_available', sequence: 3 });
    });

    expect(await screen.findByText('kedua')).toBeInTheDocument();
    expect(await screen.findByText('ketiga')).toBeInTheDocument();
    const rows = screen.getAllByRole('listitem');
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('pertama'),
      expect.stringContaining('kedua'),
      expect.stringContaining('ketiga'),
    ]);
    expect(screen.getAllByText('pertama')).toHaveLength(1);
    expect(screen.getAllByText('ketiga')).toHaveLength(1);

    view.unmount();
  });

  it('deduplicates canonical pages by sequence and preserves canonical order', () => {
    const merged = mergeCanonicalSegments(
      [segment(2, 'dua'), segment(1, 'satu')],
      [segment(2, 'dua'), segment(3, 'tiga')],
    );
    expect(merged.map((item) => item.sequence)).toEqual([1, 2, 3]);
  });
});
