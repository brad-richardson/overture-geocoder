//! Geocoder CLI for testing.
//!
//! Usage:
//!     geocoder search "boston" --db indexes/divisions-global.db
//!     geocoder search "paris" --db indexes/divisions-global.db --limit 20
//!     geocoder search "london" --db indexes/US.db --country US

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
    geocoder count --db <path>
    geocoder help

Options:
    --db <path>       Path to SQLite database (default: indexes/divisions-global.db)
    --limit <n>       Maximum results (default: 10, max: 40)
    --country <code>  Country bias (ISO 3166-1 alpha-2, e.g., US, FR)
    --no-autocomplete Disable prefix matching on last token
    --json            Output as JSON

Examples:
    geocoder search "boston"
    geocoder search "paris" --country FR --limit 5
    geocoder search "lond" --json
"#
    );
}

fn cmd_search(args: &[String]) -> Result<()> {
    if args.is_empty() {
        bail!("Missing search query. Usage: geocoder search <query>");
    }

    let query_text = &args[0];
    let mut db_path = PathBuf::from("indexes/divisions-global.db");
    let mut limit = 10usize;
    let mut country: Option<String> = None;
    let mut autocomplete = true;
    let mut json_output = false;

    // Parse options
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--db" => {
                i += 1;
                if i >= args.len() {
                    bail!("--db requires a path argument");
                }
                db_path = PathBuf::from(&args[i]);
            }
            "--limit" => {
                i += 1;
                if i >= args.len() {
                    bail!("--limit requires a number argument");
                }
                limit = args[i].parse().context("Invalid limit")?;
            }
            "--country" => {
                i += 1;
                if i >= args.len() {
                    bail!("--country requires a country code argument");
                }
                country = Some(args[i].clone());
            }
            "--no-autocomplete" => {
                autocomplete = false;
            }
            "--json" => {
                json_output = true;
            }
            other => bail!("Unknown option: {}", other),
        }
        i += 1;
    }

    // Open database
    let db = Database::open(&db_path)
        .with_context(|| format!("Failed to open database: {}", db_path.display()))?;

    // Build query
    let bias = match country {
        Some(code) => LocationBias::Country(code),
        None => LocationBias::None,
    };

    let query = GeocoderQuery::new(query_text)
        .with_limit(limit)
        .with_autocomplete(autocomplete)
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
    results.truncate(limit);

    // Output results
    if json_output {
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

fn cmd_count(args: &[String]) -> Result<()> {
    let mut db_path = PathBuf::from("indexes/divisions-global.db");

    // Parse options
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--db" => {
                i += 1;
                if i >= args.len() {
                    bail!("--db requires a path argument");
                }
                db_path = PathBuf::from(&args[i]);
            }
            other => bail!("Unknown option: {}", other),
        }
        i += 1;
    }

    let db = Database::open(&db_path)
        .with_context(|| format!("Failed to open database: {}", db_path.display()))?;

    let count = db.count()?;
    println!("Record count: {}", count);

    if let Some(release) = db.get_metadata("overture_release")? {
        println!("Overture release: {}", release);
    }

    Ok(())
}
