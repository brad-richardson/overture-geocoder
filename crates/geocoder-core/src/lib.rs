//! Overture Geocoder Core
//!
//! Platform-agnostic geocoding engine using SQLite FTS5.
//! Supports both native (rusqlite) and WASM builds.

pub mod error;
pub mod query;
pub mod types;

#[cfg(feature = "native")]
pub mod database;

pub use error::{Error, Result};
pub use types::{
    DivisionRow, DivisionType, GeocoderQuery, GeocoderResult, LocationBias, ReverseResult,
};

#[cfg(feature = "native")]
pub use database::Database;
