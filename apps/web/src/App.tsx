import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { fetchHealth, fetchReadiness } from './api';
import { env } from './env';
import { RecorderPanel } from './RecorderPanel';
import { UploadPanel } from './UploadPanel';

type Workflow = 'live' | 'upload';

type ServiceStatusProps = {
  title: string;
  value: string;
  detail: string;
  state: 'checking' | 'online' | 'unavailable';
  testId: string;
};

function ServiceStatus({ title, value, detail, state, testId }: ServiceStatusProps) {
  return (
    <section className="min-w-0 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--muted)]">
            {title}
          </p>
          <p className="mt-1 font-semibold">{value}</p>
        </div>
        <span
          className="rounded-full border border-[var(--border)] px-2.5 py-1 text-xs font-semibold"
          data-testid={testId}
        >
          {state === 'checking' ? 'Checking' : state === 'online' ? 'Online' : 'Unavailable'}
        </span>
      </div>
      <p className="mt-2 break-words text-xs leading-5 text-[var(--muted)]">{detail}</p>
    </section>
  );
}

export default function App() {
  const [workflow, setWorkflow] = useState<Workflow>('live');
  const [liveCaptureActive, setLiveCaptureActive] = useState(false);
  const health = useQuery({
    queryKey: ['healthz'],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
    retry: 1,
  });
  const readiness = useQuery({
    queryKey: ['readyz'],
    queryFn: fetchReadiness,
    refetchInterval: 10_000,
    retry: 1,
  });

  const healthState: ServiceStatusProps['state'] = health.isPending
    ? 'checking'
    : health.isSuccess
      ? 'online'
      : 'unavailable';
  const readinessState: ServiceStatusProps['state'] = readiness.isPending
    ? 'checking'
    : readiness.isSuccess
      ? 'online'
      : 'unavailable';

  const selectWorkflow = (nextWorkflow: Workflow): void => {
    if (nextWorkflow === 'upload' && liveCaptureActive) return;
    setWorkflow(nextWorkflow);
  };

  const serviceDiagnostics = (
    <div>
      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--muted)]">
        Service diagnostics
      </p>
      <div className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2">
        <ServiceStatus
          title="API process"
          value="Liveness"
          detail={
            health.isSuccess
              ? `Service ${health.data.service} answered without depending on PostgreSQL.`
              : health.error?.message || 'Checking the API process.'
          }
          state={healthState}
          testId="health-state"
        />
        <ServiceStatus
          title="Durable state"
          value="PostgreSQL readiness"
          detail={
            readiness.isSuccess
              ? `Database probe returned ${readiness.data.database}.`
              : readiness.error?.message || 'Checking PostgreSQL connectivity.'
          }
          state={readinessState}
          testId="readiness-state"
        />
      </div>
    </div>
  );

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-[90rem] min-w-0 flex-col px-4 py-5 sm:px-6 lg:px-8">
      <header className="min-w-0 rounded-[2rem] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-6">
        <div className="flex min-w-0 flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0 max-w-3xl">
            <p className="text-sm font-semibold uppercase tracking-[0.2em] text-[var(--accent)]">
              Self-hosted recording alpha
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
              {env.appName}
            </h1>
            <p className="mt-3 text-sm leading-6 text-[var(--muted)] sm:text-base">
              Record live with archive-audio safety kept separate from transcription, or resume a
              durable upload of an existing recording.
            </p>
          </div>

          <div className="min-w-0">
            <nav
              className="grid w-full min-w-0 grid-cols-2 gap-2 rounded-2xl bg-[var(--surface-muted)] p-1.5 lg:w-auto lg:min-w-[22rem]"
              aria-label="Primary workflow"
            >
              <button
                type="button"
                className={`min-h-11 rounded-xl px-4 py-2.5 text-sm font-semibold ${
                  workflow === 'live'
                    ? 'bg-[var(--surface)] shadow-sm'
                    : 'text-[var(--muted)] hover:text-[var(--foreground)]'
                }`}
                aria-current={workflow === 'live' ? 'page' : undefined}
                onClick={() => selectWorkflow('live')}
                data-testid="workflow-live"
              >
                Live
              </button>
              <button
                type="button"
                className={`min-h-11 rounded-xl px-4 py-2.5 text-sm font-semibold ${
                  workflow === 'upload'
                    ? 'bg-[var(--surface)] shadow-sm'
                    : 'text-[var(--muted)] hover:text-[var(--foreground)]'
                } ${liveCaptureActive ? 'cursor-not-allowed opacity-60' : ''}`}
                aria-current={workflow === 'upload' ? 'page' : undefined}
                aria-disabled={liveCaptureActive ? 'true' : undefined}
                aria-describedby={liveCaptureActive ? 'active-recording-workflow-guard' : undefined}
                onClick={() => selectWorkflow('upload')}
                data-testid="workflow-upload"
              >
                Upload recording
              </button>
            </nav>
            {liveCaptureActive && (
              <p
                id="active-recording-workflow-guard"
                className="mt-2 max-w-[22rem] text-xs leading-5 text-[var(--danger)]"
                role="status"
                data-testid="active-recording-workflow-guard"
              >
                Recording is active. Stop the Live recording before opening Upload recording.
              </p>
            )}
          </div>
        </div>
      </header>

      <div hidden={workflow !== 'live'} data-testid="workflow-live-panel">
        <RecorderPanel
          serviceDiagnostics={serviceDiagnostics}
          onCaptureActivityChange={setLiveCaptureActive}
        />
      </div>

      <div hidden={workflow !== 'upload'} data-testid="workflow-upload-panel">
        <UploadPanel />
      </div>

      <footer className="mt-auto pt-8 text-xs leading-5 text-[var(--muted)]">
        Live archive capture and canonical transcript recovery are independent safety paths.
        Existing recording uploads are resumable; uploaded-media processing and exports are not
        available in this alpha yet.
      </footer>
    </main>
  );
}
