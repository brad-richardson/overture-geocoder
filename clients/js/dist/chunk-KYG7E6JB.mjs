// src/duckdb-query.ts
var duckdb = null;
var db = null;
var conn = null;
var initPromise = null;
var latestRelease = null;
var duckdbUnavailable = false;
async function loadDuckDB() {
  if (duckdb) return duckdb;
  if (duckdbUnavailable) {
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features."
    );
  }
  try {
    const module = await import("./duckdb-node-KLXLGZSD.mjs");
    duckdb = module;
    return duckdb;
  } catch {
    duckdbUnavailable = true;
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features: npm install @duckdb/duckdb-wasm"
    );
  }
}
async function initDuckDB() {
  if (db) return;
  const duckdbModule = await loadDuckDB();
  const JSDELIVR_BUNDLES = duckdbModule.getJsDelivrBundles();
  const bundle = await duckdbModule.selectBundle(JSDELIVR_BUNDLES);
  const worker_url = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], {
      type: "text/javascript"
    })
  );
  const worker = new Worker(worker_url);
  const logger = new duckdbModule.ConsoleLogger();
  db = new duckdbModule.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  conn = await db.connect();
  await conn.query(`
    INSTALL httpfs;
    LOAD httpfs;
    INSTALL spatial;
    LOAD spatial;
    SET s3_region = 'us-west-2';
  `);
  URL.revokeObjectURL(worker_url);
}
async function getConnection() {
  if (!initPromise) {
    initPromise = initDuckDB();
  }
  await initPromise;
  return conn;
}
async function getOvertureRelease() {
  if (latestRelease) return latestRelease;
  try {
    const { getLatestRelease } = await import("@bradrichardson/overturemaps");
    latestRelease = await getLatestRelease();
  } catch {
    latestRelease = "2024-11-13.0";
  }
  return latestRelease;
}
async function queryOverture(sql) {
  const conn2 = await getConnection();
  const release = await getOvertureRelease();
  const query = sql.replace(/__LATEST__/g, release);
  try {
    const result = await conn2.query(query);
    return result.toArray().map((row) => {
      const obj = {};
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
async function closeDuckDB() {
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
function isDuckDBAvailable() {
  return db !== null;
}

export {
  queryOverture,
  closeDuckDB,
  isDuckDBAvailable
};
