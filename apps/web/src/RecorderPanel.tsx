import { useEffect, useMemo, useSyncExternalStore } from 'react';

import { FencedRecorderController, type FencedRecorderSnapshot } from './recorder/fencedController';

function durationLabel(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

function bytesLabel(bytes: number | null): string {
  if (bytes === null) return 'unknown';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${Math.round(bytes / (1024 * 1024))} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function missingSequenceLabel(sequences: number[]): string {
  if (sequences.length === 0) return 'none';
  const ordered = [...new Set(sequences)].sort((left, right) => left - right);
  const ranges: string[] = [];
  let start = ordered[0];
  let end = start;
  for (const sequence of ordered.slice(1)) {
    if (sequence === end + 1) {
      end = sequence;
      continue;
    }
    ranges.push(start === end ? String(start) : `${start}–${end}`);
    start = end = sequence;
  }
  ranges.push(start === end ? String(start) : `${start}–${end}`);
  return ranges.join(', ');
}

function durabilityLabel(snapshot: FencedRecorderSnapshot): string {
  if (snapshot.captureFenced && snapshot.pendingChunks > 0) {
    return 'Orphaned local evidence — not safely syncable';
  }
  if (snapshot.captureFenced) return 'Capture fenced — local ownership stale';
  if (snapshot.localWriteFailed) return 'Unsafe — local spool failed';
  if (snapshot.missingSequences.length > 0) return 'Continuity unresolved';
  if (snapshot.phase === 'complete' && snapshot.sessionId) return 'Server synced';
  if (snapshot.pendingChunks > 0 && snapshot.storage?.persisted) return 'Locally recoverable';
  if (snapshot.pendingChunks > 0) return 'Pending locally (best effort)';
  if (snapshot.phase === 'recording' && snapshot.sessionId) return 'Emitted audio synced';
  if (snapshot.phase === 'recoverable' && snapshot.sessionId) {
    return 'Server synced; continuity pending';
  }
  if (snapshot.phase === 'finalizing' && snapshot.sessionId) return 'Finalization pending';
  if (snapshot.sessionId) return 'Session active';
  return 'Ready';
}

function pendingAudioLabel(snapshot: FencedRecorderSnapshot): string {
  const noun = snapshot.pendingChunks === 1 ? 'fragment' : 'fragments';
  return snapshot.captureFenced
    ? `${snapshot.pendingChunks} orphaned ${noun}`
    : `${snapshot.pendingChunks} ${noun}`;
}

export function recorderMessage(snapshot: FencedRecorderSnapshot): string {
  if (
    snapshot.phase === 'complete' &&
    snapshot.highestAckedSequence === 0 &&
    snapshot.gapCount === 0
  ) {
    return 'Recording finalized with no durable audio captured.';
  }
  return snapshot.message;
}

export function RecorderPanel() {
  const controller = useMemo(() => new FencedRecorderController(), []);
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );

  useEffect(() => {
    void controller.initialize();
    return () => controller.dispose();
  }, [controller]);

  const canStart =
    !snapshot.captureFenced &&
    (snapshot.phase === 'idle' || snapshot.phase === 'complete' || snapshot.phase === 'error');
  const recoverable = snapshot.phase === 'recoverable' && !snapshot.captureFenced;
  const hasMissingSequences = snapshot.missingSequences.length > 0;
  const busy = snapshot.phase === 'requesting' || snapshot.phase === 'finalizing';

  return (
    <section className="mt-10 rounded-[2rem] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-7">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
            Reliable recorder
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">
            Capture first, intelligence later.
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted)]">
            Unacknowledged emitted audio is spooled in this browser, sequenced, and deleted locally
            only after the server returns a durable ACK. The currently open MediaRecorder fragment
            is not locally durable until the browser emits it.
          </p>
        </div>
        <div className="rounded-2xl border border-[var(--border)] px-4 py-3 text-right">
          <p
            className="font-mono text-2xl font-semibold tabular-nums"
            data-testid="recording-elapsed"
          >
            {durationLabel(snapshot.elapsedMs)}
          </p>
          <p className="mt-1 text-xs uppercase tracking-[0.14em] text-[var(--muted)]">
            {snapshot.phase}
          </p>
        </div>
      </div>

      <div className="mt-6 flex flex-wrap gap-3">
        {canStart && (
          <button
            type="button"
            onClick={() => void controller.start()}
            className="rounded-full bg-[var(--accent)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-50"
            data-testid="start-recording"
          >
            Start recording
          </button>
        )}
        {snapshot.phase === 'recording' && !snapshot.captureFenced && (
          <button
            type="button"
            onClick={() => void controller.stop()}
            className="rounded-full border border-[var(--border)] px-5 py-3 text-sm font-semibold"
            data-testid="stop-recording"
          >
            Stop
          </button>
        )}
        {snapshot.phase === 'finalizing' && !snapshot.captureFenced && (
          <button
            type="button"
            onClick={() => controller.deferFinalization()}
            className="rounded-full border border-[var(--border)] px-5 py-3 text-sm font-semibold"
            data-testid="defer-finalization"
          >
            Keep locally and finish later
          </button>
        )}
        {recoverable && !hasMissingSequences && (
          <button
            type="button"
            onClick={() => void controller.resume()}
            className="rounded-full bg-[var(--accent)] px-5 py-3 text-sm font-semibold text-white"
            data-testid="resume-recording"
          >
            Resume recording
          </button>
        )}
        {recoverable && (
          <button
            type="button"
            onClick={() => void controller.finishRecovered()}
            className="rounded-full border border-[var(--border)] px-5 py-3 text-sm font-semibold"
            data-testid="finish-recovered"
          >
            {hasMissingSequences ? 'Retry missing audio' : 'Finish recovered audio'}
          </button>
        )}
        {recoverable && hasMissingSequences && (
          <button
            type="button"
            onClick={() => void controller.declareMissingSequencesAsGaps()}
            className="rounded-full border border-[var(--danger)] px-5 py-3 text-sm font-semibold text-[var(--danger)]"
            data-testid="declare-missing-gaps"
          >
            Declare missing audio as lost & finish
          </button>
        )}
        {snapshot.sessionId && snapshot.pendingChunks > 0 && !snapshot.captureFenced && (
          <button
            type="button"
            onClick={() => void controller.syncNow()}
            disabled={busy}
            className="rounded-full border border-[var(--border)] px-5 py-3 text-sm font-semibold disabled:opacity-50"
            data-testid="sync-recording"
          >
            Sync now
          </button>
        )}
      </div>

      {hasMissingSequences && !snapshot.captureFenced && (
        <div
          className="mt-5 rounded-2xl border border-[var(--danger)] p-4 text-sm"
          data-testid="missing-sequences"
        >
          <p className="font-semibold text-[var(--danger)]">
            Unaccounted audio sequence(s): {missingSequenceLabel(snapshot.missingSequences)}
          </p>
          <p className="mt-2 leading-6 text-[var(--muted)]">
            Retry recovery first if these fragments may still exist locally. Declaring them lost is
            an explicit, permanent continuity gap for this recording; Recantor will not do it
            automatically.
          </p>
        </div>
      )}

      <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Durability" value={durabilityLabel(snapshot)} testId="durability-state" />
        <Metric
          label="Pending local audio"
          value={pendingAudioLabel(snapshot)}
          testId="pending-chunks"
        />
        <Metric
          label="Server ACK"
          value={`through sequence ${snapshot.highestAckedSequence}`}
          testId="acked-sequence"
        />
        <Metric
          label="Network"
          value={snapshot.online ? 'Online' : 'Offline'}
          testId="network-state"
        />
      </div>

      <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 text-sm">
        <p className="font-medium" data-testid="recorder-message">
          {recorderMessage(snapshot)}
        </p>
        <p className="mt-2 text-[var(--muted)]">
          Browser storage: {snapshot.storage?.persisted ? 'persistent' : 'best effort'} · estimated
          free {bytesLabel(snapshot.storage?.remainingBytes ?? null)} · capture lock{' '}
          <span data-testid="capture-lock-state">{snapshot.lockKind ?? 'not held'}</span> · explicit
          gaps {snapshot.gapCount}
        </p>
        {snapshot.error && (
          <p className="mt-2 font-medium text-[var(--danger)]" role="alert">
            {snapshot.error}
          </p>
        )}
      </div>
    </section>
  );
}

function Metric({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="rounded-2xl border border-[var(--border)] p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--muted)]">
        {label}
      </p>
      <p className="mt-2 text-sm font-semibold" data-testid={testId}>
        {value}
      </p>
    </div>
  );
}
