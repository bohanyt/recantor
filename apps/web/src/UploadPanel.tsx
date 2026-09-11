import { Tus, Uppy } from './vendor/uppy.min.mjs';
import { type ChangeEvent, type DragEvent, useEffect, useRef, useState } from 'react';

import {
  createUploadSession,
  getUploadSession,
  UploadApiError,
  type UploadSession,
} from './upload/api';
import {
  clearUploadRecovery,
  createUploadRecovery,
  loadUploadRecovery,
  saveUploadRecovery,
  type UploadRecovery,
} from './upload/recovery';

const ACCEPT = '.wav,.mp3,.m4a,.ogg,.webm,.mp4';
const VERIFY_ATTEMPTS = 20;
const VERIFY_DELAY_MS = 250;

type UploadPhase =
  | 'idle'
  | 'ready'
  | 'preparing'
  | 'uploading'
  | 'paused'
  | 'verifying'
  | 'complete'
  | 'error';

function bytesLabel(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function mediaType(file: File): string {
  return file.type || 'application/octet-stream';
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function recoverOrCreateSession(
  file: File,
): Promise<{ recovery: UploadRecovery; session: UploadSession }> {
  let recovery = loadUploadRecovery(file) ?? createUploadRecovery(file);

  if (recovery.sessionId) {
    try {
      const existing = await getUploadSession(recovery.sessionId, recovery.capabilityToken);
      return { recovery, session: existing };
    } catch (error) {
      if (!(error instanceof UploadApiError) || ![403, 404, 410].includes(error.status)) {
        throw error;
      }
      clearUploadRecovery(recovery);
      recovery = createUploadRecovery(file);
    }
  }

  const session = await createUploadSession({
    client_request_id: recovery.clientRequestId,
    capability_token: recovery.capabilityToken,
    original_filename: file.name,
    content_type: mediaType(file),
    byte_length: file.size,
  });
  recovery = {
    ...recovery,
    sessionId: session.session_id,
    expiresAt: session.expires_at,
  };
  saveUploadRecovery(recovery);
  return { recovery, session };
}

async function waitForDurableCompletion(
  sessionId: string,
  capabilityToken: string,
): Promise<UploadSession> {
  for (let attempt = 0; attempt < VERIFY_ATTEMPTS; attempt += 1) {
    const session = await getUploadSession(sessionId, capabilityToken);
    if (session.state === 'uploaded' && session.completed_at) return session;
    if (session.state === 'failed') {
      throw new Error(session.failure_message || 'Server rejected the completed upload.');
    }
    await delay(VERIFY_DELAY_MS);
  }
  throw new Error(
    'Transfer finished, but durable server confirmation did not arrive. Retry safely.',
  );
}

export function UploadPanel() {
  const inputRef = useRef<HTMLInputElement>(null);
  const uppyRef = useRef<Uppy | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState('Choose an existing recording to upload.');
  const [dragging, setDragging] = useState(false);

  useEffect(
    () => () => {
      uppyRef.current?.destroy();
      uppyRef.current = null;
    },
    [],
  );

  function resetTransfer(): void {
    uppyRef.current?.destroy();
    uppyRef.current = null;
    setProgress(0);
  }

  function selectFile(nextFile: File | null): void {
    resetTransfer();
    setFile(nextFile);
    if (nextFile) {
      setPhase('ready');
      setMessage('Ready to start or resume this recording.');
    } else {
      setPhase('idle');
      setMessage('Choose an existing recording to upload.');
    }
  }

  function onInputChange(event: ChangeEvent<HTMLInputElement>): void {
    selectFile(event.target.files?.[0] ?? null);
  }

  function onDrop(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    setDragging(false);
    selectFile(event.dataTransfer.files?.[0] ?? null);
  }

  async function start(): Promise<void> {
    if (!file) return;
    resetTransfer();
    setPhase('preparing');
    setMessage('Restoring the durable upload session…');

    try {
      const { recovery, session } = await recoverOrCreateSession(file);
      if (session.state === 'uploaded' && session.completed_at) {
        clearUploadRecovery(recovery);
        setProgress(100);
        setPhase('complete');
        setMessage('Durably uploaded. Ready for downstream transcription processing.');
        return;
      }

      const uppy = new Uppy({
        autoProceed: false,
        allowMultipleUploadBatches: false,
        restrictions: {
          maxNumberOfFiles: 1,
          allowedFileTypes: ['.wav', '.mp3', '.m4a', '.ogg', '.webm', '.mp4'],
        },
      }).use(Tus, {
        endpoint: session.upload_endpoint,
        headers: { 'X-Recantor-Upload-Token': recovery.capabilityToken },
        retryDelays: [0, 1_000, 3_000, 5_000, 10_000],
        allowedMetaFields: ['recantor_session_id', 'filename', 'filetype'],
      });
      uppyRef.current = uppy;

      uppy.on('upload-progress', (_uppyFile, uploadProgress) => {
        const total = uploadProgress.bytesTotal ?? file.size;
        const percent =
          total > 0 ? Math.floor((uploadProgress.bytesUploaded / total) * 100) : 0;
        setProgress(Math.min(99, Math.max(0, percent)));
      });
      uppy.on('upload-error', (_uppyFile, error, response) => {
        const status = response?.status ? ` (HTTP ${response.status})` : '';
        setPhase('error');
        setMessage(`${error.message || 'Upload interrupted'}${status}. You can retry safely.`);
      });
      uppy.on('upload-success', () => {
        setPhase('verifying');
        setMessage('Transfer complete. Verifying durable server state…');
        void waitForDurableCompletion(session.session_id, recovery.capabilityToken)
          .then((durable) => {
            clearUploadRecovery(recovery);
            setProgress(100);
            setPhase('complete');
            setMessage(
              durable.completed_at
                ? 'Durably uploaded. Ready for downstream transcription processing.'
                : 'Durable upload confirmation is pending.',
            );
          })
          .catch((error: unknown) => {
            setPhase('error');
            setMessage(error instanceof Error ? error.message : 'Durable verification failed.');
          });
      });

      uppy.addFile({
        name: file.name,
        type: mediaType(file),
        data: file,
        meta: {
          recantor_session_id: session.session_id,
          filename: session.original_filename,
          filetype: session.content_type,
        },
      });
      setPhase('uploading');
      setMessage(
        session.received_bytes > 0 ? 'Resuming from durable server progress…' : 'Uploading…',
      );
      void uppy.upload();
    } catch (error) {
      setPhase('error');
      setMessage(error instanceof Error ? error.message : 'Unable to start upload.');
    }
  }

  function pause(): void {
    if (!uppyRef.current) return;
    uppyRef.current.pauseAll();
    setPhase('paused');
    setMessage('Paused. Server progress is retained and can be resumed.');
  }

  function resume(): void {
    if (!uppyRef.current) return;
    if (phase === 'error') {
      void uppyRef.current.retryAll();
    } else {
      uppyRef.current.resumeAll();
    }
    setPhase('uploading');
    setMessage('Resuming upload…');
  }

  const busy = ['preparing', 'uploading', 'paused', 'verifying'].includes(phase);

  return (
    <section className="mt-6 rounded-[2rem] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-7">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
            Existing recording
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">
            Upload without starting over.
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted)]">
            WAV, MP3, M4A, OGG, WebM, or MP4. The transfer uses resumable tus storage; a completed
            bar is not treated as durable until the Recantor API confirms the stored object.
          </p>
        </div>
        <div className="min-w-28 rounded-2xl border border-[var(--border)] px-4 py-3 text-right">
          <p className="font-mono text-2xl font-semibold tabular-nums" data-testid="upload-progress">
            {progress}%
          </p>
          <p className="mt-1 text-xs uppercase tracking-[0.14em] text-[var(--muted)]">{phase}</p>
        </div>
      </div>

      <div
        className={`mt-6 rounded-2xl border border-dashed p-6 text-center transition ${
          dragging ? 'border-[var(--accent)]' : 'border-[var(--border)]'
        }`}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        data-testid="upload-dropzone"
      >
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          accept={ACCEPT}
          onChange={onInputChange}
          data-testid="upload-file-input"
        />
        <button
          type="button"
          className="rounded-full border border-[var(--border)] px-4 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
        >
          Choose recording
        </button>
        <p className="mt-3 text-sm text-[var(--muted)]">or drop one file here</p>
        {file ? (
          <p className="mt-3 text-sm font-medium" data-testid="upload-selected-file">
            {file.name} · {bytesLabel(file.size)}
          </p>
        ) : null}
      </div>

      <div
        className="mt-5 h-2 overflow-hidden rounded-full bg-[var(--background)]"
        aria-hidden="true"
      >
        <div
          className="h-full bg-[var(--accent)] transition-[width]"
          style={{ width: `${progress}%` }}
        />
      </div>

      <p className="mt-4 text-sm leading-6 text-[var(--muted)]" data-testid="upload-message">
        {message}
      </p>

      <div className="mt-5 flex flex-wrap gap-3">
        <button
          type="button"
          className="rounded-full bg-[var(--foreground)] px-5 py-2 text-sm font-semibold text-[var(--background)] disabled:opacity-50"
          onClick={() => void start()}
          disabled={!file || busy || phase === 'complete'}
          data-testid="upload-start"
        >
          {phase === 'error' ? 'Restart safely' : 'Start / resume'}
        </button>
        <button
          type="button"
          className="rounded-full border border-[var(--border)] px-5 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={pause}
          disabled={phase !== 'uploading'}
          data-testid="upload-pause"
        >
          Pause
        </button>
        <button
          type="button"
          className="rounded-full border border-[var(--border)] px-5 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={resume}
          disabled={phase !== 'paused' && phase !== 'error'}
          data-testid="upload-resume"
        >
          Retry / resume
        </button>
      </div>
    </section>
  );
}
