import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import { fetchHealth, fetchReadiness } from './api';

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
  beforeEach(() => {
    mockHealth.mockResolvedValue({ status: 'ok', service: 'recantor-api' });
    mockReadiness.mockResolvedValue({ status: 'ready', database: 'ok' });
  });

  it('shows independent API and database readiness states', async () => {
    renderApp();

    expect(await screen.findByTestId('health-state')).toHaveTextContent('Online');
    expect(await screen.findByTestId('readiness-state')).toHaveTextContent('Online');
    expect(screen.getByText(/without depending on PostgreSQL/i)).toBeInTheDocument();
  });
});
