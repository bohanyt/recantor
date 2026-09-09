import { useQuery } from '@tanstack/react-query';

import { fetchHealth, fetchReadiness } from './api';
import { env } from './env';

type StatusCardProps = {
  title: string;
  label: string;
  detail: string;
  state: 'loading' | 'online' | 'offline';
  testId: string;
};

function StatusCard({ title, label, detail, state, testId }: StatusCardProps) {
  return (
    <section className="rounded-3xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-medium text-[var(--muted)]">{title}</p>
          <p className="mt-1 text-lg font-semibold">{label}</p>
        </div>
        <span
          data-testid={testId}
          className="rounded-full border border-[var(--border)] px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em]"
        >
          {state === 'loading' ? 'Checking' : state === 'online' ? 'Online' : 'Unavailable'}
        </span>
      </div>
      <p className="mt-4 text-sm leading-6 text-[var(--muted)]">{detail}</p>
    </section>
  );
}

export default function App() {
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

  const healthState: StatusCardProps['state'] = health.isPending
    ? 'loading'
    : health.isSuccess
      ? 'online'
      : 'offline';
  const readinessState: StatusCardProps['state'] = readiness.isPending
    ? 'loading'
    : readiness.isSuccess
      ? 'online'
      : 'offline';

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-5 py-8 sm:px-8 sm:py-12">
      <header className="max-w-2xl">
        <p className="text-sm font-semibold uppercase tracking-[0.2em] text-[var(--accent)]">
          Phase 0 foundation
        </p>
        <h1 className="mt-3 text-4xl font-semibold tracking-tight sm:text-5xl">{env.appName}</h1>
        <p className="mt-4 text-base leading-7 text-[var(--muted)] sm:text-lg">
          Reliable capture comes first. This shell currently proves the browser-to-API, PostgreSQL,
          generated-contract, and deployment foundations before recording code is introduced.
        </p>
      </header>

      <div className="mt-10 grid gap-4 md:grid-cols-2">
        <StatusCard
          title="API process"
          label="Liveness"
          detail={
            health.isSuccess
              ? `Service ${health.data.service} answered without depending on PostgreSQL.`
              : health.error?.message || 'Checking the API process.'
          }
          state={healthState}
          testId="health-state"
        />
        <StatusCard
          title="Durable state"
          label="PostgreSQL readiness"
          detail={
            readiness.isSuccess
              ? `Database probe returned ${readiness.data.database}.`
              : readiness.error?.message || 'Checking PostgreSQL connectivity.'
          }
          state={readinessState}
          testId="readiness-state"
        />
      </div>

      <footer className="mt-auto pt-12 text-sm text-[var(--muted)]">
        No recording, STT, diarization, or LLM capability is claimed in this phase.
      </footer>
    </main>
  );
}
