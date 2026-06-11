//! Error types for the geocoder.

use thiserror::Error;

/// Result type alias for geocoder operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Geocoder error types.
#[derive(Debug, Error)]
pub enum Error {
    /// SQLite error.
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    /// IO error.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// Serialization error.
    #[error("serialization error: {0}")]
    Serialization(#[from] serde_json::Error),
}
