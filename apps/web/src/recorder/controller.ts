import type {
  FinalizeSessionRequest,
  GapDeclarationRequest,
  RecordingStateResponse,
} from '../api/generated/types.gen';
import {
  claimCapture,
  createLiveSession,
  declareRecordingGap,
  fetchRecordingState,
  finalizeRecording,
  heartbeatCapture,
  RecordingApiError,
  uploadRecordingChunk,
} from './api';
import {
  appendCapturedChunk,
  chunkKey,
  commitStartedSession,
  countSpoolChunks,
  deleteLocalSession,
  deleteSpoolChunk,
  getLatestLocalSession,
  getOrCreatePendingStart,
  listSpoolChunks,
  putLocalSession,
  type LocalRecordingSession,
  type SpoolChunk,
} from './db';
import { sha256Blob } from './hash';
import { reconcileLocalSpool } from './reconcile';
import { inspectStorageSafety, type StorageSafety } from './storageSafety';
import { acquireCaptureTabLock, type CaptureTabLock } from './tabCoordinator';

export type RecorderPhase =
  'idle' | 'requesting' | 'recording' | 'recoverable' | 'finalizing' | 'complete' | 'error';

export type RecorderSnapshot = {
  phase: RecorderPhase;
  sessionId: string | null;
  pendingChunks: number;
  highestAckedSequence: number;
  elapsedMs: number;
  online: boolean;
  storage: StorageSafety | null;
  localWriteFailed: boolean;
  gapCount: number;
  lockKind: CaptureTabLock['kind'] | null;
  message: string;
  error: string | null;
};

type Listener = () => void;

const CHUNK_TIMESLICE_MS = 2_000;
const HEARTBEAT_MS = 5_000;
const STORAGE_REFRESH_MS = 15_000;
const MAX_UPLOAD_ATTEMPTS = 4;

function defaultSnapshot(): RecorderSnapshot {
  return {
    phase: 'idle',
    sessionId: null,
    pendingChunks: 0,
    highestAckedSequence: 0,
    elapsedMs: 0,
    online: navigator.onLine,
    storage: null,
    localWriteFailed: false,
    gapCount: 0,
    lockKind: null,
    message: 'Ready to record.',
    error: null,
  };
}

function chooseMimeType(): string {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
  return candidates.find((candidate) => MediaRecorder.isTypeSupported(candidate)) ?? '';
}

function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function isRetryableUpload(error: unknown): boolean {
  if (!(error instanceof RecordingApiError)) return false;
  return (
    error.status === null || error.status === 408 || error.status === 429 || error.status >= 500
  );
}

export class RecorderController {
  private snapshot = defaultSnapshot();
  private readonly listeners = new Set<Listener>();
  private localSession: LocalRecordingSession | null = null;
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private captureLock: CaptureTabLock | null = null;
  private captureStartedPerformance = 0;
  private captureBaseMonotonicMs = 0;
  private chunkChain: Promise<void> = Promise.resolve();
  private syncPromise: Promise<void> | null = null;
  private spoolFailurePromise: Promise<void> | null = null;
  private captureInterruptionPromise: Promise<void> | null = null;
  private heartbeatTimer: number | null = null;
  private clockTimer: number | null = null;
  private storageTimer: number | null = null;
  private initialized = false;
  private lifecycleVersion = 0;
  private captureFaulted = false;

  readonly subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getSnapshot = (): RecorderSnapshot => this.snapshot;

  private patch(update: Partial<RecorderSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...update };
    for (const listener of this.listeners) listener();
  }

  private lifecycleCurrent(version: number): boolean {
    return this.initialized && this.lifecycleVersion === version;
  }

  private readonly handleOnline = (): void => {
    this.patch({ online: true, message: 'Network restored. Reconciling pending audio…' });
    void this.syncNow();
  };

  private readonly handleOffline = (): void => {
    this.patch({
      online: false,
      message: 'Offline. Unacknowledged audio remains in browser storage.',
    });
  };

  async initialize(): Promise<void> {
    if (this.initialized) return;
    this.initialized = true;
    const lifecycleVersion = ++this.lifecycleVersion;
    window.addEventListener('online', this.handleOnline);
    window.addEventListener('offline', this.handleOffline);
    await this.refreshStorage(false);
    if (!this.lifecycleCurrent(lifecycleVersion)) return;

    const local = await getLatestLocalSession();
    if (!this.lifecycleCurrent(lifecycleVersion) || !local) return;

    this.localSession = local;
    try {
      const state = await fetchRecordingState(local.sessionId);
      if (!this.lifecycleCurrent(lifecycleVersion)) return;
      await reconcileLocalSpool(local.sessionId, state);
      if (!this.lifecycleCurrent(lifecycleVersion)) return;
      if (state.session.state === 'complete') {
        await deleteLocalSession(local.sessionId);
        if (!this.lifecycleCurrent(lifecycleVersion)) return;
        this.localSession = null;
        this.patch({ phase: 'idle', message: 'Previous recording was already complete.' });
        return;
      }
      const pendingChunks = await countSpoolChunks(local.sessionId);
      if (!this.lifecycleCurrent(lifecycleVersion)) return;
      const legacy = !local.recoveryToken;
      this.patch({
        phase: 'recoverable',
        sessionId: local.sessionId,
        pendingChunks,
        highestAckedSequence: state.highest_contiguous_sequence,
        gapCount: state.gaps.length,
        elapsedMs: Math.max(0, Date.now() - local.recordingStartedWallMs),
        message: legacy
          ? 'Legacy recovery found. Existing audio can be finished, but a new capture generation cannot be started.'
          : 'Interrupted recording found. Resume it or finish the recovered audio.',
      });
    } catch (error) {
      if (!this.lifecycleCurrent(lifecycleVersion)) return;
      const pendingChunks = await countSpoolChunks(local.sessionId);
      if (!this.lifecycleCurrent(lifecycleVersion)) return;
      this.patch({
        phase: 'recoverable',
        sessionId: local.sessionId,
        pendingChunks,
        elapsedMs: Math.max(0, Date.now() - local.recordingStartedWallMs),
        message: 'Local recovery audio found. Server reconciliation is waiting for connectivity.',
        error: error instanceof Error ? error.message : 'Failed to reconcile recovery state.',
      });
    }
  }

  dispose(): void {
    window.removeEventListener('online', this.handleOnline);
    window.removeEventListener('offline', this.handleOffline);
    this.initialized = false;
    this.lifecycleVersion += 1;
    if (!this.recorder) this.stopTimers();
  }

  async start(): Promise<void> {
    if (
      this.snapshot.phase !== 'idle' &&
      this.snapshot.phase !== 'complete' &&
      this.snapshot.phase !== 'error'
    )
      return;
    this.captureFaulted = false;
    this.patch({
      phase: 'requesting',
      error: null,
      localWriteFailed: false,
      message: 'Requesting microphone access…',
    });
    await this.refreshStorage(true);

    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const pending = await getOrCreatePendingStart();
      const remote = await createLiveSession({
        client_request_id: pending.clientRequestId,
        writer_id: pending.writerId,
        recovery_token: pending.recoveryToken,
      });
      const mimeType = chooseMimeType();
      const now = Date.now();
      const local: LocalRecordingSession = {
        sessionId: remote.id,
        clientRequestId: pending.clientRequestId,
        writerId: pending.writerId,
        captureEpoch: remote.capture_epoch,
        recoveryToken: pending.recoveryToken,
        pendingWriterId: null,
        state: 'recording',
        mimeType,
        lastSequence: 0,
        lastMonotonicEndMs: 0,
        lastWallEndMs: null,
        recordingStartedWallMs: now,
        createdAt: now,
        updatedAt: now,
      };
      await commitStartedSession(pending, local);
      this.localSession = local;
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab already owns this recording session.');
      this.patch({ lockKind: this.captureLock.kind });
      this.beginMediaRecorder(stream, local);
      stream = null;
    } catch (error) {
      stopStream(stream);
      this.releaseCaptureLock();
      const local = this.localSession;
      if (local) {
        const interrupted: LocalRecordingSession = {
          ...local,
          state: 'interrupted',
          updatedAt: Date.now(),
        };
        try {
          await putLocalSession(interrupted);
          this.localSession = interrupted;
        } catch {
          // The original local session may still be recoverable even when this update fails.
        }
        this.patch({
          phase: 'recoverable',
          sessionId: local.sessionId,
          error: error instanceof Error ? error.message : 'Failed to start recording.',
          message: 'Capture did not start cleanly; recovery state was kept.',
        });
        return;
      }
      this.patch({
        phase: 'error',
        error: error instanceof Error ? error.message : 'Failed to start recording.',
        message: 'Recording did not start. The persisted start identity will be reused on retry.',
      });
    }
  }

  async resume(): Promise<void> {
    const local = this.localSession ?? (await getLatestLocalSession());
    if (!local || this.snapshot.phase !== 'recoverable') return;
    if (!local.recoveryToken) {
      this.patch({
        error: 'This legacy recovery session has no capture recovery capability.',
        message: 'Finish the recovered audio instead of starting a new capture generation.',
      });
      return;
    }
    this.patch({
      phase: 'requesting',
      error: null,
      message: 'Reconciling old recovery audio before claiming a new capture generation…',
    });
    await this.refreshStorage(true);

    let stream: MediaStream | null = null;
    try {
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab is already resuming this recording.');
      this.patch({ lockKind: this.captureLock.kind });

      const state = await fetchRecordingState(local.sessionId);
      await reconcileLocalSpool(local.sessionId, state);
      await this.syncNow();
      const pending = await countSpoolChunks(local.sessionId);
      if (pending > 0) {
        throw new Error(
          `${pending} recovery audio fragment(s) must receive a durable ACK before a new capture generation can start.`,
        );
      }

      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const nextWriterId = local.pendingWriterId ?? crypto.randomUUID();
      const claimIntent: LocalRecordingSession = local.pendingWriterId
        ? local
        : { ...local, pendingWriterId: nextWriterId, updatedAt: Date.now() };
      if (!local.pendingWriterId) {
        await putLocalSession(claimIntent);
        this.localSession = claimIntent;
      }

      const remote = await claimCapture(local.sessionId, {
        writer_id: nextWriterId,
        expected_epoch: local.captureEpoch,
        recovery_token: local.recoveryToken,
      });
      const resumed: LocalRecordingSession = {
        ...claimIntent,
        writerId: nextWriterId,
        captureEpoch: remote.capture_epoch,
        pendingWriterId: null,
        state: 'recording',
        updatedAt: Date.now(),
      };
      await putLocalSession(resumed);
      this.localSession = resumed;

      await this.recordRecoveryGap(resumed);
      this.captureFaulted = false;
      this.patch({ localWriteFailed: false });
      this.beginMediaRecorder(stream, resumed);
      stream = null;
    } catch (error) {
      stopStream(stream);
      this.releaseCaptureLock();
      this.patch({
        phase: 'recoverable',
        error: error instanceof Error ? error.message : 'Failed to resume recording.',
        message: 'Recovery is still available; a new capture generation was not started.',
      });
    }
  }

  async syncNow(): Promise<void> {
    if (!this.localSession) return;
    if (this.syncPromise) return this.syncPromise;
    this.syncPromise = this.syncPending().finally(() => {
      this.syncPromise = null;
    });
    return this.syncPromise;
  }

  async stop(): Promise<void> {
    const local = this.localSession;
    if (!local || this.snapshot.phase !== 'recording') return;
    this.patch({
      phase: 'finalizing',
      message: 'Stopping capture and flushing the final audio fragment…',
    });
    this.stopTimers();

    try {
      await this.stopMediaRecorder();
      await this.chunkChain;
      await this.syncNow();
      const current = this.localSession ?? local;
      const pending = await countSpoolChunks(current.sessionId);
      if (pending > 0) {
        const interrupted: LocalRecordingSession = {
          ...current,
          state: 'interrupted',
          updatedAt: Date.now(),
        };
        await putLocalSession(interrupted);
        this.localSession = interrupted;
        this.releaseCaptureLock();
        this.patch({
          phase: 'recoverable',
          pendingChunks: pending,
          message:
            'Capture stopped, but some audio still needs a server ACK. Finish recovery later.',
        });
        return;
      }
      await this.finalizeLocalSession(current);
    } catch (error) {
      const current = this.localSession ?? local;
      const interrupted: LocalRecordingSession = {
        ...current,
        state: 'interrupted',
        updatedAt: Date.now(),
      };
      try {
        await putLocalSession(interrupted);
        this.localSession = interrupted;
      } catch {
        // Keep the in-memory recovery state and surface the failure instead of claiming completion.
      }
      this.releaseCaptureLock();
      this.patch({
        phase: 'recoverable',
        error: error instanceof Error ? error.message : 'Failed to finalize recording.',
        message: 'Capture stopped. Local recovery state was kept instead of claiming completion.',
      });
    }
  }

  async finishRecovered(): Promise<void> {
    const local = this.localSession;
    if (!local || this.snapshot.phase !== 'recoverable') return;
    this.patch({
      phase: 'finalizing',
      error: null,
      message: 'Reconciling recovered audio before finalization…',
    });
    try {
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab is already handling this recording.');
      this.patch({ lockKind: this.captureLock.kind });

      let state = await fetchRecordingState(local.sessionId);
      await reconcileLocalSpool(local.sessionId, state);
      await this.syncNow();
      const pending = await countSpoolChunks(local.sessionId);
      if (pending > 0) {
        throw new Error(`${pending} local audio fragment(s) still lack a durable ACK.`);
      }
      state = await fetchRecordingState(local.sessionId);

      let finalizingOwner = this.localSession ?? local;
      if (finalizingOwner.pendingWriterId) {
        const pendingWriterId = finalizingOwner.pendingWriterId;
        if (
          state.session.capture_epoch === finalizingOwner.captureEpoch + 1 &&
          state.session.active_writer_id === pendingWriterId
        ) {
          if (!finalizingOwner.recoveryToken) {
            throw new Error('Cannot recover an already-claimed generation without its capability.');
          }
          const remote = await claimCapture(finalizingOwner.sessionId, {
            writer_id: pendingWriterId,
            expected_epoch: finalizingOwner.captureEpoch,
            recovery_token: finalizingOwner.recoveryToken,
          });
          finalizingOwner = {
            ...finalizingOwner,
            writerId: pendingWriterId,
            captureEpoch: remote.capture_epoch,
            pendingWriterId: null,
            updatedAt: Date.now(),
          };
        } else if (
          state.session.capture_epoch === finalizingOwner.captureEpoch &&
          state.session.active_writer_id === finalizingOwner.writerId
        ) {
          finalizingOwner = {
            ...finalizingOwner,
            pendingWriterId: null,
            updatedAt: Date.now(),
          };
        } else {
          throw new Error('Capture ownership changed while recovery finalization was pending.');
        }
        await putLocalSession(finalizingOwner);
        this.localSession = finalizingOwner;
      }

      await this.recordRecoveryGap(finalizingOwner);
      await this.finalizeLocalSession(finalizingOwner);
    } catch (error) {
      this.releaseCaptureLock();
      this.patch({
        phase: 'recoverable',
        error: error instanceof Error ? error.message : 'Failed to finish recovered recording.',
        message: 'Recovered audio remains available locally.',
      });
    }
  }

  private beginMediaRecorder(stream: MediaStream, local: LocalRecordingSession): void {
    const recorder = local.mimeType
      ? new MediaRecorder(stream, { mimeType: local.mimeType })
      : new MediaRecorder(stream);
    this.stream = stream;
    this.recorder = recorder;
    this.captureFaulted = false;
    this.chunkChain = Promise.resolve();
    this.captureStartedPerformance = performance.now();
    this.captureBaseMonotonicMs = local.lastMonotonicEndMs;

    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size === 0 || this.captureFaulted) return;
      const capturedPerformance = performance.now();
      const wallEndMs = Date.now();
      this.chunkChain = this.chunkChain
        .then(() => this.persistCapturedBlob(event.data, capturedPerformance, wallEndMs))
        .catch((error) => {
          this.captureFaulted = true;
          void this.failCaptureBecauseSpoolIsUnsafe(error);
        });
    });
    recorder.addEventListener('error', () => {
      if (this.snapshot.phase !== 'recording') return;
      void this.failCaptureBecauseMediaInterrupted('MediaRecorder reported a capture error.');
    });
    for (const track of stream.getAudioTracks()) {
      track.addEventListener('ended', () => {
        if (this.snapshot.phase !== 'recording') return;
        void this.failCaptureBecauseMediaInterrupted('The microphone track ended unexpectedly.');
      });
    }

    recorder.start(CHUNK_TIMESLICE_MS);
    this.startTimers();
    this.patch({
      phase: 'recording',
      sessionId: local.sessionId,
      error: null,
      message: `Recording capture generation ${local.captureEpoch}. Emitted audio is kept locally until the server durably acknowledges it.`,
      elapsedMs: Math.max(0, Date.now() - local.recordingStartedWallMs),
    });
  }

  private async persistCapturedBlob(
    blob: Blob,
    capturedPerformance: number,
    wallEndMs: number,
  ): Promise<void> {
    const local = this.localSession;
    if (!local) throw new Error('Missing local recording session.');
    const monotonicStartMs = local.lastMonotonicEndMs;
    const measuredEnd = Math.round(
      this.captureBaseMonotonicMs + (capturedPerformance - this.captureStartedPerformance),
    );
    const monotonicEndMs = Math.max(monotonicStartMs + 1, measuredEnd);
    const sequence = local.lastSequence + 1;
    const contentType = blob.type || local.mimeType || 'application/octet-stream';
    const chunk: SpoolChunk = {
      key: chunkKey(local.sessionId, sequence),
      sessionId: local.sessionId,
      writerId: local.writerId,
      captureEpoch: local.captureEpoch,
      sequence,
      monotonicStartMs,
      monotonicEndMs,
      wallEndMs,
      contentType,
      sha256: await sha256Blob(blob),
      byteLength: blob.size,
      blob,
      createdAt: Date.now(),
    };
    const advanced: LocalRecordingSession = {
      ...local,
      lastSequence: sequence,
      lastMonotonicEndMs: monotonicEndMs,
      lastWallEndMs: wallEndMs,
      updatedAt: Date.now(),
    };

    await appendCapturedChunk(chunk, advanced);
    this.localSession = advanced;
    this.patch({ pendingChunks: await countSpoolChunks(local.sessionId) });
    void this.syncNow();
  }

  private async failCaptureBecauseSpoolIsUnsafe(error: unknown): Promise<void> {
    if (this.spoolFailurePromise) return this.spoolFailurePromise;
    this.spoolFailurePromise = (async () => {
      this.captureFaulted = true;
      this.stopTimers();
      try {
        await this.stopMediaRecorder();
      } catch {
        stopStream(this.stream);
        this.stream = null;
        this.recorder = null;
      }

      const local = this.localSession;
      if (local) {
        const interrupted: LocalRecordingSession = {
          ...local,
          state: 'interrupted',
          updatedAt: Date.now(),
        };
        try {
          await putLocalSession(interrupted);
          this.localSession = interrupted;
        } catch {
          // If IndexedDB itself is unavailable, the UI must remain explicitly unsafe.
        }
      }
      this.releaseCaptureLock();

      let pendingChunks = this.snapshot.pendingChunks;
      if (local) {
        try {
          pendingChunks = await countSpoolChunks(local.sessionId);
        } catch {
          // Preserve the last known count when IndexedDB cannot be read.
        }
      }
      this.patch({
        phase: 'recoverable',
        pendingChunks,
        localWriteFailed: true,
        error: error instanceof Error ? error.message : 'Browser recovery spool write failed.',
        message:
          'Capture stopped because local recovery storage became unsafe. The uncertain interval must be preserved as an explicit gap before completion.',
      });
    })().finally(() => {
      this.spoolFailurePromise = null;
    });
    return this.spoolFailurePromise;
  }

  private async failCaptureBecauseMediaInterrupted(message: string): Promise<void> {
    if (this.captureInterruptionPromise) return this.captureInterruptionPromise;
    this.captureInterruptionPromise = (async () => {
      this.captureFaulted = true;
      this.stopTimers();
      try {
        await this.stopMediaRecorder();
      } catch {
        stopStream(this.stream);
        this.stream = null;
        this.recorder = null;
      }

      const local = this.localSession;
      if (local) {
        const interrupted: LocalRecordingSession = {
          ...local,
          state: 'interrupted',
          updatedAt: Date.now(),
        };
        await putLocalSession(interrupted);
        this.localSession = interrupted;
      }
      this.releaseCaptureLock();
      this.patch({
        phase: 'recoverable',
        pendingChunks: local ? await countSpoolChunks(local.sessionId) : 0,
        error: message,
        message:
          'Capture stopped after a browser or microphone interruption. Already-emitted audio remains recoverable; the uncertain tail will be represented as a gap.',
      });
    })().finally(() => {
      this.captureInterruptionPromise = null;
    });
    return this.captureInterruptionPromise;
  }

  private async syncPending(): Promise<void> {
    const local = this.localSession;
    if (!local) return;
    if (!navigator.onLine) {
      this.patch({ online: false });
      return;
    }

    try {
      const state = await fetchRecordingState(local.sessionId);
      const pendingAfterReconcile = await reconcileLocalSpool(local.sessionId, state);
      this.patch({
        highestAckedSequence: state.highest_contiguous_sequence,
        pendingChunks: pendingAfterReconcile.length,
        gapCount: state.gaps.length,
      });

      for (const chunk of pendingAfterReconcile) {
        const ack = await this.uploadWithRetry(chunk);
        await deleteSpoolChunk(local.sessionId, chunk.sequence);
        this.patch({
          highestAckedSequence: Math.max(this.snapshot.highestAckedSequence, ack.sequence),
          pendingChunks: await countSpoolChunks(local.sessionId),
          online: true,
          message:
            this.snapshot.phase === 'recording'
              ? 'Recording — emitted audio is server-synced.'
              : this.snapshot.message,
        });
      }
    } catch (error) {
      this.patch({
        pendingChunks: await countSpoolChunks(local.sessionId),
        online: navigator.onLine,
        error: error instanceof Error ? error.message : 'Audio synchronization failed.',
        message: 'Server sync is delayed. Persisted browser audio is retained for retry.',
      });
    }
  }

  private async uploadWithRetry(chunk: SpoolChunk) {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < MAX_UPLOAD_ATTEMPTS; attempt += 1) {
      try {
        return await uploadRecordingChunk(chunk);
      } catch (error) {
        lastError = error;
        if (!isRetryableUpload(error) || attempt === MAX_UPLOAD_ATTEMPTS - 1) throw error;
        await sleep(250 * 2 ** attempt);
      }
    }
    throw lastError;
  }

  private async recordRecoveryGap(local: LocalRecordingSession): Promise<void> {
    const gapStartedWallMs = local.lastWallEndMs ?? local.recordingStartedWallMs;
    const wallEndMs = Date.now();
    if (wallEndMs - gapStartedWallMs < 1_000) return;
    const body: GapDeclarationRequest = {
      client_gap_id: crypto.randomUUID(),
      writer_id: local.writerId,
      capture_epoch: local.captureEpoch,
      wall_started_at: new Date(gapStartedWallMs).toISOString(),
      wall_ended_at: new Date(wallEndMs).toISOString(),
      reason: this.snapshot.localWriteFailed
        ? 'browser_local_spool_failure'
        : 'browser_interruption_before_recovery',
    };
    await declareRecordingGap(local.sessionId, body);
    const state = await fetchRecordingState(local.sessionId);
    this.patch({ gapCount: state.gaps.length, localWriteFailed: false });
  }

  private async finalizeLocalSession(local: LocalRecordingSession): Promise<void> {
    const finalizing: LocalRecordingSession = {
      ...local,
      state: 'finalizing',
      updatedAt: Date.now(),
    };
    await putLocalSession(finalizing);
    this.localSession = finalizing;
    const body: FinalizeSessionRequest = {
      writer_id: finalizing.writerId,
      capture_epoch: finalizing.captureEpoch,
      final_sequence: finalizing.lastSequence,
      final_monotonic_end_ms: finalizing.lastMonotonicEndMs,
      gap_sequences: [],
    };
    const final = await finalizeRecording(finalizing.sessionId, body);
    if (!final.complete) {
      throw new Error(`Server still expects sequence(s): ${final.missing_sequences.join(', ')}`);
    }
    await deleteLocalSession(finalizing.sessionId);
    this.localSession = null;
    this.releaseCaptureLock();
    this.patch({
      phase: 'complete',
      pendingChunks: 0,
      gapCount: final.gaps.length,
      localWriteFailed: false,
      message: final.gaps.length
        ? 'Recording finalized. Explicit interruption evidence is attached.'
        : 'Recording finalized with every expected audio sequence durably acknowledged.',
      error: null,
    });
  }

  private async stopMediaRecorder(): Promise<void> {
    const recorder = this.recorder;
    if (!recorder) return;
    if (recorder.state !== 'inactive') {
      await new Promise<void>((resolve) => {
        recorder.addEventListener('stop', () => resolve(), { once: true });
        recorder.stop();
      });
    }
    stopStream(this.stream);
    this.stream = null;
    this.recorder = null;
  }

  private startTimers(): void {
    this.stopTimers();
    this.clockTimer = window.setInterval(() => {
      if (!this.localSession) return;
      this.patch({ elapsedMs: Math.max(0, Date.now() - this.localSession.recordingStartedWallMs) });
    }, 1_000);
    this.heartbeatTimer = window.setInterval(() => {
      const local = this.localSession;
      if (!local || this.snapshot.phase !== 'recording') return;
      void heartbeatCapture(local.sessionId, local.writerId, local.captureEpoch).catch((error) => {
        this.patch({
          error: error instanceof Error ? error.message : 'Heartbeat failed.',
          message: 'Session heartbeat is delayed; capture continues into the recovery spool.',
        });
      });
    }, HEARTBEAT_MS);
    this.storageTimer = window.setInterval(
      () => void this.refreshStorage(false),
      STORAGE_REFRESH_MS,
    );
  }

  private stopTimers(): void {
    for (const timer of [this.clockTimer, this.heartbeatTimer, this.storageTimer]) {
      if (timer !== null) window.clearInterval(timer);
    }
    this.clockTimer = this.heartbeatTimer = this.storageTimer = null;
  }

  private releaseCaptureLock(): void {
    this.captureLock?.release();
    this.captureLock = null;
    this.patch({ lockKind: null });
  }

  private async refreshStorage(requestPersistence: boolean): Promise<void> {
    try {
      this.patch({ storage: await inspectStorageSafety(requestPersistence) });
    } catch {
      this.patch({ storage: null });
    }
  }

  async debugState(): Promise<RecordingStateResponse | null> {
    if (!this.localSession) return null;
    return fetchRecordingState(this.localSession.sessionId);
  }

  async debugPendingChunks(): Promise<SpoolChunk[]> {
    if (!this.localSession) return [];
    return listSpoolChunks(this.localSession.sessionId);
  }
}