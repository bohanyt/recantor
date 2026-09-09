import { env } from '../env';
import { client } from './generated/client.gen';
import { healthz, readyz } from './generated/sdk.gen';

client.setConfig({ baseUrl: env.apiBaseUrl });

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  return 'request failed';
}

export async function fetchHealth() {
  const result = await healthz();
  if (result.error) {
    throw new Error(errorMessage(result.error));
  }
  if (!result.data) {
    throw new Error('health response contained no data');
  }
  return result.data;
}

export async function fetchReadiness() {
  const result = await readyz();
  if (result.error) {
    throw new Error(errorMessage(result.error));
  }
  if (!result.data) {
    throw new Error('readiness response contained no data');
  }
  return result.data;
}
