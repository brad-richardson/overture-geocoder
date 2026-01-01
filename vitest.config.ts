import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    testTimeout: 30000,  // Allow time for wrangler startup
    hookTimeout: 30000,
    pool: 'forks',       // Isolate test files
    include: ['tests/**/*.test.ts'],
    globalSetup: ['tests/e2e/globalSetup.ts'],
  },
});
