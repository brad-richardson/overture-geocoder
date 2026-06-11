//! Geocoder CLI for testing.
//!
//! Usage:
//!     geocoder search "boston" --db indexes/divisions-global.db
//!     geocoder search "paris" --db indexes/divisions-global.db --limit 20
//!     geocoder search "london" --db indexes/US.db --country US
//!     geocoder reverse 42.36 -71.06 --db indexes/reverse/US.db

use std::env;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use geocoder_core::{Database, GeocoderQuery, LocationBias};

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();

    if args.len() < 2 {
        print_usage();
        return Ok(());
    }

    match args[1].as_str() {
        "search" => cmd_search(&args[2..])?,
        "reverse" => cmd_reverse(&args[2..])?,
        "count" => cmd_count(&args[2..])?,
        "help" | "--help" | "-h" => print_usage(),
        cmd => bail!("Unknown command: {}. Use 'help' for usage.", cmd),
    }

    Ok(())
}

fn print_usage() {
    println!(
        r#"Overture Geocoder CLI

Usage:
    geocoder search <query> [options]
    geocoder reverse <lat> <lon> [options]
    geocoder count --db <path>
    geocoder help

Options:
    --db <path>       Path to SQLite database (default: indexes/divisions-global.db)
    --limit <n>       Maximum results (default: 10, max: 40)
    --country <code>  Country bias (ISO 3166-1 alpha-2, e.g., US, FR)
    --lat <deg>       Latitude for coordinate bias (search only; requires --lon)
    --lon <deg>       Longitude for coordinate bias (search only; requires --lat)
    --no-autocomplete Disable prefix matching on last token
    --json            Output as JSON

Examples:
    geocoder search "boston"
    geocoder search "paris" --country FR --limit 5
    geocoder search "spring" --lat 42.1 --lon -72.6
    geocoder reverse 42.36 -71.06 --db indexes/reverse/US.db
    geocoder search "lond" --json
"#
    );
}

/// Parse `--flag value` style options; returns (positional args, parsed flags).
struct Options {
    db_path: PathBuf,
    limit: usize,
    country: Option<String>,
    bias_lat: Option<f64>,
    bias_lon: Option<f64>,
    autocomplete: bool,
    json_output: bool,
    positional: Vec<String>,
}

fn parse_options(args: &[String]) -> Result<Options> {
    let mut opts = Options {
        db_path: PathBuf::from("indexes/divisions-global.db"),
        limit: 10,
        country: None,
        bias_lat: None,
        bias_lon: None,
        autocomplete: true,
        json_output: false,
        positional: Vec::new(),
    };

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--db" => {
                i += 1;
                let v = args.get(i).context("--db requires a path argument")?;
                opts.db_path = PathBuf::from(v);
            }
            "--limit" => {
                i += 1;
                let v = args.get(i).context("--limit requires a number argument")?;
                opts.limit = v.parse().context("Invalid limit")?;
            }
            "--country" => {
                i += 1;
                let v = args
                    .get(i)
                    .context("--country requires a country code argument")?;
                opts.country = Some(v.clone());
            }
            "--lat" => {
                i += 1;
                let v = args.get(i).context("--lat requires a number argument")?;
                opts.bias_lat = Some(v.parse().context("Invalid --lat")?);
            }
            "--lon" => {
                i += 1;
                let v = args.get(i).context("--lon requires a number argument")?;
                opts.bias_lon = Some(v.parse().context("Invalid --lon")?);
            }
            "--no-autocomplete" => opts.autocomplete = false,
            "--json" => opts.json_output = true,
            other if other.starts_with("--") => bail!("Unknown option: {}", other),
            positional => opts.positional.push(positional.to_string()),
        }
        i += 1;
    }

    Ok(opts)
}

fn cmd_search(args: &[String]) -> Result<()> {
    let opts = parse_options(args)?;
    let [query_text] = opts.positional.as_slice() else {
        bail!("Usage: geocoder search <query> [options]");
    };

    let db = Database::open(&opts.db_path)
        .with_context(|| format!("Failed to open database: {}", opts.db_path.display()))?;

    let bias = match (opts.country, opts.bias_lat, opts.bias_lon) {
        (Some(country), Some(lat), Some(lon)) => LocationBias::Full { country, lat, lon },
        (Some(country), None, None) => LocationBias::Country(country),
        (None, Some(lat), Some(lon)) => LocationBias::Coordinates { lat, lon },
        (_, Some(_), None) | (_, None, Some(_)) => {
            bail!("--lat and --lon must be provided together")
        }
        (None, None, None) => LocationBias::None,
    };

    let query = GeocoderQuery::new(query_text)
        .with_limit(opts.limit)
        .with_autocomplete(opts.autocomplete)
        .with_bias(bias.clone());

    // Execute search (returns more results than limit to allow bias to elevate)
    let mut results = db.search(&query)?;

    // Apply exact match bonus (helps "Paris" rank above "Parish")
    geocoder_core::query::apply_exact_match_bonus(&mut results, query_text);

    // Apply location bias (re-ranks results)
    if !matches!(bias, LocationBias::None) {
        geocoder_core::query::apply_location_bias(&mut results, &bias);
    }

    // Truncate to requested limit after bias is applied
    results.truncate(opts.limit);

    // Importance is unclamped through the ranking pipeline; clamp for output.
    for r in &mut results {
        r.importance = r.importance.clamp(0.0, 1.0);
    }

    if opts.json_output {
        println!("{}", serde_json::to_string_pretty(&results)?);
    } else {
        println!("Results for '{}' ({})", query_text, results.len());
        println!("{}", "-".repeat(60));

        for (i, r) in results.iter().enumerate() {
            let pop = r
                .population
                .map(|p| format!(", pop={}", p))
                .unwrap_or_default();
            let country = r.country.as_deref().unwrap_or("??");

            println!(
                "{:2}. [{:12}] {} ({}){}",
                i + 1,
                r.division_type,
                r.primary_name,
                country,
                pop
            );
            println!(
                "    importance={:.3}, lat={:.4}, lon={:.4}",
                r.importance, r.lat, r.lon
            );
        }
    }

    Ok(())
}

fn cmd_reverse(args: &[String]) -> Result<()> {
    let opts = parse_options(args)?;
    let [lat, lon] = opts.positional.as_slice() else {
        bail!("Usage: geocoder reverse <lat> <lon> [options]");
    };
    let lat: f64 = lat.parse().context("Invalid latitude")?;
    let lon: f64 = lon.parse().context("Invalid longitude")?;
    if !(-90.0..=90.0).contains(&lat) {
        bail!("lat must be between -90 and 90");
    }
    if !(-180.0..=180.0).contains(&lon) {
        bail!("lon must be between -180 and 180");
    }

    let db = Database::open(&opts.db_path)
        .with_context(|| format!("Failed to open database: {}", opts.db_path.display()))?;

    let result = db.reverse_geocode(lat, lon)?;

    match result {
        None => {
            if opts.json_output {
                println!("null");
            } else {
                println!("No result for ({}, {})", lat, lon);
            }
        }
        Some(r) => {
            if opts.json_output {
                println!("{}", serde_json::to_string_pretty(&r)?);
            } else {
                println!(
                    "[{:12}] {} (confidence={}, {:.2} km from centroid)",
                    r.subtype, r.primary_name, r.confidence, r.distance_km
                );
                for h in &r.hierarchy {
                    println!("    {:12} {} ({})", h.subtype, h.name, h.gers_id);
                }
            }
        }
    }

    Ok(())
}

fn cmd_count(args: &[String]) -> Result<()> {
    let opts = parse_options(args)?;

    let db = Database::open(&opts.db_path)
        .with_context(|| format!("Failed to open database: {}", opts.db_path.display()))?;

    let count = db.count()?;
    println!("Record count: {}", count);

    if let Some(release) = db.get_metadata("overture_release")? {
        println!("Overture release: {}", release);
    }

    Ok(())
}
