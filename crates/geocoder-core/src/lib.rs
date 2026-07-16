//! Overture Geocoder Core
//!
//! Platform-agnostic geocoding engine using SQLite FTS5.
//! Supports both native and wasm32-unknown-unknown builds (rusqlite 0.38+).

pub mod database;
pub mod error;
pub mod geo;
pub mod query;
pub mod routing;
pub mod types;

pub use database::Database;
pub use error::{Error, Result};
pub use types::{
    BBox, DivisionRow, DivisionType, GeocoderQuery, GeocoderResult, IdLocatorMetadata,
    IdLookupResult, LocationBias, ReverseResult,
};
