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
const D1_DB_FILE = 'd3e041905f05e515c70eeff7a19bb719b7fd5943de6f9e0a4f0dad6a65e8bfec.sqlite';

let workerProcess: ChildProcess | null = null;

/**
 * Copy test fixture to wrangler D1 location.
 */
function setupTestDatabase(): void {
  const fixtureDb = join(process.cwd(), 'tests/fixtures/test.db');
  const targetDir = join(process.cwd(), D1_STATE_DIR);
  const targetDb = join(targetDir, D1_DB_FILE);

  if (!existsSync(fixtureDb)) {
    throw new Error(
      `Test fixture not found: ${fixtureDb}\n` +
      'Run: python scripts/create_test_fixture.py'
    );
  }

  // Create D1 state directory if it doesn't exist
  mkdirSync(targetDir, { recursive: true });

  // Remove any existing WAL files
  const walFile = targetDb + '-wal';
  const shmFile = targetDb + '-shm';
  if (existsSync(walFile)) rmSync(walFile);
  if (existsSync(shmFile)) rmSync(shmFile);

  // Copy fixture to D1 location
  copyFileSync(fixtureDb, targetDb);
}

/**
 * Start wrangler dev and wait for it to be ready.
 */
export async function startWorker(): Promise<string> {
  // Setup test database
  setupTestDatabase();

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
