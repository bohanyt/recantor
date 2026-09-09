const trimTrailingSlash = (value: string): string => value.replace(/\/+$/, '');

export const env = {
  appName: import.meta.env.VITE_APP_NAME || 'Recantor',
  apiBaseUrl: trimTrailingSlash(import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'),
};
