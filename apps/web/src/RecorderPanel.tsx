import { type ReactNode, useEffect, useMemo, useSyncExternalStore } from 'react';

import {
  deriveRecorderUx,
  type ProductStatus,
  type RecorderActionId,
} from './recording/recorderUx';
import { FencedRecorderController, type FencedRecorderSnapshot } from './recorder/fencedController';
import { TranscriptPanel } from './transcript/TranscriptPanel';

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

const toneClass: Record<ProductStatus['tone'], string> = {
  neutral: 'border-[var(--border)] bg-[var(--surface-muted)]',
  positive: 'border-[var(--positive-border)] bg-[var(--positive-surface)]',
  warning: 'border-[var(--warning-border)] bg-[var(--warning-surface)]',
  danger: 'border-[var(--danger)] bg-[var(--danger-surface)]',
};

function ProductStatusCard({
  title,
  status,
  testId,
}: {
  title: string;
  status: ProductStatus;
  testId: string;
}) {
  return (
    <section
      className={`min-w-0 rounded-2xl border p-4 ${toneClass[status.tone]}`}
      role={status.tone === 'danger' ? 'alert' : undefined}
      data-testid={testId}
    >
      <p className="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--muted)]">
        {title}
      </p>
      <p className="mt-1 text-base font-semibold">{status.label}</p>
      <p className="mt-1 text-sm leading-6 text-[var(--muted)]">{status.detail}</p>
    </section>
  );
}

type RecorderPanelProps = {
  serviceDiagnostics?: ReactNode;
  onCaptureActivityChange?: (active: boolean) => void;
};

export function RecorderPanel({ serviceDiagnostics, onCaptureActivityChange }: RecorderPanelProps) {
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

  useEffect(() => {
    onCaptureActivityChange?.(snapshot.phase === 'requesting' || snapshot.phase === 'recording');
  }, [onCaptureActivityChange, snapshot.phase]);

  const ux = deriveRecorderUx(snapshot);

  const runAction = (action: RecorderActionId): void => {
    if (action === 'start') void controller.start();
    if (action === 'stop') void controller.stop();
    if (action === 'defer-finalization') controller.deferFinalization();
    if (action === 'resume') void controller.resume();
    if (action === 'finish-recovered') void controller.finishRecovered();
    if (action === 'declare-missing-gaps') void controller.declareMissingSequencesAsGaps();
    if (action === 'sync') void controller.syncNow();
  };

  const actionLabel = (action: RecorderActionId): string => {
    if (action === 'start') return 'Start recording';
    if (action === 'stop') return 'Stop recording';
    if (action === 'defer-finalization') return 'Keep locally and finish later';
    if (action === 'resume') return 'Resume recording';
    if (action === 'finish-recovered') {
      return snapshot.missingSequences.length > 0
        ? 'Retry missing audio'
        : 'Finish recovered audio';
    }
    if (action === 'declare-missing-gaps') return 'Declare missing audio as lost & finish';
    return 'Sync now';
  };

  const actionTestId = (action: RecorderActionId): string => {
    if (action === 'start') return 'start-recording';
    if (action === 'stop') return 'stop-recording';
    if (action === 'defer-finalization') return 'defer-finalization';
    if (action === 'resume') return 'resume-recording';
    if (action === 'finish-recovered') return 'finish-recovered';
    if (action === 'declare-missing-gaps') return 'declare-missing-gaps';
    return 'sync-recording';
  };

  const actionButton = (action: RecorderActionId, priority: 'primary' | 'secondary' | 'danger') => (
    <button
      key={action}
      type="button"
      onClick={() => runAction(action)}
      className={
        priority === 'primary'
          ? 'min-h-11 rounded-full bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-white shadow-sm'
          : priority === 'danger'
            ? 'min-h-11 rounded-full border border-[var(--danger)] px-5 py-2.5 text-sm font-semibold text-[var(--danger)]'
            : 'min-h-11 rounded-full border border-[var(--border)] bg-[var(--surface)] px-5 py-2.5 text-sm font-semibold'
      }
      data-testid={actionTestId(action)}
    >
      {actionLabel(action)}
    </button>
  );

  return (
    <section aria-labelledby="live-recording-heading" className="mt-6 min-w-0">
      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        <div className="min-w-0 rounded-[2rem] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-6">
          <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div
              className="min-w-0"
              role="status"
              aria-live="polite"
              aria-atomic="true"
              data-testid="recording-lifecycle-status"
            >
              <p className="text-sm font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
                Live recording
              </p>
              <h2
                id="live-recording-heading"
                className="mt-2 text-2xl font-semibold capitalize tracking-tight"
              >
                {ux.lifecycle.label}
              </h2>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted)]">
                {ux.lifecycle.detail}
              </p>
            </div>
            <div className="shrink-0 rounded-2xl border border-[var(--border)] px-4 py-3 text-left sm:text-right">
              <p
                className="font-mono text-2xl font-semibold tabular-nums"
                data-testid="recording-elapsed"
                aria-label={`Elapsed recording time ${durationLabel(snapshot.elapsedMs)}`}
              >
                {durationLabel(snapshot.elapsedMs)}
              </p>
              <p className="mt-1 text-xs uppercase tracking-[0.14em] text-[var(--muted)]">
                Elapsed
              </p>
            </div>
          </div>

          <div className="mt-5 flex flex-wrap gap-3" aria-label="Recording actions">
            {ux.actions.primary && actionButton(ux.actions.primary, 'primary')}
            {ux.actions.secondary.map((action) => actionButton(action, 'secondary'))}
            {ux.actions.danger.map((action) => actionButton(action, 'danger'))}
          </div>

          <div className="mt-5 grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
            <ProductStatusCard title="Audio safety" status={ux.audio} testId="durability-state" />
            <ProductStatusCard
              title="Transcription"
              status={ux.transcription}
              testId="transcription-state"
            />
          </div>

          {snapshot.captureFenced && (
            <div
              className="mt-4 rounded-2xl border border-[var(--danger)] bg-[var(--danger-surface)] p-4 text-sm"
              role="alert"
              data-testid="fenced-warning"
            >
              <p className="font-semibold">This tab no longer owns the recording session.</p>
              <p className="mt-2 leading-6 text-[var(--muted)]">
                {snapshot.pendingChunks > 0
                  ? 'Orphaned local evidence is retained here. Start, stop, resume, finish, sync, and loss-declaration actions are withheld because this capture generation is stale.'
                  : 'Unsafe recording and recovery actions are withheld because this capture generation is stale.'}
              </p>
            </div>
          )}

          {snapshot.missingSequences.length > 0 && !snapshot.captureFenced && (
            <div
              className="mt-4 rounded-2xl border border-[var(--danger)] bg-[var(--danger-surface)] p-4 text-sm"
              data-testid="missing-sequences"
              role="alert"
            >
              <p className="font-semibold text-[var(--danger)]">
                Missing audio sequence(s): {missingSequenceLabel(snapshot.missingSequences)}
              </p>
              <p className="mt-2 leading-6 text-[var(--muted)]">
                Retry recovery first if these fragments may still exist locally. Declaring them lost
                creates an explicit, permanent continuity gap; Recantor never does that
                automatically.
              </p>
            </div>
          )}

          {snapshot.localWriteFailed && !snapshot.captureFenced && (
            <div
              className="mt-4 rounded-2xl border border-[var(--danger)] bg-[var(--danger-surface)] p-4 text-sm"
              role="alert"
              data-testid="storage-unsafe-warning"
            >
              <p className="font-semibold">Browser audio storage is unsafe.</p>
              <p className="mt-2 leading-6 text-[var(--muted)]">
                Recording safety is downgraded until retained evidence can be reconciled. Use the
                recovery actions above; transcription status does not change this audio warning.
              </p>
            </div>
          )}

          {snapshot.error && !snapshot.captureFenced && (
            <p
              className="mt-4 rounded-2xl border border-[var(--danger)] bg-[var(--danger-surface)] p-4 text-sm font-medium text-[var(--danger)]"
              role="alert"
            >
              {snapshot.error}
            </p>
          )}

          <details
            className="mt-5 rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)]"
            data-testid="diagnostics"
          >
            <summary className="min-h-11 cursor-pointer rounded-2xl px-4 py-3 text-sm font-semibold">
              Advanced / Diagnostics
            </summary>
            <div className="border-t border-[var(--border)] p-4 text-sm">
              <p className="font-medium" data-testid="recorder-message">
                {recorderMessage(snapshot)}
              </p>
              <dl className="mt-4 grid gap-x-5 gap-y-3 sm:grid-cols-2">
                <Diagnostic label="Lifecycle phase" value={`recorder/${snapshot.phase}`} />
                <Diagnostic label="Session" value={snapshot.sessionId ?? 'none'} testId="session-id" />
                <Diagnostic
                  label="Pending local audio"
                  value={pendingAudioLabel(snapshot)}
                  testId="pending-chunks"
                />
                <Diagnostic
                  label="Server ACK"
                  value={`through sequence ${snapshot.highestAckedSequence}`}
                  testId="acked-sequence"
                />
                <Diagnostic
                  label="Browser storage"
                  value={`${snapshot.storage?.persisted ? 'persistent' : 'best effort'} · estimated free ${bytesLabel(snapshot.storage?.remainingBytes ?? null)}`}
                />
                <Diagnostic
                  label="Capture lock"
                  value={snapshot.lockKind ?? 'not held'}
                  testId="capture-lock-state"
                />
                <Diagnostic
                  label="Network"
                  value={snapshot.online ? 'Online' : 'Offline'}
                  testId="network-state"
                />
                <Diagnostic label="Explicit gaps" value={String(snapshot.gapCount)} />
                <Diagnostic
                  label="Realtime speech lane"
                  value={snapshot.realtimeStatus}
                  testId="realtime-status"
                />
                <Diagnostic
                  label="Durable utterances"
                  value={String(snapshot.realtimeUtterances)}
                  testId="realtime-utterances"
                />
              </dl>
              {snapshot.realtimeError && (
                <p className="mt-4 text-[var(--muted)]" data-testid="realtime-error">
                  Archive recording continues independently. Realtime speech detail:{' '}
                  {snapshot.realtimeError}
                </p>
              )}
              {serviceDiagnostics && <div className="mt-5">{serviceDiagnostics}</div>}
            </div>
          </details>
        </div>

        <div className="min-w-0 xl:max-h-[calc(100vh-9rem)] xl:overflow-y-auto xl:pr-1">
          <TranscriptPanel sessionId={snapshot.sessionId} />
        </div>
      </div>
    </section>
  );
}

function Diagnostic({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--muted)]">
        {label}
      </dt>
      <dd className="mt-1 break-words font-medium" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}
