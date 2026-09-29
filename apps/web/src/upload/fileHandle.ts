// Thin wrapper over the File System Access API. Only Chromium-based desktop browsers in a
// secure context expose it; every caller must keep a truthful reselect fallback.

type ReadDescriptor = { mode: 'read' };

type PersistentFileHandle = FileSystemFileHandle & {
  queryPermission?: (descriptor: ReadDescriptor) => Promise<PermissionState>;
  requestPermission?: (descriptor: ReadDescriptor) => Promise<PermissionState>;
};

type OpenFilePickerOptions = {
  multiple?: boolean;
  types?: Array<{ description?: string; accept: Record<string, string[]> }>;
};

type PickerWindow = Window & {
  showOpenFilePicker?: (options?: OpenFilePickerOptions) => Promise<FileSystemFileHandle[]>;
};

export type PickResult =
  | { kind: 'picked'; file: File; handle: FileSystemFileHandle }
  | { kind: 'cancelled' }
  | { kind: 'unsupported' }
  | { kind: 'error'; message: string };

export type ReacquireResult =
  | { status: 'ready'; file: File }
  | { status: 'needs_permission' }
  | { status: 'denied' }
  | { status: 'missing' }
  | { status: 'unsupported' }
  | { status: 'error'; message: string };

const RECORDING_TYPES: OpenFilePickerOptions['types'] = [
  {
    description: 'Audio or video recordings',
    accept: {
      'audio/*': ['.wav', '.mp3', '.m4a', '.ogg', '.webm'],
      'video/*': ['.mp4', '.webm'],
    },
  },
];

function pickerWindow(): PickerWindow {
  return window as unknown as PickerWindow;
}

export function supportsPersistentFileHandles(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext !== false &&
    typeof pickerWindow().showOpenFilePicker === 'function'
  );
}

// DOMException is not reliably `instanceof Error` across realms, so read the fields directly.
function errorName(error: unknown): string {
  const name = (error as { name?: unknown } | null)?.name;
  return typeof name === 'string' ? name : '';
}

function errorMessage(error: unknown): string {
  const message = (error as { message?: unknown } | null)?.message;
  return typeof message === 'string' && message ? message : 'unknown error';
}

export async function pickRecordingWithHandle(): Promise<PickResult> {
  if (!supportsPersistentFileHandles()) return { kind: 'unsupported' };
  try {
    const [handle] = await pickerWindow().showOpenFilePicker!({
      multiple: false,
      types: RECORDING_TYPES,
    });
    if (!handle) return { kind: 'cancelled' };
    return { kind: 'picked', handle, file: await handle.getFile() };
  } catch (error) {
    if (errorName(error) === 'AbortError') return { kind: 'cancelled' };
    return { kind: 'error', message: errorMessage(error) };
  }
}

/**
 * Reopen a persisted handle and read a fresh `File` snapshot.
 *
 * Permission is never requested unless `requestPermission` is true; that must only happen inside a
 * user gesture. A grant that did not survive the browser restart surfaces as `needs_permission`.
 */
export async function reacquireFile(
  handle: FileSystemFileHandle,
  options: { requestPermission: boolean },
): Promise<ReacquireResult> {
  const candidate = handle as PersistentFileHandle;
  if (typeof candidate.queryPermission !== 'function') return { status: 'unsupported' };
  try {
    let state = await candidate.queryPermission({ mode: 'read' });
    if (state === 'prompt' && options.requestPermission) {
      if (typeof candidate.requestPermission !== 'function') return { status: 'unsupported' };
      state = await candidate.requestPermission({ mode: 'read' });
    }
    if (state === 'prompt') return { status: 'needs_permission' };
    if (state !== 'granted') return { status: 'denied' };
    return { status: 'ready', file: await candidate.getFile() };
  } catch (error) {
    const name = errorName(error);
    if (name === 'NotFoundError') return { status: 'missing' };
    if (name === 'NotAllowedError' || name === 'SecurityError') return { status: 'denied' };
    return { status: 'error', message: errorMessage(error) };
  }
}
