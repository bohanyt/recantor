import type { TranscriptSegmentResponse } from '../api/generated/types.gen';

function timestampLabel(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

export function TranscriptLog({ segments }: { segments: readonly TranscriptSegmentResponse[] }) {
  return (
    <div
      className="mt-4 space-y-3"
      role="log"
      aria-label="Live transcript updates"
      aria-relevant="additions"
      aria-atomic="false"
      data-testid="transcript-segments"
    >
      {segments.map((segment) => (
        <div
          key={segment.sequence}
          role="article"
          className="grid gap-1 sm:grid-cols-[7rem_1fr] sm:gap-3"
          data-testid={`transcript-segment-${segment.sequence}`}
        >
          <span className="font-mono text-xs tabular-nums text-[var(--muted)]">
            {timestampLabel(segment.start_ms)}
          </span>
          <span className="text-sm leading-6">{segment.text}</span>
        </div>
      ))}
    </div>
  );
}
