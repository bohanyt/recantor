import { Tus, Uppy } from './vendor/uppy.min.mjs';
import { type ChangeEvent, type DragEvent, useEffect, useRef, useState } from 'react';

import { TranscriptLog } from './transcript/TranscriptLog';
import {
  createUploadSession,
  downloadUploadExport,
  getUploadResultStatus,
  getUploadSession,
  getUploadTranscript,
  UploadApiError,
  type UploadExportFormat,
  type UploadResultStatus,
  type UploadSession,
  type UploadTranscriptPage,
} from './upload/api';
import {
  clearUploadRecovery,
  createUploadRecovery,
  loadLatestUploadRecovery,
  loadUploadRecovery,
  markUploadDurablyComplete,
  saveUploadRecovery,
  type UploadRecovery,
} from './upload/recovery';

const ACCEPT = '.wav,.mp3,.m4a,.ogg,.webm,.mp4';
const VERIFY_ATTEMPTS = 20;
const VERIFY_DELAY_MS = 250;
const RESULT_POLL_MS = 1_000;
const RESULT_RETRY_MS = 1_500;
const TRANSCRIPT_PAGE_SIZE = 200;

type UploadPhase =
  | 'idle'
  | 'ready'
  | 'preparing'
  | 'uploading'
  | 'paused'
  | 'verifying'
  | 'upload_complete'
  | 'preparing_audio'
  | 'transcribing'
  | 'reconnecting'
  | 'complete'
  | 'no_speech'
  | 'failed'
  | 'expired'
  | 'error';

const phaseLabels: Record<UploadPhase, string> = {
  idle: 'Ready',
  ready: 'Ready',
  preparing: 'Preparing upload',
  uploading: 'Uploading',
  paused: 'Paused',
  verifying: 'Verifying upload',
  upload_complete: 'Upload complete',
  preparing_audio: 'Preparing audio',
  transcribing: 'Transcribing',
  reconnecting: 'Reconnecting',
  complete: 'Complete',
  no_speech: 'No speech detected',
  failed: 'Failed',
  expired: 'Access expired',
  error: 'Needs attention',
};

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
      if (error instanceof UploadApiError && error.status === 410) {
        clearUploadRecovery(recovery);
        throw error;
      }
      if (!(error instanceof UploadApiError) || ![403, 404].includes(error.status)) {
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
    updatedAt: new Date().toISOString(),
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

function resultMessage(result: UploadResultStatus): string {
  if (result.state === 'preparing') {
    return 'Durably uploaded. Preparing audio for transcription…';
  }
  if (result.state === 'transcribing') {
    return 'Durably uploaded. Audio is ready. Transcribing the recording…';
  }
  if (result.state === 'complete') {
    return 'Durably uploaded. Transcript complete. Review it below or export a copy.';
  }
  if (result.state === 'no_speech') {
    return 'Durably uploaded. Processing complete. No speech was detected, so there is no transcript text to show.';
  }
  if (result.state === 'failed') {
    return result.failure_message
      ? `Durably uploaded. ${result.failure_message}`
      : 'Durably uploaded, but transcription could not be completed.';
  }
  return 'Upload transfer is not durably complete yet. Reselect the same file to resume it.';
}

export function UploadPanel() {
  const inputRef = useRef<HTMLInputElement>(null);
  const uppyRef = useRef<Uppy | null>(null);
  const resultTimerRef = useRef<number | null>(null);
  const activeResultSessionRef = useRef<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState('Choose an existing recording to upload.');
  const [dragging, setDragging] = useState(false);
  const [trackedRecovery, setTrackedRecovery] = useState<UploadRecovery | null>(null);
  const [result, setResult] = useState<UploadResultStatus | null>(null);
  const [transcriptPage, setTranscriptPage] = useState<UploadTranscriptPage | null>(null);
  const [downloading, setDownloading] = useState<UploadExportFormat | null>(null);

  function stopResultPolling(): void {
    activeResultSessionRef.current = null;
    if (resultTimerRef.current !== null) {
      window.clearTimeout(resultTimerRef.current);
      resultTimerRef.current = null;
    }
  }

  function handleExpired(recovery: UploadRecovery): void {
    stopResultPolling();
    clearUploadRecovery(recovery);
    setTrackedRecovery(null);
    setResult(null);
    setTranscriptPage(null);
    setPhase('expired');
    setMessage(
      'Saved upload access has expired. The server recording was not deleted; start a fresh upload to access a new result.',
    );
  }

  function handleInvalidRecovery(recovery: UploadRecovery): void {
    stopResultPolling();
    clearUploadRecovery(recovery);
    setTrackedRecovery(null);
    setResult(null);
    setTranscriptPage(null);
    setPhase('error');
    setMessage('Saved upload access is no longer valid. Start a fresh upload to continue.');
  }

  async function loadTranscriptPage(recovery: UploadRecovery, afterSequence = 0): Promise<void> {
    if (!recovery.sessionId) return;
    try {
      const page = await getUploadTranscript(
        recovery.sessionId,
        recovery.capabilityToken,
        afterSequence,
        TRANSCRIPT_PAGE_SIZE,
      );
      setTranscriptPage(page);
    } catch (error) {
      if (error instanceof UploadApiError && error.status === 410) {
        handleExpired(recovery);
        return;
      }
      if (error instanceof UploadApiError && [403, 404].includes(error.status)) {
        handleInvalidRecovery(recovery);
        return;
      }
      setMessage('Transcript page could not be loaded. You can retry without re-uploading.');
    }
  }

  function scheduleResultPoll(recovery: UploadRecovery, delayMs: number): void {
    if (!recovery.sessionId || activeResultSessionRef.current !== recovery.sessionId) return;
    if (resultTimerRef.current !== null) window.clearTimeout(resultTimerRef.current);
    resultTimerRef.current = window.setTimeout(() => void pollResult(recovery), delayMs);
  }

  async function pollResult(recovery: UploadRecovery): Promise<void> {
    if (!recovery.sessionId || activeResultSessionRef.current !== recovery.sessionId) return;
    try {
      const next = await getUploadResultStatus(recovery.sessionId, recovery.capabilityToken);
      if (activeResultSessionRef.current !== recovery.sessionId) return;
      setResult(next);
      setMessage(resultMessage(next));
      if (next.state === 'preparing') {
        setPhase('preparing_audio');
        scheduleResultPoll(recovery, RESULT_POLL_MS);
      } else if (next.state === 'transcribing') {
        setPhase('transcribing');
        scheduleResultPoll(recovery, RESULT_POLL_MS);
      } else if (next.state === 'complete') {
        setPhase('complete');
        await loadTranscriptPage(recovery, 0);
      } else if (next.state === 'no_speech') {
        setPhase('no_speech');
        setTranscriptPage(null);
      } else if (next.state === 'failed') {
        setPhase('failed');
        setTranscriptPage(null);
      } else {
        setPhase('upload_complete');
        setMessage(resultMessage(next));
      }
    } catch (error) {
      if (activeResultSessionRef.current !== recovery.sessionId) return;
      if (error instanceof UploadApiError && error.status === 410) {
        handleExpired(recovery);
        return;
      }
      if (error instanceof UploadApiError && [403, 404].includes(error.status)) {
        handleInvalidRecovery(recovery);
        return;
      }
      setPhase('reconnecting');
      setMessage('Result tracking is temporarily disconnected. Reconnecting without re-uploading…');
      scheduleResultPoll(recovery, RESULT_RETRY_MS);
    }
  }

  function trackResult(recovery: UploadRecovery): void {
    if (!recovery.sessionId) return;
    stopResultPolling();
    activeResultSessionRef.current = recovery.sessionId;
    setTrackedRecovery(recovery);
    setProgress(100);
    setPhase('upload_complete');
    setMessage('Durably uploaded. Checking server processing state…');
    void pollResult(recovery);
  }

  async function restoreSavedUpload(recovery: UploadRecovery): Promise<void> {
    if (!recovery.sessionId) return;
    if (recovery.durableCompletedAt) {
      trackResult(recovery);
      return;
    }
    try {
      const session = await getUploadSession(recovery.sessionId, recovery.capabilityToken);
      if (session.state === 'uploaded' && session.completed_at) {
        const completed = markUploadDurablyComplete(
          recovery,
          session.completed_at,
          session.expires_at,
        );
        trackResult(completed);
        return;
      }
      if (session.state === 'failed') {
        setPhase('failed');
        setMessage('The saved upload failed. Start a fresh upload to try again.');
        return;
      }
      setPhase('ready');
      setMessage('A saved upload is still in progress. Reselect the same file to resume it.');
    } catch (error) {
      if (error instanceof UploadApiError && error.status === 410) {
        handleExpired(recovery);
      } else if (error instanceof UploadApiError && [403, 404].includes(error.status)) {
        handleInvalidRecovery(recovery);
      } else {
        setPhase('reconnecting');
        setMessage('Saved upload state is temporarily unavailable. Reload to retry recovery.');
      }
    }
  }

  useEffect(() => {
    const saved = loadLatestUploadRecovery();
    const restoreTimer = saved?.sessionId
      ? window.setTimeout(() => void restoreSavedUpload(saved), 0)
      : null;
    return () => {
      if (restoreTimer !== null) window.clearTimeout(restoreTimer);
      uppyRef.current?.destroy();
      uppyRef.current = null;
      stopResultPolling();
    };
    // Recovery is intentionally a one-time mount action. Polling owns subsequent refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function resetTransfer(): void {
    uppyRef.current?.destroy();
    uppyRef.current = null;
    setProgress(0);
  }

  function selectFile(nextFile: File | null): void {
    stopResultPolling();
    resetTransfer();
    setTrackedRecovery(null);
    setResult(null);
    setTranscriptPage(null);
    setFile(nextFile);
    if (nextFile) {
      setPhase('ready');
      setMessage('Ready to start or resume this recording upload.');
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
        const completed = markUploadDurablyComplete(
          recovery,
          session.completed_at,
          session.expires_at,
        );
        trackResult(completed);
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
        const percent = total > 0 ? Math.floor((uploadProgress.bytesUploaded / total) * 100) : 0;
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
            if (!durable.completed_at) {
              setPhase('error');
              setMessage('Durable upload confirmation is still pending. Retry safely.');
              return;
            }
            const completed = markUploadDurablyComplete(
              recovery,
              durable.completed_at,
              durable.expires_at,
            );
            trackResult(completed);
          })
          .catch((error: unknown) => {
            if (error instanceof UploadApiError && error.status === 410) {
              handleExpired(recovery);
              return;
            }
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
      if (error instanceof UploadApiError && error.status === 410) {
        const recovery = loadUploadRecovery(file);
        if (recovery) clearUploadRecovery(recovery);
        setPhase('expired');
        setMessage(
          'Saved upload access has expired. The server recording was not deleted; start again for a new access capability.',
        );
        return;
      }
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

  async function exportTranscript(exportFormat: UploadExportFormat): Promise<void> {
    const recovery = trackedRecovery;
    if (!recovery?.sessionId) return;
    setDownloading(exportFormat);
    try {
      const blob = await downloadUploadExport(
        recovery.sessionId,
        recovery.capabilityToken,
        exportFormat,
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `recantor-${recovery.sessionId}.${exportFormat}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (error instanceof UploadApiError && error.status === 410) {
        handleExpired(recovery);
      } else if (error instanceof UploadApiError && [403, 404].includes(error.status)) {
        handleInvalidRecovery(recovery);
      } else {
        setMessage('Export could not be downloaded. Retry without re-uploading.');
      }
    } finally {
      setDownloading(null);
    }
  }

  const busy = ['preparing', 'uploading', 'paused', 'verifying'].includes(phase);
  const resultReady = result?.state === 'complete' || result?.state === 'no_speech';

  return (
    <section
      className="mt-6 min-w-0 rounded-[2rem] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-7"
      aria-labelledby="upload-recording-heading"
    >
      <div className="flex min-w-0 flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-sm font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
            Upload recording
          </p>
          <h2 id="upload-recording-heading" className="mt-2 text-2xl font-semibold tracking-tight">
            Resume the upload, then follow the transcript to completion.
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted)]">
            WAV, MP3, M4A, OGG, WebM, or MP4. Recantor keeps resumable transfer progress, then uses
            server-side audio preparation and transcription. Processing progress, transcript
            results, and exports stay attached to the same durable upload access.
          </p>
        </div>
        <div className="min-w-36 shrink-0 rounded-2xl border border-[var(--border)] px-4 py-3 text-left sm:text-right">
          <p
            className="font-mono text-2xl font-semibold tabular-nums"
            data-testid="upload-progress"
          >
            {progress}%
          </p>
          <p
            className="mt-1 text-xs uppercase tracking-[0.14em] text-[var(--muted)]"
            data-testid="upload-phase"
          >
            {phaseLabels[phase]}
          </p>
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
          className="min-h-11 rounded-full border border-[var(--border)] px-4 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
        >
          Choose recording
        </button>
        <p className="mt-3 text-sm text-[var(--muted)]">or drop one file here</p>
        {file ? (
          <p className="mt-3 break-words text-sm font-medium" data-testid="upload-selected-file">
            {file.name} · {bytesLabel(file.size)}
          </p>
        ) : null}
      </div>

      <div
        className="mt-5 h-2 overflow-hidden rounded-full bg-[var(--background)]"
        role="progressbar"
        aria-label="Upload progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <div
          className="h-full bg-[var(--accent)] transition-[width]"
          style={{ width: `${progress}%` }}
        />
      </div>

      <p
        className="mt-4 text-sm leading-6 text-[var(--muted)]"
        data-testid="upload-message"
        role={['error', 'failed', 'expired'].includes(phase) ? 'alert' : 'status'}
        aria-live={['error', 'failed', 'expired'].includes(phase) ? undefined : 'polite'}
      >
        {message}
      </p>

      <div className="mt-5 flex flex-wrap gap-3">
        <button
          type="button"
          className="min-h-11 rounded-full bg-[var(--foreground)] px-5 py-2 text-sm font-semibold text-[var(--background)] disabled:opacity-50"
          onClick={() => void start()}
          disabled={!file || busy || resultReady}
          data-testid="upload-start"
        >
          {phase === 'error' || phase === 'expired' || phase === 'failed'
            ? 'Start fresh / resume'
            : 'Start / resume'}
        </button>
        <button
          type="button"
          className="min-h-11 rounded-full border border-[var(--border)] px-5 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={pause}
          disabled={phase !== 'uploading'}
          data-testid="upload-pause"
        >
          Pause
        </button>
        <button
          type="button"
          className="min-h-11 rounded-full border border-[var(--border)] px-5 py-2 text-sm font-semibold disabled:opacity-50"
          onClick={resume}
          disabled={phase !== 'paused' && phase !== 'error'}
          data-testid="upload-resume"
        >
          Retry / resume
        </button>
      </div>

      {resultReady && trackedRecovery?.sessionId ? (
        <div
          className="mt-7 rounded-2xl border border-[var(--border)] bg-[var(--background)] p-5"
          data-testid="upload-result"
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="text-sm font-semibold">Transcript result</p>
              <p className="mt-1 text-sm text-[var(--muted)]">
                {result?.transcript_segment_count ?? 0} canonical transcript segment
                {(result?.transcript_segment_count ?? 0) === 1 ? '' : 's'}.
              </p>
            </div>
            <div className="flex flex-wrap gap-2" aria-label="Transcript exports">
              {(['txt', 'json', 'vtt', 'srt'] as const).map((exportFormat) => (
                <button
                  key={exportFormat}
                  type="button"
                  className="min-h-10 rounded-full border border-[var(--border)] px-4 py-2 text-xs font-semibold uppercase tracking-[0.1em] disabled:opacity-50"
                  onClick={() => void exportTranscript(exportFormat)}
                  disabled={downloading !== null}
                  data-testid={`upload-export-${exportFormat}`}
                >
                  {downloading === exportFormat ? 'Downloading…' : exportFormat}
                </button>
              ))}
            </div>
          </div>

          {result?.state === 'no_speech' ? (
            <p className="mt-5 text-sm text-[var(--muted)]" data-testid="upload-no-speech">
              No speech was detected. Exports contain the truthful empty transcript result.
            </p>
          ) : transcriptPage ? (
            <>
              <TranscriptLog
                segments={transcriptPage.segments}
                ariaLabel="Uploaded recording transcript"
              />
              <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
                <span className="text-[var(--muted)]">
                  Showing up to {TRANSCRIPT_PAGE_SIZE} segments at a time.
                </span>
                {transcriptPage.after_sequence > 0 ? (
                  <button
                    type="button"
                    className="rounded-full border border-[var(--border)] px-3 py-2 font-semibold"
                    onClick={() => void loadTranscriptPage(trackedRecovery, 0)}
                    data-testid="upload-transcript-first-page"
                  >
                    Back to first page
                  </button>
                ) : null}
                {transcriptPage.has_more ? (
                  <button
                    type="button"
                    className="rounded-full border border-[var(--border)] px-3 py-2 font-semibold"
                    onClick={() =>
                      void loadTranscriptPage(trackedRecovery, transcriptPage.next_after_sequence)
                    }
                    data-testid="upload-transcript-next-page"
                  >
                    Next page
                  </button>
                ) : null}
              </div>
            </>
          ) : (
            <p className="mt-5 text-sm text-[var(--muted)]">Loading the canonical transcript…</p>
          )}
        </div>
      ) : null}
    </section>
  );
}
