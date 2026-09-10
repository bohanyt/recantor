import { env } from '../env';

export type RealtimeAudioStatus = 'inactive' | 'connecting' | 'live' | 'degraded';

export type RealtimeAudioUpdate = {
  status: RealtimeAudioStatus;
  error: string | null;
};

export type RealtimeAudioLaneOptions = {
  sessionId: string;
  writerId: string;
  captureEpoch: number;
  timelineBaseMs: number;
  onUpdate: (update: RealtimeAudioUpdate) => void;
  onUtteranceCommitted: () => void;
};

const MAX_BUFFERED_BYTES = 512 * 1024;
const RECOVERED_BUFFERED_BYTES = 128 * 1024;
const STOP_GRACE_MS = 300;

export function realtimeWebSocketUrl(sessionId: string): string {
  const base = new URL(env.apiBaseUrl);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  base.pathname = `${base.pathname.replace(/\/$/, '')}/api/v1/sessions/${encodeURIComponent(
    sessionId,
  )}/realtime-audio`;
  base.search = '';
  base.hash = '';
  return base.toString();
}

export function encodePcmPacket(sampleOffset: number, samples: Float32Array): ArrayBuffer {
  if (!Number.isSafeInteger(sampleOffset) || sampleOffset < 0) {
    throw new Error('Realtime PCM sample offset must be a non-negative safe integer.');
  }
  const packet = new ArrayBuffer(8 + samples.length * 2);
  const view = new DataView(packet);
  const low = sampleOffset >>> 0;
  const high = Math.floor(sampleOffset / 2 ** 32);
  view.setUint32(0, low, true);
  view.setUint32(4, high, true);

  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index] ?? 0));
    const encoded = value < 0 ? Math.round(value * 32768) : Math.round(value * 32767);
    view.setInt16(8 + index * 2, encoded, true);
  }
  return packet;
}

export class RealtimeAudioLane {
  private socket: WebSocket | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private worklet: AudioWorkletNode | null = null;
  private sink: GainNode | null = null;
  private stopped = false;
  private status: RealtimeAudioStatus = 'inactive';
  private stopAcknowledged: (() => void) | null = null;

  constructor(private readonly options: RealtimeAudioLaneOptions) {}

  private publish(status: RealtimeAudioStatus, error: string | null = null): void {
    this.status = status;
    this.options.onUpdate({ status, error });
  }

  async start(stream: MediaStream): Promise<void> {
    if (this.status !== 'inactive') return;
    this.publish('connecting');

    try {
      const context = new AudioContext({ latencyHint: 'interactive' });
      this.context = context;
      await context.audioWorklet.addModule('/realtime-pcm-worklet.js');
      if (this.stopped) return;

      const socket = new WebSocket(realtimeWebSocketUrl(this.options.sessionId));
      socket.binaryType = 'arraybuffer';
      this.socket = socket;

      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const settleReject = (error: Error) => {
          if (settled) return;
          settled = true;
          reject(error);
        };
        socket.addEventListener(
          'open',
          () => {
            socket.send(
              JSON.stringify({
                type: 'start',
                writer_id: this.options.writerId,
                capture_epoch: this.options.captureEpoch,
                stream_id: crypto.randomUUID(),
                timeline_base_ms: this.options.timelineBaseMs,
                sample_rate: context.sampleRate,
                channels: 1,
                sample_format: 's16le',
              }),
            );
          },
          { once: true },
        );
        socket.addEventListener(
          'error',
          () => settleReject(new Error('Realtime audio WebSocket failed to connect.')),
          { once: true },
        );
        socket.addEventListener(
          'close',
          () =>
            settleReject(new Error('Realtime audio WebSocket closed before it became ready.')),
          { once: true },
        );
        socket.addEventListener('message', (event) => {
          if (typeof event.data !== 'string') return;
          try {
            const message = JSON.parse(event.data) as { type?: string; detail?: string };
            if (message.type === 'ready' && !settled) {
              settled = true;
              resolve();
            } else if (message.type === 'error') {
              settleReject(
                new Error(message.detail || 'Realtime audio server rejected the stream.'),
              );
            }
          } catch {
            settleReject(new Error('Realtime audio server sent invalid control JSON.'));
          }
        });
      });
      if (this.stopped) return;

      socket.addEventListener('message', this.handleServerMessage);
      socket.addEventListener('close', this.handleSocketClose);
      socket.addEventListener('error', this.handleSocketError);

      const source = context.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(context, 'recantor-realtime-pcm');
      const sink = context.createGain();
      sink.gain.value = 0;
      this.source = source;
      this.worklet = worklet;
      this.sink = sink;
      worklet.port.onmessage = this.handlePcmFrame;
      source.connect(worklet);
      worklet.connect(sink);
      sink.connect(context.destination);

      if (context.state === 'suspended') await context.resume();
      if (context.state !== 'running') {
        throw new Error(`Realtime AudioContext is ${context.state}.`);
      }
      this.publish('live');
    } catch (error) {
      await this.cleanup(true);
      if (this.stopped) {
        this.publish('inactive');
        return;
      }
      const message = error instanceof Error ? error.message : 'Realtime audio startup failed.';
      this.publish('degraded', message);
      throw error;
    }
  }

  private readonly handlePcmFrame = (event: MessageEvent): void => {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN || this.stopped) return;
    const value = event.data as { sampleOffset?: unknown; samples?: unknown };
    if (typeof value.sampleOffset !== 'number' || !(value.samples instanceof Float32Array)) return;

    if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
      if (this.status !== 'degraded') {
        this.publish(
          'degraded',
          'Realtime audio is dropping frames because transport is backlogged.',
        );
      }
      return;
    }
    if (this.status === 'degraded' && socket.bufferedAmount <= RECOVERED_BUFFERED_BYTES) {
      this.publish('live');
    }

    try {
      socket.send(encodePcmPacket(value.sampleOffset, value.samples));
    } catch (error) {
      this.publish(
        'degraded',
        error instanceof Error ? error.message : 'Realtime audio frame send failed.',
      );
    }
  };

  private readonly handleServerMessage = (event: MessageEvent): void => {
    if (typeof event.data !== 'string') return;
    try {
      const message = JSON.parse(event.data) as { type?: string; detail?: string };
      if (message.type === 'utterance_committed') {
        this.options.onUtteranceCommitted();
      } else if (message.type === 'discontinuity') {
        this.publish(
          'degraded',
          'Realtime audio transport discontinuity detected; archive capture continues.',
        );
      } else if (message.type === 'stopped') {
        this.stopAcknowledged?.();
      } else if (message.type === 'error') {
        this.publish('degraded', message.detail || 'Realtime audio server reported an error.');
      }
    } catch {
      this.publish('degraded', 'Realtime audio server sent invalid control JSON.');
    }
  };

  private readonly handleSocketClose = (): void => {
    if (!this.stopped) {
      this.publish('degraded', 'Realtime audio connection closed; archive capture continues.');
    }
  };

  private readonly handleSocketError = (): void => {
    if (!this.stopped) {
      this.publish('degraded', 'Realtime audio connection failed; archive capture continues.');
    }
  };

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    const socket = this.socket;
    let acknowledge: Promise<void> | null = null;
    if (socket?.readyState === WebSocket.OPEN) {
      acknowledge = new Promise<void>((resolve) => {
        this.stopAcknowledged = resolve;
      });
      try {
        socket.send(JSON.stringify({ type: 'stop' }));
      } catch {
        acknowledge = null;
      }
    }

    this.disconnectGraph();
    if (acknowledge) {
      await Promise.race([
        acknowledge,
        new Promise<void>((resolve) => window.setTimeout(resolve, STOP_GRACE_MS)),
      ]);
    }
    await this.cleanup(true);
    this.publish('inactive');
  }

  private disconnectGraph(): void {
    if (this.worklet) this.worklet.port.onmessage = null;
    try {
      this.source?.disconnect();
    } catch {
      // The archive MediaStream owns microphone tracks; graph cleanup is best effort only.
    }
    try {
      this.worklet?.disconnect();
    } catch {
      // Already disconnected.
    }
    try {
      this.sink?.disconnect();
    } catch {
      // Already disconnected.
    }
    this.source = null;
    this.worklet = null;
    this.sink = null;
  }

  private async cleanup(closeSocket: boolean): Promise<void> {
    this.disconnectGraph();
    const socket = this.socket;
    this.socket = null;
    this.stopAcknowledged = null;
    if (closeSocket && socket && socket.readyState < WebSocket.CLOSING) {
      try {
        socket.close(1000);
      } catch {
        // Socket cleanup is best effort and must never affect archive capture.
      }
    }
    const context = this.context;
    this.context = null;
    if (context && context.state !== 'closed') {
      try {
        await context.close();
      } catch {
        // AudioContext cleanup is best effort and never stops the shared MediaStream tracks.
      }
    }
  }
}
