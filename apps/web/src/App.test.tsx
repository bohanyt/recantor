import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import { fetchHealth, fetchReadiness } from './api';
import { recorderDb } from './recorder/db';

vi.mock('./api', () => ({
  fetchHealth: vi.fn(),
  fetchReadiness: vi.fn(),
}));

const mockHealth = vi.mocked(fetchHealth);
const mockReadiness = vi.mocked(fetchReadiness);

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

describe('App product shell', () => {
  beforeEach(async () => {
    mockHealth.mockResolvedValue({ status: 'ok', service: 'recantor-api' });
    mockReadiness.mockResolvedValue({ status: 'ready', database: 'ok' });
    await recorderDb.open();
    await recorderDb.transaction('rw', recorderDb.sessions, recorderDb.chunks, async () => {
      await recorderDb.sessions.clear();
      await recorderDb.chunks.clear();
    });
  });

  it('defaults to Live, keeps Upload as an explicit workflow, and removes stale phase copy', async () => {
    renderApp();

    expect(await screen.findByTestId('start-recording')).toBeVisible();
    expect(screen.getByTestId('workflow-live')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('workflow-live-panel')).toBeVisible();
    expect(screen.getByTestId('workflow-upload-panel')).not.toBeVisible();
    expect(screen.queryByText(/Phase 1 reliable capture/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/durable audio first, intelligence second/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('workflow-upload'));
    expect(screen.getByTestId('workflow-upload')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('workflow-upload-panel')).toBeVisible();
    expect(screen.getByTestId('workflow-live-panel')).not.toBeVisible();
    expect(screen.getByText(/processing is not available in this alpha yet/i)).toBeVisible();
  });

  it('keeps service internals inside collapsed read-only diagnostics', async () => {
    renderApp();
    const diagnostics = await screen.findByTestId('diagnostics');

    expect(diagnostics).not.toHaveAttribute('open');
    await waitFor(() => expect(screen.getByTestId('health-state')).toHaveTextContent('Online'));
    await waitFor(() => expect(screen.getByTestId('readiness-state')).toHaveTextContent('Online'));
    expect(screen.getByTestId('health-state')).not.toBeVisible();
    expect(screen.getByTestId('readiness-state')).not.toBeVisible();

    fireEvent.click(screen.getByText('Advanced / Diagnostics'));
    expect(diagnostics).toHaveAttribute('open');
    expect(screen.getByTestId('health-state')).toBeVisible();
    expect(screen.getByText(/without depending on PostgreSQL/i)).toBeVisible();
  });
});
