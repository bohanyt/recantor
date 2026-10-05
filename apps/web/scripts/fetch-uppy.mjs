import { mkdir, stat, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SOURCE = 'https://releases.transloadit.com/uppy/v6.0.0/uppy.min.mjs';
const here = dirname(fileURLToPath(import.meta.url));
const destination = resolve(here, '../src/vendor/uppy.min.mjs');

try {
  const existing = await stat(destination);
  if (existing.size > 100_000) process.exit(0);
} catch {
  // First build in a clean checkout: fetch the version-pinned upstream bundle below.
}

const response = await fetch(SOURCE, { redirect: 'follow' });
if (!response.ok) {
  throw new Error(`Unable to fetch Uppy 6.0.0 bundle: HTTP ${response.status}`);
}
const bytes = new Uint8Array(await response.arrayBuffer());
if (bytes.byteLength < 100_000) {
  throw new Error(`Uppy bundle is unexpectedly small: ${bytes.byteLength} bytes`);
}
const prefix = new TextDecoder().decode(bytes.slice(0, 256)).trimStart();
if (prefix.startsWith('<')) {
  throw new Error('Pinned Uppy URL returned HTML instead of the ESM bundle');
}
await mkdir(dirname(destination), { recursive: true });
await writeFile(destination, bytes);
console.log(`Fetched pinned Uppy 6.0.0 bundle (${bytes.byteLength} bytes).`);
