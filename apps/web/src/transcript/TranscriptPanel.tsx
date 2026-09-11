import { useEffect, useState } from 'react';

import type { TranscriptSegmentResponse } from '../api/generated/types.gen';
import { fetchTranscriptPage, transcriptSocketUrl } from './api';

type DeliveryState = 'idle' | 'starting' | 'live' | 'recovering' | 'degraded';

export function mergeCanonicalSegments(
  current: readonly TranscriptSegmentResponse[],
  incoming: readonly TranscriptSegmentResponse[],
): TranscriptSegmentResponse[] {
  const bySequence = new Map<number, TranscriptSegmentResponse>();
  for (const segment of current) bySequence.set(segment.sequence, segment);
  for (const segment of incoming) bySequence.set(segment.sequence, segment);
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

function realtimeType(data: unknown): string | null {
  if (typeof data !== 'string') return null;
  try {
    const parsed = JSON.parse(data) as { type?: unknown };
    return typeof parsed.type === 'string' ? parsed.type : null;
  } catch {
    return null;
  }
}

function timestampLabel(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

function deliveryLabel(state: DeliveryState): string {
  if (state === 'live') return 'Live';
  if (state === 'recovering') return 'Catching up';
  if (state === 'degraded') return 'Delayed';
  if (state === 'starting') return 'Starting';
  return 'Waiting';
}

export function TranscriptPanel({ sessionId }: { sessionId: string | null }) {
  const [segments, setSegments] = useState<TranscriptSegmentResponse[]>([]);
  const [deliveryState, setDeliveryState] = useState<DeliveryState>('idle');
  const [detail, setDetail] = useState<string | null>(null);

  useEffect(() => {
    setSegments([]);
    setDetail(null);
    setDeliveryState(sessionId ? 'starting' : 'idle');
    if (!sessionId) return;

    let disposed = false;
    let cursor = 0;
    let syncing = false;
    let syncAgain = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    const abortController = new AbortController();

    const syncCanonical = async (): Promise<void> => {
      if (disposed) return;
      if (syncing) {
        syncAgain = true;
        return;
      }
      syncing = true;
      try {
        do {
          syncAgain = false;
          let afterSequence = cursor;
          while (!disposed) {
            const page = await fetchTranscriptPage(
              sessionId,
              afterSequence,
              abortController.signal,
            );
            if (disposed) return;
            setSegments((current) => mergeCanonicalSegments(current, page.segments));
            const nextCursor = Math.max(afterSequence, page.next_after_sequence);
            cursor = Math.max(cursor, nextCursor);
            if (!page.has_more || nextCursor <= afterSequence) break;
            afterSequence = nextCursor;
          }
        } while (syncAgain && !disposed);
      } catch (error) {
        if (abortController.signal.aborted || disposed) return;
        setDeliveryState((current) => (current === 'live' ? 'recovering' : 'degraded'));
        setDetail(
          error instanceof Error
            ? `Transcript recovery is temporarily unavailable: ${error.message}`
            : 'Transcript recovery is temporarily unavailable.',
        );
      } finally {
        syncing = false;
      }
    };

    const connect = () => {
      if (disposed) return;
      const nextSocket = new WebSocket(transcriptSocketUrl(sessionId));
      socket = nextSocket;

      nextSocket.onmessage = (event) => {
        const type = realtimeType(event.data);
        if (type === 'ready') {
          setDeliveryState('live');
          setDetail(null);
          // Catch up after subscription is established. This closes the fetch/subscribe race.
          void syncCanonical();
          return;
        }
        if (type === 'transcript_available') {
          // The event is only a wake-up hint. Never merge its payload as transcript truth.
          void syncCanonical();
          return;
        }
        if (type === 'delivery_degraded') {
          setDeliveryState('degraded');
          setDetail(
            'Live transcript updates are delayed. Canonical transcript recovery continues automatically.',
          );
        }
      };

      nextSocket.onerror = () => {
        if (disposed) return;
        setDeliveryState((current) => (current === 'degraded' ? current : 'recovering'));
        setDetail(
          'Live transcript updates are reconnecting. Canonical transcript recovery continues automatically.',
        );
      };

      nextSocket.onclose = () => {
        if (disposed) return;
        setDeliveryState((current) => (current === 'degraded' ? current : 'recovering'));
        setDetail(
          'Live transcript updates are reconnecting. Canonical transcript recovery continues automatically.',
        );
        if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
        reconnectTimer = window.setTimeout(connect, 1_000);
      };
    };

    // Rebuild from sequence zero before relying on realtime hints. A small fallback cursor poll
    // also guarantees convergence while the ephemeral delivery path is unavailable.
    void syncCanonical();
    connect();
    const fallbackTimer = window.setInterval(() => void syncCanonical(), 2_500);

    return () => {
      disposed = true;
      abortController.abort();
      window.clearInterval(fallbackTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [sessionId]);

  return (
    <section
      className="mt-6 rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 sm:p-5"
      data-testid="live-transcript-panel"
    >
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-semibold">Live transcript</p>
          <p className="mt-1 text-xs text-[var(--muted)]">Committed speech appears here in order.</p>
        </div>
        <span
          className="rounded-full border border-[var(--border)] px-3 py-1 text-xs font-semibold uppercase tracking-[0.12em]"
          data-testid="transcript-delivery-state"
        >
          {deliveryLabel(deliveryState)}
        </span>
      </div>

      {!sessionId ? (
        <p className="mt-4 text-sm text-[var(--muted)]" data-testid="transcript-empty">
          Start a recording to see the live transcript.
        </p>
      ) : segments.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--muted)]" data-testid="transcript-empty">
          Listening for speech. The first committed transcript segment will appear automatically.
        </p>
      ) : (
        <ol className="mt-4 space-y-3" aria-live="polite" data-testid="transcript-segments">
          {segments.map((segment) => (
            <li
              key={segment.sequence}
              className="grid gap-1 sm:grid-cols-[7rem_1fr] sm:gap-3"
              data-testid={`transcript-segment-${segment.sequence}`}
            >
              <span className="font-mono text-xs tabular-nums text-[var(--muted)]">
                {timestampLabel(segment.start_ms)}
              </span>
              <span className="text-sm leading-6">{segment.text}</span>
            </li>
          ))}
        </ol>
      )}

      {detail && (
        <p className="mt-4 text-xs leading-5 text-[var(--muted)]" role="status">
          {detail} Recording and saved transcript evidence continue independently.
        </p>
      )}
    </section>
  );
}
