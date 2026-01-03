//! Error types for the geocoder.

use thiserror::Error;

/// Result type alias for geocoder operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Geocoder error types.
#[derive(Debug, Error)]
pub enum Error {
    /// Database error.
    #[error("database error: {0}")]
    Database(String),

    /// Query preparation error.
    #[error("invalid query: {0}")]
    InvalidQuery(String),

    /// SQLite error (native builds).
    #[cfg(feature = "native")]
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    /// IO error.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// Serialization error.
    #[error("serialization error: {0}")]
    Serialization(#[from] serde_json::Error),
}
