import type { RecordingStateResponse } from '../api/generated/types.gen';
import { RecorderController, type RecorderSnapshot } from './controller';
import { getLatestLocalSession, type SpoolChunk } from './db';

const STALE_ACTIVE_GENERATION_DETAIL = 'capture writer or epoch is no longer active';

export type FencedRecorderSnapshot = RecorderSnapshot & {
  captureFenced: boolean;
};

type Listener = () => void;

function isStaleActiveGenerationError(message: string | null): boolean {
  return message === STALE_ACTIVE_GENERATION_DETAIL;
}

export class FencedRecorderController {
  private readonly inner = new RecorderController();
  private readonly listeners = new Set<Listener>();
  private captureFenced = false;
  private stoppingForFence = false;
  private snapshot: FencedRecorderSnapshot;

  constructor() {
    this.snapshot = this.decorate(this.inner.getSnapshot());
    this.inner.subscribe(this.handleInnerUpdate);
  }

  readonly subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getSnapshot = (): FencedRecorderSnapshot => this.snapshot;

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  private decorate(raw: RecorderSnapshot): FencedRecorderSnapshot {
    if (!this.captureFenced) return { ...raw, captureFenced: false };

    const stopping = raw.phase === 'recording' || raw.phase === 'finalizing';
    return {
      ...raw,
      captureFenced: true,
      message: stopping
        ? 'A newer capture generation fenced this recorder. The microphone is being stopped and emitted audio is being kept locally.'
        : raw.pendingChunks > 0
          ? 'Capture stopped after ownership changed. Local audio is retained as orphaned evidence and is not safe to sync under the current generation.'
          : 'Capture stopped after ownership changed. This local capture identity is stale, so recovery actions that would be rejected are withheld.',
    };
  }

  private publish(raw = this.inner.getSnapshot()): void {
    this.snapshot = this.decorate(raw);
    this.emit();
  }

  private readonly handleInnerUpdate = (): void => {
    const raw = this.inner.getSnapshot();
    if (
      raw.sessionId &&
      isStaleActiveGenerationError(raw.error) &&
      !this.captureFenced
    ) {
      this.captureFenced = true;
    }
    this.publish(raw);

    if (this.captureFenced && raw.phase === 'recording' && !this.stoppingForFence) {
      this.stoppingForFence = true;
      void this.inner.stop().finally(() => {
        this.stoppingForFence = false;
      });
    }
  };

  private async refreshFenceFromServer(): Promise<void> {
    try {
      const [local, state] = await Promise.all([getLatestLocalSession(), this.inner.debugState()]);
      if (!local || !state || state.session.state === 'complete') return;

      const claimAlreadyAdvanced =
        Boolean(local.pendingWriterId) &&
        state.session.capture_epoch === local.captureEpoch + 1 &&
        state.session.active_writer_id === local.pendingWriterId;
      if (claimAlreadyAdvanced) return;

      const ownershipChanged =
        state.session.capture_epoch !== local.captureEpoch ||
        (state.session.active_writer_id !== null &&
          state.session.active_writer_id !== local.writerId);
      if (ownershipChanged) {
        this.captureFenced = true;
        this.publish();
      }
    } catch {
      // Ordinary recovery already owns connectivity/error reporting. Fence inference is best effort
      // until server truth can be read again; no local evidence is deleted here.
    }
  }

  async initialize(): Promise<void> {
    await this.inner.initialize();
    await this.refreshFenceFromServer();
  }

  dispose(): void {
    this.inner.dispose();
  }

  async start(): Promise<void> {
    if (this.captureFenced) return;
    await this.inner.start();
  }

  async resume(): Promise<void> {
    if (this.captureFenced) return;
    await this.inner.resume();
  }

  async stop(): Promise<void> {
    await this.inner.stop();
  }

  deferFinalization(): void {
    if (this.captureFenced) return;
    this.inner.deferFinalization();
  }

  async finishRecovered(): Promise<void> {
    if (this.captureFenced) return;
    await this.inner.finishRecovered();
  }

  async syncNow(): Promise<void> {
    if (this.captureFenced) return;
    await this.inner.syncNow();
  }

  async debugState(): Promise<RecordingStateResponse | null> {
    return this.inner.debugState();
  }

  async debugPendingChunks(): Promise<SpoolChunk[]> {
    return this.inner.debugPendingChunks();
  }
}
