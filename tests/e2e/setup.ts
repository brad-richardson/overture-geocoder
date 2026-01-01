/**
 * E2E Test Setup - Spawns wrangler dev and waits for it to be ready.
 */

import { spawn, ChildProcess } from 'child_process';
import { copyFileSync, existsSync, mkdirSync, rmSync } from 'fs';
import { join } from 'path';

const TEST_PORT = 9876;
const STARTUP_TIMEOUT = 15000;

// D1 database location for wrangler local dev
const D1_STATE_DIR = '.wrangler/state/v3/d1/miniflare-D1DatabaseObject';

// Database file hashes (determined by wrangler/miniflare from binding names)
const D1_DB_FILES = {
  DB_MA: 'd3e041905f05e515c70eeff7a19bb719b7fd5943de6f9e0a4f0dad6a65e8bfec.sqlite',
  DB_DIVISIONS: '6a593fa526311561b192f28ff582309b09b9be3c2857186ee19b9b2a1255904b.sqlite',
};

let workerProcess: ChildProcess | null = null;

/**
 * Copy a database file and clean up WAL files.
 */
function copyDatabase(sourceFile: string, targetDir: string, targetFile: string): void {
  const targetPath = join(targetDir, targetFile);

  // Remove any existing WAL files
  const walFile = targetPath + '-wal';
  const shmFile = targetPath + '-shm';
  if (existsSync(walFile)) rmSync(walFile);
  if (existsSync(shmFile)) rmSync(shmFile);

  // Copy fixture to D1 location
  copyFileSync(sourceFile, targetPath);
}

/**
 * Copy test fixtures to wrangler D1 locations.
 */
function setupTestDatabases(): void {
  const fixturesDir = join(process.cwd(), 'tests/fixtures');
  const targetDir = join(process.cwd(), D1_STATE_DIR);

  // Check fixtures exist
  const featuresFixture = join(fixturesDir, 'test.db');
  const divisionsFixture = join(fixturesDir, 'divisions.db');

  if (!existsSync(featuresFixture)) {
    throw new Error(
      `Test fixture not found: ${featuresFixture}\n` +
      'Run: python scripts/create_test_fixture.py'
    );
  }

  if (!existsSync(divisionsFixture)) {
    throw new Error(
      `Test fixture not found: ${divisionsFixture}\n` +
      'Run: python scripts/create_test_fixture.py'
    );
  }

  // Create D1 state directory if it doesn't exist
  mkdirSync(targetDir, { recursive: true });

  // Copy both fixtures
  copyDatabase(featuresFixture, targetDir, D1_DB_FILES.DB_MA);
  copyDatabase(divisionsFixture, targetDir, D1_DB_FILES.DB_DIVISIONS);
}

/**
 * Start wrangler dev and wait for it to be ready.
 */
export async function startWorker(): Promise<string> {
  // Setup test databases
  setupTestDatabases();

  const baseUrl = `http://localhost:${TEST_PORT}`;

  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      if (workerProcess) {
        workerProcess.kill();
        workerProcess = null;
      }
      reject(new Error(`Wrangler startup timeout after ${STARTUP_TIMEOUT}ms`));
    }, STARTUP_TIMEOUT);

    workerProcess = spawn('npx', ['wrangler', 'dev', '--local', '--port', String(TEST_PORT)], {
      cwd: process.cwd(),
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, FORCE_COLOR: '0' },
    });

    let output = '';

    workerProcess.stdout?.on('data', (data: Buffer) => {
      const text = data.toString();
      output += text;

      // Look for ready message
      if (text.includes('Ready on') || text.includes(`localhost:${TEST_PORT}`)) {
        clearTimeout(timeout);
        // Give it a moment to fully initialize
        setTimeout(() => resolve(baseUrl), 500);
      }
    });

    workerProcess.stderr?.on('data', (data: Buffer) => {
      output += data.toString();
    });

    workerProcess.on('error', (err) => {
      clearTimeout(timeout);
      reject(new Error(`Failed to start wrangler: ${err.message}\n${output}`));
    });

    workerProcess.on('exit', (code) => {
      if (code !== null && code !== 0) {
        clearTimeout(timeout);
        reject(new Error(`Wrangler exited with code ${code}\n${output}`));
      }
    });
  });
}

/**
 * Stop the worker process.
 */
export async function stopWorker(): Promise<void> {
  if (workerProcess) {
    workerProcess.kill('SIGTERM');

    // Wait for process to exit
    await new Promise<void>((resolve) => {
      if (!workerProcess) {
        resolve();
        return;
      }

      const timeout = setTimeout(() => {
        workerProcess?.kill('SIGKILL');
        resolve();
      }, 5000);

      workerProcess.on('exit', () => {
        clearTimeout(timeout);
        resolve();
      });
    });

    workerProcess = null;
  }
}

/**
 * Get the base URL for the running worker.
 */
export function getBaseUrl(): string {
  return `http://localhost:${TEST_PORT}`;
}
