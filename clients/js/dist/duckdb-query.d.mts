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
/**
 * Execute a query against Overture S3 data
 *
 * @param sql SQL query with __LATEST__ placeholder for release version
 * @returns Array of result rows
 */
declare function queryOverture(sql: string): Promise<Record<string, unknown>[]>;
/**
 * Close DuckDB connection and release resources
 */
declare function closeDuckDB(): Promise<void>;
/**
 * Check if DuckDB is available/initialized
 */
declare function isDuckDBAvailable(): boolean;

export { closeDuckDB, isDuckDBAvailable, queryOverture };
