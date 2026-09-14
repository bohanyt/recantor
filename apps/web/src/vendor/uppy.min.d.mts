export type UploadProgress = {
  bytesUploaded: number;
  bytesTotal?: number | null;
};

export type UploadErrorResponse = {
  status?: number;
};

export class Uppy {
  constructor(options?: Record<string, unknown>);
  use(plugin: unknown, options?: Record<string, unknown>): this;
  on(
    event: 'upload-progress',
    callback: (file: unknown, progress: UploadProgress) => void,
  ): this;
  on(
    event: 'upload-error',
    callback: (file: unknown, error: Error, response?: UploadErrorResponse) => void,
  ): this;
  on(event: 'upload-success', callback: (file?: unknown, response?: unknown) => void): this;
  addFile(file: {
    name: string;
    type: string;
    data: File;
    meta?: Record<string, string>;
  }): string;
  upload(): Promise<unknown>;
  pauseAll(): void;
  resumeAll(): void;
  retryAll(): Promise<unknown>;
  destroy(): void;
}

export const Tus: unknown;
