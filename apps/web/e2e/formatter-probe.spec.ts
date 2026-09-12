import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';

test('prints exact prettier delta for bounded safety files', () => {
  const files = ['src/RecorderPanel.tsx', 'e2e/safety-layout.spec.ts'];
  execFileSync('pnpm', ['exec', 'prettier', '--write', ...files], {
    cwd: process.cwd(),
    encoding: 'utf8',
  });
  const diff = execFileSync('git', ['diff', '--', ...files], {
    cwd: process.cwd(),
    encoding: 'utf8',
  });
  console.log(`FORMATTER_PROBE_START\n${diff}\nFORMATTER_PROBE_END`);
  expect(diff).not.toBe('');
});
