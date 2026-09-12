import { test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

test('temporary product-shell formatter capture', () => {
  test.skip(process.env.CI !== 'true', 'Formatter capture only runs in CI.');
  const repositoryRoot = process.env.GITHUB_WORKSPACE ?? path.resolve(process.cwd(), '../..');
  execFileSync('pnpm', ['--dir', 'apps/web', 'exec', 'prettier', '--write', 'e2e/product-shell.spec.ts'], {
    cwd: repositoryRoot,
    encoding: 'utf8',
  });
  const diff = execFileSync(
    'git',
    ['diff', '--no-color', '--', 'apps/web/e2e/product-shell.spec.ts'],
    { cwd: repositoryRoot, encoding: 'utf8' },
  );
  console.log(`PRODUCT_SHELL_PRETTIER_DIFF_START\n${diff}\nPRODUCT_SHELL_PRETTIER_DIFF_END`);
});
