/**
 * Global setup for E2E tests - starts wrangler once for all tests.
 */

import { startWorker, stopWorker, getBaseUrl } from './setup';

export async function setup() {
  console.log('Starting wrangler for E2E tests...');
  const url = await startWorker();
  console.log(`Worker ready at ${url}`);

  // Store URL for tests to access
  (globalThis as Record<string, unknown>).__TEST_BASE_URL__ = url;
}

export async function teardown() {
  console.log('Stopping wrangler...');
  await stopWorker();
}
