const trimTrailingSlash = (value: string): string => value.replace(/\/+$/, '');

function positiveInteger(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

export const env = {
  appName: import.meta.env.VITE_APP_NAME || 'Recantor',
  apiBaseUrl: trimTrailingSlash(import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'),
  recordingRequestTimeoutMs: positiveInteger(
    import.meta.env.VITE_RECORDING_REQUEST_TIMEOUT_MS,
    5_000,
  ),
};
