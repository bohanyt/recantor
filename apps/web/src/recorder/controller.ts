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
  chunkKey,
  countSpoolChunks,
  deleteLocalSession,
  deleteSpoolChunk,
  getLatestLocalSession,
  listSpoolChunks,
  putLocalSession,
  putSpoolChunk,
  type LocalRecordingSession,
  type SpoolChunk,
} from './db';
import { sha256Blob } from './hash';
import { reconcileLocalSpool } from './reconcile';
import { inspectStorageSafety, type StorageSafety } from './storageSafety';
import { acquireCaptureTabLock, type CaptureTabLock } from './tabCoordinator';

export type RecorderPhase =
  | 'idle'
  | 'requesting'
  | 'recording'
  | 'recoverable'
  | 'finalizing'
  | 'complete'
  | 'error';

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
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ];
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
  return error.status === null || error.status === 408 || error.status === 429 || error.status >= 500;
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
  private heartbeatTimer: number | null = null;
  private clockTimer: number | null = null;
  private storageTimer: number | null = null;
  private initialized = false;

  readonly subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getSnapshot = (): RecorderSnapshot => this.snapshot;

  private patch(update: Partial<RecorderSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...update };
    for (const listener of this.listeners) listener();
  }

  private readonly handleOnline = (): void => {
    this.patch({ online: true, message: 'Network restored. Reconciling pending audio…' });
    void this.syncNow();
  };

  private readonly handleOffline = (): void => {
    this.patch({ online: false, message: 'Offline. Unacknowledged audio remains in browser storage.' });
  };

  async initialize(): Promise<void> {
    if (this.initialized) return;
    this.initialized = true;
    window.addEventListener('online', this.handleOnline);
    window.addEventListener('offline', this.handleOffline);
    await this.refreshStorage(false);

    const local = await getLatestLocalSession();
    if (!local) return;

    this.localSession = local;
    try {
      const state = await fetchRecordingState(local.sessionId);
      await reconcileLocalSpool(local.sessionId, state);
      if (state.session.state === 'complete') {
        await deleteLocalSession(local.sessionId);
        this.localSession = null;
        this.patch({ phase: 'idle', message: 'Previous recording was already complete.' });
        return;
      }
      const pendingChunks = await countSpoolChunks(local.sessionId);
      this.patch({
        phase: 'recoverable',
        sessionId: local.sessionId,
        pendingChunks,
        highestAckedSequence: state.highest_contiguous_sequence,
        gapCount: state.gaps.length,
        elapsedMs: Math.max(0, Date.now() - local.recordingStartedWallMs),
        message: 'Interrupted recording found. Resume it or finish the recovered audio.',
      });
    } catch (error) {
      const pendingChunks = await countSpoolChunks(local.sessionId);
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
    this.stopTimers();
  }

  async start(): Promise<void> {
    if (this.snapshot.phase !== 'idle' && this.snapshot.phase !== 'complete') return;
    this.patch({ phase: 'requesting', error: null, message: 'Requesting microphone access…' });
    await this.refreshStorage(true);

    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const writerId = crypto.randomUUID();
      const clientRequestId = crypto.randomUUID();
      const remote = await createLiveSession({
        client_request_id: clientRequestId,
        writer_id: writerId,
      });
      const mimeType = chooseMimeType();
      const now = Date.now();
      const local: LocalRecordingSession = {
        sessionId: remote.id,
        clientRequestId,
        writerId,
        captureEpoch: remote.capture_epoch,
        state: 'recording',
        mimeType,
        lastSequence: 0,
        lastMonotonicEndMs: 0,
        lastWallEndMs: null,
        recordingStartedWallMs: now,
        createdAt: now,
        updatedAt: now,
      };
      await putLocalSession(local);
      this.localSession = local;
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab already owns this recording session.');
      this.patch({ lockKind: this.captureLock.kind });
      this.beginMediaRecorder(stream, local);
      stream = null;
    } catch (error) {
      stopStream(stream);
      this.patch({
        phase: 'error',
        error: error instanceof Error ? error.message : 'Failed to start recording.',
        message: 'Recording did not start.',
      });
    }
  }

  async resume(): Promise<void> {
    const local = this.localSession ?? (await getLatestLocalSession());
    if (!local || this.snapshot.phase !== 'recoverable') return;
    this.patch({ phase: 'requesting', error: null, message: 'Reclaiming the interrupted recording…' });
    await this.refreshStorage(true);

    let stream: MediaStream | null = null;
    try {
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab is already resuming this recording.');
      this.patch({ lockKind: this.captureLock.kind });

      const state = await fetchRecordingState(local.sessionId);
      await reconcileLocalSpool(local.sessionId, state);
      const remote = await claimCapture(local.sessionId, {
        writer_id: local.writerId,
        expected_epoch: local.captureEpoch,
      });
      local.captureEpoch = remote.capture_epoch;
      local.state = 'recording';
      local.updatedAt = Date.now();
      await putLocalSession(local);
      this.localSession = local;

      await this.recordRecoveryGap(local);
      await this.syncNow();
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.beginMediaRecorder(stream, local);
      stream = null;
    } catch (error) {
      stopStream(stream);
      this.releaseCaptureLock();
      this.patch({
        phase: 'recoverable',
        error: error instanceof Error ? error.message : 'Failed to resume recording.',
        message: 'Recovery is still available; resume could not start.',
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
    this.patch({ phase: 'finalizing', message: 'Stopping capture and flushing the final audio fragment…' });
    this.stopTimers();

    try {
      await this.stopMediaRecorder();
      await this.chunkChain;
      await this.syncNow();
      const pending = await countSpoolChunks(local.sessionId);
      if (pending > 0) {
        local.state = 'interrupted';
        local.updatedAt = Date.now();
        await putLocalSession(local);
        this.releaseCaptureLock();
        this.patch({
          phase: 'recoverable',
          pendingChunks: pending,
          message: 'Capture stopped, but some audio still needs a server ACK. Finish recovery later.',
        });
        return;
      }
      await this.finalizeLocalSession(local);
    } catch (error) {
      local.state = 'interrupted';
      local.updatedAt = Date.now();
      await putLocalSession(local);
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
    this.patch({ phase: 'finalizing', error: null, message: 'Reconciling recovered audio before finalization…' });
    try {
      this.captureLock = await acquireCaptureTabLock(local.sessionId, local.writerId);
      if (!this.captureLock) throw new Error('Another tab is already handling this recording.');
      this.patch({ lockKind: this.captureLock.kind });
      const state = await fetchRecordingState(local.sessionId);
      await reconcileLocalSpool(local.sessionId, state);
      await claimCapture(local.sessionId, {
        writer_id: local.writerId,
        expected_epoch: local.captureEpoch,
      });
      await this.recordRecoveryGap(local);
      await this.syncNow();
      const pending = await countSpoolChunks(local.sessionId);
      if (pending > 0) throw new Error(`${pending} local audio fragment(s) still lack a durable ACK.`);
      await this.finalizeLocalSession(local);
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
    this.captureStartedPerformance = performance.now();
    this.captureBaseMonotonicMs = local.lastMonotonicEndMs;

    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size === 0) return;
      const capturedPerformance = performance.now();
      const wallEndMs = Date.now();
      this.chunkChain = this.chunkChain
        .then(() => this.persistCapturedBlob(event.data, capturedPerformance, wallEndMs))
        .catch((error) => {
          this.patch({
            localWriteFailed: true,
            error: error instanceof Error ? error.message : 'Browser recovery spool write failed.',
            message: navigator.onLine
              ? 'Local recovery write failed. Server sync is required for safety.'
              : 'Unsafe: browser recovery write failed while offline.',
          });
        });
    });
    recorder.addEventListener('error', () => {
      this.patch({ error: 'MediaRecorder reported a capture error.', message: 'Capture degraded.' });
    });

    recorder.start(CHUNK_TIMESLICE_MS);
    this.startTimers();
    this.patch({
      phase: 'recording',
      sessionId: local.sessionId,
      error: null,
      message: 'Recording. Audio is kept locally until the server durably acknowledges it.',
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

    await putSpoolChunk(chunk);
    local.lastSequence = sequence;
    local.lastMonotonicEndMs = monotonicEndMs;
    local.lastWallEndMs = wallEndMs;
    local.updatedAt = Date.now();
    await putLocalSession(local);
    this.patch({
      pendingChunks: await countSpoolChunks(local.sessionId),
      localWriteFailed: false,
    });
    void this.syncNow();
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
          message: this.snapshot.phase === 'recording' ? 'Recording — server sync is current.' : this.snapshot.message,
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
    if (local.lastWallEndMs === null) return;
    const wallEndMs = Date.now();
    if (wallEndMs - local.lastWallEndMs < 1_000) return;
    const body: GapDeclarationRequest = {
      client_gap_id: crypto.randomUUID(),
      writer_id: local.writerId,
      capture_epoch: local.captureEpoch,
      wall_started_at: new Date(local.lastWallEndMs).toISOString(),
      wall_ended_at: new Date(wallEndMs).toISOString(),
      reason: 'browser_interruption_before_recovery',
    };
    await declareRecordingGap(local.sessionId, body);
    const state = await fetchRecordingState(local.sessionId);
    this.patch({ gapCount: state.gaps.length });
  }

  private async finalizeLocalSession(local: LocalRecordingSession): Promise<void> {
    local.state = 'finalizing';
    local.updatedAt = Date.now();
    await putLocalSession(local);
    const body: FinalizeSessionRequest = {
      writer_id: local.writerId,
      capture_epoch: local.captureEpoch,
      final_sequence: local.lastSequence,
      final_monotonic_end_ms: local.lastMonotonicEndMs,
      gap_sequences: [],
    };
    const final = await finalizeRecording(local.sessionId, body);
    if (!final.complete) {
      throw new Error(`Server still expects sequence(s): ${final.missing_sequences.join(', ')}`);
    }
    await deleteLocalSession(local.sessionId);
    this.localSession = null;
    this.releaseCaptureLock();
    this.patch({
      phase: 'complete',
      pendingChunks: 0,
      gapCount: final.gaps.length,
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
    this.storageTimer = window.setInterval(() => void this.refreshStorage(false), STORAGE_REFRESH_MS);
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
