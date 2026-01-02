/**
 * DuckDB-WASM query utility for Overture S3 data
 *
 * Provides direct SQL queries against Overture Maps S3 data using DuckDB-WASM.
 * Used for nearby places/addresses lookups that aren't in the indexed D1 database.
 *
 * NOTE: Uses @duckdb/duckdb-wasm which is a transitive dependency via @bradrichardson/overturemaps.
 * Ideally, the overturemaps package would export a queryOverture function directly,
 * which would avoid the need for this separate module. This is a prototype implementation
 * that demonstrates the capability.
 *
 * TODO: Add queryOverture export to @bradrichardson/overturemaps package to share
 * the DuckDB instance and avoid potential duplicate initialization.
 */

// Types for DuckDB-WASM (allows optional dependency)
interface DuckDBModule {
  getJsDelivrBundles: () => unknown;
  selectBundle: (bundles: unknown) => Promise<{
    mainModule: string;
    mainWorker?: string;
    pthreadWorker?: string;
  }>;
  ConsoleLogger: new () => unknown;
  AsyncDuckDB: new (logger: unknown, worker: Worker) => AsyncDuckDB;
}

interface AsyncDuckDB {
  instantiate: (mainModule: string, pthreadWorker?: string) => Promise<void>;
  connect: () => Promise<AsyncDuckDBConnection>;
  terminate: () => Promise<void>;
}

interface AsyncDuckDBConnection {
  query: (sql: string) => Promise<QueryResult>;
  close: () => Promise<void>;
}

interface QueryResult {
  toArray: () => Array<Record<string, unknown>>;
}

let duckdb: DuckDBModule | null = null;
let db: AsyncDuckDB | null = null;
let conn: AsyncDuckDBConnection | null = null;
let initPromise: Promise<void> | null = null;
let latestRelease: string | null = null;
let duckdbUnavailable = false;

/**
 * Dynamically import DuckDB-WASM
 */
async function loadDuckDB(): Promise<DuckDBModule> {
  if (duckdb) return duckdb;
  if (duckdbUnavailable) {
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features."
    );
  }

  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const module = (await import("@duckdb/duckdb-wasm")) as any;
    duckdb = module as DuckDBModule;
    return duckdb;
  } catch {
    duckdbUnavailable = true;
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features: npm install @duckdb/duckdb-wasm"
    );
  }
}

/**
 * Initialize DuckDB-WASM with required extensions
 */
async function initDuckDB(): Promise<void> {
  if (db) return;

  const duckdbModule = await loadDuckDB();

  // Use CDN bundles for browser compatibility
  const JSDELIVR_BUNDLES = duckdbModule.getJsDelivrBundles();

  // Select bundle based on environment
  const bundle = await duckdbModule.selectBundle(JSDELIVR_BUNDLES);

  const worker_url = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker!}");`], {
      type: "text/javascript",
    })
  );

  const worker = new Worker(worker_url);
  const logger = new duckdbModule.ConsoleLogger();

  db = new duckdbModule.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);

  conn = await db.connect();

  // Configure for S3 access
  await conn.query(`
    INSTALL httpfs;
    LOAD httpfs;
    INSTALL spatial;
    LOAD spatial;
    SET s3_region = 'us-west-2';
  `);

  URL.revokeObjectURL(worker_url);
}

/**
 * Get the connection, initializing if needed
 */
async function getConnection(): Promise<AsyncDuckDBConnection> {
  if (!initPromise) {
    initPromise = initDuckDB();
  }
  await initPromise;
  return conn!;
}

/**
 * Get the latest Overture release version
 */
async function getOvertureRelease(): Promise<string> {
  if (latestRelease) return latestRelease;

  try {
    // Import dynamically to avoid circular dependency
    const { getLatestRelease } = await import("@bradrichardson/overturemaps");
    latestRelease = await getLatestRelease();
  } catch {
    // Fallback to a known release
    latestRelease = "2024-11-13.0";
  }

  return latestRelease;
}

/**
 * Execute a query against Overture S3 data
 *
 * @param sql SQL query with __LATEST__ placeholder for release version
 * @returns Array of result rows
 */
export async function queryOverture(
  sql: string
): Promise<Record<string, unknown>[]> {
  const conn = await getConnection();
  const release = await getOvertureRelease();

  // Replace __LATEST__ placeholder with actual release
  const query = sql.replace(/__LATEST__/g, release);

  try {
    const result = await conn.query(query);
    return result.toArray().map((row: Record<string, unknown>) => {
      const obj: Record<string, unknown> = {};
      for (const key of Object.keys(row)) {
        obj[key] = row[key];
      }
      return obj;
    });
  } catch (error) {
    throw new Error(
      `DuckDB query failed: ${error instanceof Error ? error.message : String(error)}`
    );
  }
}

/**
 * Close DuckDB connection and release resources
 */
export async function closeDuckDB(): Promise<void> {
  if (conn) {
    await conn.close();
    conn = null;
  }
  if (db) {
    await db.terminate();
    db = null;
  }
  initPromise = null;
}

/**
 * Check if DuckDB is available/initialized
 */
export function isDuckDBAvailable(): boolean {
  return db !== null;
}
