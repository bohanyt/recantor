import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
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

describe('App', () => {
  beforeEach(async () => {
    mockHealth.mockResolvedValue({ status: 'ok', service: 'recantor-api' });
    mockReadiness.mockResolvedValue({ status: 'ready', database: 'ok' });
    await recorderDb.open();
    await recorderDb.transaction('rw', recorderDb.sessions, recorderDb.chunks, async () => {
      await recorderDb.sessions.clear();
      await recorderDb.chunks.clear();
    });
  });

  it('shows the recorder plus independent API and database readiness states', async () => {
    renderApp();

    expect(await screen.findByTestId('start-recording')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('health-state')).toHaveTextContent('Online'));
    await waitFor(() => expect(screen.getByTestId('readiness-state')).toHaveTextContent('Online'));
    expect(screen.getByText(/without depending on PostgreSQL/i)).toBeInTheDocument();
  });
});
