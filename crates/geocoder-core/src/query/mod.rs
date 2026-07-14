//! Query preparation and execution.

mod bias;
mod fts;
mod merge;

pub use bias::apply_location_bias;
pub use fts::prepare_fts_query;
pub use merge::merge_results;

// =============================================================================
// Scoring constants
// =============================================================================

/// Weight of precomputed static importance (0-1) in the composed score.
/// The static term spans 0..0.5, so importance CAN outvote adjacent ladder
/// steps (which differ by ~0.1) — that is deliberate, letting a famous
/// prefix match beat an obscure near-exact one. What it can never outvote
/// is the full ladder range: a zero-quality match tops out at 0.5 + the
/// bm25 tiebreaker and cannot beat an exact match (1.0+).
pub const STATIC_IMPORTANCE_WEIGHT: f64 = 0.5;

/// Weight of the per-shard-normalized BM25 score in the composed score.
/// Deliberately small: raw BM25 embeds per-shard IDF/doc-length statistics
/// and is not comparable across shards, so it acts as a tiebreaker only.
pub const BM25_TIEBREAK_WEIGHT: f64 = 0.2;

/// Multiplier for the natural log of population in boosted score calculation.
/// Higher values increase the ranking advantage of high-population places.
/// With 2.0, a city with 1M population gets ~27.6 points of boost (ln(1M) * 2.0).
pub const POPULATION_BOOST_MULTIPLIER: f64 = 2.0;

/// Penalty applied to places with no population data.
/// This gives a slight advantage to places with known population over unknowns.
/// Set to match the boost a place with population=1 would receive (ln(2) * 2.0 ≈ 1.4).
pub const MISSING_POPULATION_PENALTY: f64 = 2.0;

/// SQL query for searching divisions in shards that carry a precomputed
/// `importance` column (static prominence: type prior + wikidata + capped
/// population) and the three-column FTS index
/// (`search_name`, `search_alias`, `search_context`).
///
/// The bm25() column weights (name 4.0, alias 2.0, context 1.0) make a
/// primary-name hit outrank an alias hit, which outranks a context (parent
/// region/country) hit. The ORDER BY blends text relevance with static
/// importance so high-prominence matches can't be cut off by the fetch
/// LIMIT; the final ordering is recomputed in Rust (composed score).
pub const SEARCH_DIVISIONS_SQL_WEIGHTED: &str = r#"
    SELECT d.rowid, d.gers_id, d.type, d.primary_name, d.lat, d.lon,
           d.bbox_xmin, d.bbox_ymin, d.bbox_xmax, d.bbox_ymax,
           d.population, d.country, d.region, d.importance,
           bm25(divisions_fts, 4.0, 2.0, 1.0) AS text_score,
           d.search_name
    FROM divisions_fts JOIN divisions d ON divisions_fts.rowid = d.rowid
    WHERE divisions_fts MATCH ?1
    ORDER BY text_score - 5.0 * d.importance
    LIMIT ?2
"#;

/// SQL query for searching divisions, with the population boost applied
/// in SQL so `ORDER BY ... LIMIT` ranks on the final score. Computing the
/// boost after the LIMIT (the old approach) could permanently drop a
/// high-population match that raw BM25 ranked below the fetch window.
///
/// Requires SQLite built with math functions (`ln`); see
/// [`SEARCH_DIVISIONS_SQL_NO_MATH`] for the fallback.
pub const SEARCH_DIVISIONS_SQL: &str = r#"
    SELECT
        d.rowid,
        d.gers_id,
        d.type,
        d.primary_name,
        d.lat,
        d.lon,
        d.bbox_xmin,
        d.bbox_ymin,
        d.bbox_xmax,
        d.bbox_ymax,
        d.population,
        d.country,
        d.region,
        bm25(divisions_fts)
            - CASE
                WHEN d.population > 0 THEN ln(d.population + 1.0) * 2.0
                ELSE 2.0
              END as boosted_score
    FROM divisions_fts
    JOIN divisions d ON divisions_fts.rowid = d.rowid
    WHERE divisions_fts MATCH ?1
    ORDER BY boosted_score
    LIMIT ?2
"#;

/// Calculate boosted score from BM25 and population.
/// Lower score = better match.
pub fn calculate_boosted_score(bm25_score: f64, population: Option<i64>) -> f64 {
    match population {
        Some(pop) if pop > 0 => {
            bm25_score - ((pop as f64 + 1.0).ln() * POPULATION_BOOST_MULTIPLIER)
        }
        _ => bm25_score - MISSING_POPULATION_PENALTY,
    }
}

/// Match quality between the query and a result's display name
/// (Photon's `QueryReranker` ladder).
///
/// Compares the lowercased, comma-truncated display name (the part of
/// `primary_name` before the first comma, so "Boston, MA" compares as
/// "boston") against the lowercased query:
///
/// - exact equality: **1.0**
/// - query is a prefix of the name ending at a word boundary: **0.9**
///   ("paris" vs "Paris Township")
/// - bare prefix: **0.8** ("paris" vs "Parisville")
/// - otherwise: `0.8 * (longest common prefix chars / query chars)`
///
/// Both sides get a cheap one-to-one fold of common Latin diacritics so
/// "Zürich" matches "zurich". Multi-char foldings (e.g. ß → ss) are not
/// applied; old shards porter-stem the index but `primary_name` is raw, so
/// this comparison is best-effort for such names.
fn normalize_for_match(s: &str) -> String {
    s.trim()
        .chars()
        .flat_map(char::to_lowercase)
        .map(fold_latin_diacritic)
        .collect()
}

/// A query pre-normalized for match-quality checks.
///
/// Normalization (lowercase + diacritic fold), comma truncation, and the
/// padded forms used for word-boundary checks are computed once per query
/// instead of once per candidate row — the scoring loop runs over hundreds
/// of rows per shard.
///
/// The query is comma-truncated like display names are: users qualify
/// queries the same way names are written ("Boston, MA"), and without the
/// truncation such queries could never reach the exact/prefix rungs.
pub struct NormalizedQuery {
    text: String,
    /// `" {text} "` — whole-word containment checks.
    padded: String,
    /// `" {text}"` — word-prefix containment checks.
    prefixed: String,
    chars: usize,
}

impl NormalizedQuery {
    pub fn new(query: &str) -> Self {
        let text = normalize_for_match(query.split(',').next().unwrap_or(""));
        let padded = format!(" {text} ");
        let prefixed = format!(" {text}");
        let chars = text.chars().count();
        Self {
            text,
            padded,
            prefixed,
            chars,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }
}

/// Match quality against alternate/translated names (the `search_name`
/// column: primary + short/common/official names, `;`-separated).
///
/// Exonym queries ("moscow" for Москва, "cairo" for القاهرة) score 0 on the
/// display-name ladder; without this rung they lose to exact-named homonyms
/// (Moscow, ID). Capped below display-exact (1.0) so the local name still
/// wins ties between same-named places.
///
/// New shards separate distinct alternate names with `;` so multi-word alt
/// names stay intact and can phrase-match ("mexico city" hits the "mexico
/// city" segment of Ciudad de México's search_name). Old shards flattened
/// everything into one deduplicated token bag — no `;` — and evaluate as a
/// single segment, which preserves their historical behavior exactly.
///
/// Matching a word of the *primary* name is not matching an alternate name:
/// without the primary-name exclusion, "york" would score 0.95 against New
/// York City (only 0.05 below a true exact match) and its importance would
/// let it bury the exact-named York. The display ladder ([`match_quality`])
/// is authoritative for anything that appears in the primary name.
pub fn alt_name_quality(search_name: &str, primary_name: &str, query: &NormalizedQuery) -> f64 {
    if query.is_empty() || search_name.is_empty() {
        return 0.0;
    }

    // Defer to the display ladder when the query matches at a word start
    // of the primary name itself (whole word or prefix).
    let padded_primary = format!(" {} ", normalize_for_match(primary_name));
    if padded_primary.contains(&query.prefixed) {
        return 0.0;
    }

    let mut best = 0.0_f64;
    for segment in search_name.split(';') {
        if segment.is_empty() {
            continue;
        }
        let padded = format!(" {} ", normalize_for_match(segment));
        // Whole word/phrase appears within this name
        if padded.contains(&query.padded) {
            return 0.95;
        }
        // A name word starts with the query (autocomplete-style)
        if padded.contains(&query.prefixed) {
            best = 0.85;
        }
    }
    best
}

pub fn match_quality(primary_name: &str, query: &NormalizedQuery) -> f64 {
    let display = normalize_for_match(primary_name.split(',').next().unwrap_or(""));

    if query.is_empty() || display.is_empty() {
        return 0.0;
    }
    if display == query.text {
        return 1.0;
    }
    if let Some(rest) = display.strip_prefix(&query.text) {
        // strip_prefix is byte-correct for multi-byte names.
        return match rest.chars().next() {
            Some(c) if !c.is_alphanumeric() => 0.9, // word boundary
            _ => 0.8,                               // bare prefix
        };
    }

    // Partial-overlap credit requires a meaningful shared prefix: at least
    // 3 chars AND at least half the query. Without it, accidents like
    // "ge"/"ger" let Gelsenkirchen or Gera outscore Deutschland for the
    // query "germany" (alt-name matches legitimately score 0 on the
    // display-name ladder and must win on importance instead).
    let min_meaningful_lcp = 3.max(query.chars.div_ceil(2));
    let lcp = display
        .chars()
        .zip(query.text.chars())
        .take_while(|(a, b)| a == b)
        .count();
    if lcp < min_meaningful_lcp {
        return 0.0;
    }
    0.8 * lcp as f64 / query.chars as f64
}

/// Fold common Latin diacritics to their base letter (lowercase input).
/// One-to-one mappings only; anything else passes through unchanged.
fn fold_latin_diacritic(c: char) -> char {
    match c {
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => 'a',
        'è' | 'é' | 'ê' | 'ë' => 'e',
        'ì' | 'í' | 'î' | 'ï' => 'i',
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' => 'o',
        'ù' | 'ú' | 'û' | 'ü' => 'u',
        'ç' => 'c',
        'ñ' => 'n',
        'ý' | 'ÿ' => 'y',
        _ => c,
    }
}

/// Maximum candidate rows fetched per subtype during reverse geocoding.
/// Keeps every hierarchy level represented (the old flat `LIMIT 50` could
/// truncate region/country in dense areas) while bounding the result set.
pub const REVERSE_CANDIDATES_PER_SUBTYPE: u32 = 8;

/// SQL query for reverse geocoding via the R*Tree spatial index
/// (`divisions_reverse_rtree`, present in shards built after 2026-06).
///
/// The R*Tree stores bboxes as 32-bit floats rounded outward, so the join
/// re-checks exact containment against the attribute table. The window
/// function keeps the N smallest divisions per subtype so parent levels
/// (region, country) always survive into the result.
pub const REVERSE_GEOCODE_RTREE_SQL: &str = r#"
    WITH candidates AS (
        SELECT
            d.gers_id,
            d.subtype,
            d.primary_name,
            d.lat,
            d.lon,
            d.bbox_xmin,
            d.bbox_ymin,
            d.bbox_xmax,
            d.bbox_ymax,
            d.area,
            d.country,
            d.region,
            ROW_NUMBER() OVER (PARTITION BY d.subtype ORDER BY d.area ASC) AS rn
        FROM divisions_reverse_rtree r
        JOIN divisions_reverse d ON d.rowid = r.id
        WHERE r.xmin <= ?1 AND r.xmax >= ?1
          AND r.ymin <= ?2 AND r.ymax >= ?2
          AND d.bbox_xmin <= ?1 AND d.bbox_xmax >= ?1
          AND d.bbox_ymin <= ?2 AND d.bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area, country, region
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;

/// Fallback reverse geocoding SQL for shards without the R*Tree table.
/// The bbox B-tree index only serves the leading column, so this scans
/// a large fraction of the table — acceptable only for legacy shards.
pub const REVERSE_GEOCODE_SQL: &str = r#"
    WITH candidates AS (
        SELECT
            gers_id,
            subtype,
            primary_name,
            lat,
            lon,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            area,
            country,
            region,
            ROW_NUMBER() OVER (PARTITION BY subtype ORDER BY area ASC) AS rn
        FROM divisions_reverse
        WHERE bbox_xmin <= ?1
          AND bbox_xmax >= ?1
          AND bbox_ymin <= ?2
          AND bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area, country, region
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;

pub const REVERSE_GEOCODE_RTREE_SQL_WKB: &str = r#"
    WITH candidates AS (
        SELECT
            d.gers_id,
            d.subtype,
            d.primary_name,
            d.lat,
            d.lon,
            d.bbox_xmin,
            d.bbox_ymin,
            d.bbox_xmax,
            d.bbox_ymax,
            d.area,
            d.country,
            d.region,
            d.wkb,
            ROW_NUMBER() OVER (PARTITION BY d.subtype ORDER BY d.area ASC) AS rn
        FROM divisions_reverse_rtree r
        JOIN divisions_reverse d ON d.rowid = r.id
        WHERE r.xmin <= ?1 AND r.xmax >= ?1
          AND r.ymin <= ?2 AND r.ymax >= ?2
          AND d.bbox_xmin <= ?1 AND d.bbox_xmax >= ?1
          AND d.bbox_ymin <= ?2 AND d.bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area, country, region, wkb
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;

pub const REVERSE_GEOCODE_SQL_WKB: &str = r#"
    WITH candidates AS (
        SELECT
            gers_id,
            subtype,
            primary_name,
            lat,
            lon,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            area,
            country,
            region,
            wkb,
            ROW_NUMBER() OVER (PARTITION BY subtype ORDER BY area ASC) AS rn
        FROM divisions_reverse
        WHERE bbox_xmin <= ?1
          AND bbox_xmax >= ?1
          AND bbox_ymin <= ?2
          AND bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area, country, region, wkb
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;

#[cfg(test)]
mod tests {
    use super::*;

    fn mq(name: &str, query: &str) -> f64 {
        match_quality(name, &NormalizedQuery::new(query))
    }

    fn alt(search_name: &str, primary_name: &str, query: &str) -> f64 {
        alt_name_quality(search_name, primary_name, &NormalizedQuery::new(query))
    }

    #[test]
    fn test_match_quality_ladder() {
        // Exact match (comma-truncated display name)
        assert_eq!(mq("Paris", "paris"), 1.0);
        assert_eq!(mq("Boston, MA", "boston"), 1.0);
        // Prefix at word boundary
        assert_eq!(mq("Paris Township", "paris"), 0.9);
        // Bare prefix
        assert_eq!(mq("Parisville", "paris"), 0.8);
        // Partial: coverage-scaled
        let q = mq("Paradise", "paris");
        assert!((q - 0.8 * 3.0 / 5.0).abs() < 1e-9, "got {q}");
        // No overlap
        assert_eq!(mq("London", "paris"), 0.0);
    }

    #[test]
    fn test_match_quality_comma_qualified_query() {
        // Queries carry the same comma qualifier as display names; both
        // sides are truncated so the exact/prefix rungs stay reachable.
        assert_eq!(mq("Boston, MA", "boston, ma"), 1.0);
        assert_eq!(mq("Boston", "Boston, Massachusetts"), 1.0);
        assert_eq!(mq("Paris Township, OH", "paris, ohio"), 0.9);
    }

    #[test]
    fn test_match_quality_diacritics_and_case() {
        assert_eq!(mq("Zürich", "zurich"), 1.0);
        assert_eq!(mq("São Paulo", "sao paulo"), 1.0);
        assert_eq!(mq("PARIS", "Paris"), 1.0);
    }

    #[test]
    fn test_match_quality_multiword_prefix() {
        // Query covering the first words of a longer name = word-boundary prefix
        assert_eq!(mq("New York City, NY", "new york"), 0.9);
    }

    #[test]
    fn test_match_quality_empty() {
        assert_eq!(mq("Paris", ""), 0.0);
        assert_eq!(mq("", "paris"), 0.0);
    }

    #[test]
    fn test_alt_name_quality_exonyms() {
        // Whole alternate name: the exonym rung
        assert_eq!(alt("москва moscow moskva", "Москва", "moscow"), 0.95);
        // Alternate-name word prefix (autocomplete-style)
        assert_eq!(alt("москва moscow moskva", "Москва", "mosk"), 0.85);
        // No match anywhere
        assert_eq!(alt("москва moscow moskva", "Москва", "berlin"), 0.0);
    }

    #[test]
    fn test_alt_name_quality_ignores_primary_name_tokens() {
        // Legacy token-bag shards: every word of a multi-word primary name
        // appears standalone. Those must not earn alt credit: "york"
        // scoring 0.95 against NYC would bury the exact-named York.
        assert_eq!(alt("new york city nyc", "New York City", "york"), 0.0);
        assert_eq!(alt("los angeles la", "Los Angeles", "angeles"), 0.0);
        // Prefix of a primary-name word: also the display ladder's call
        assert_eq!(alt("san francisco sf", "San Francisco", "san"), 0.0);
        assert_eq!(alt("san francisco sf", "San Francisco", "franc"), 0.0);
        // A genuine alternate name ("nyc") still gets the rung
        assert_eq!(alt("new york city nyc", "New York City", "nyc"), 0.95);
    }

    #[test]
    fn test_alt_name_quality_segmented_phrase_match() {
        // ';'-separated shards keep name boundaries: a multi-word alternate
        // name is phrase-matchable (the "mexico city" regression). The
        // token-bag form of the same data could never reach this rung
        // because the words are unordered.
        let cdmx = "ciudad de méxico;cdmx;mexico city;méxico d.f.";
        assert_eq!(alt(cdmx, "Ciudad de México", "mexico city"), 0.95);
        // Same phrase against the legacy bag (words reordered by dedup)
        assert_eq!(
            alt(
                "cd. d.f. city mexico cdmx méxico de ciudad",
                "Ciudad de México",
                "mexico city"
            ),
            0.0
        );
        // Word-prefix within a segment still earns the autocomplete rung
        assert_eq!(alt(cdmx, "Ciudad de México", "mexico ci"), 0.85);
        // Segments don't leak into each other: no false phrase across ';'
        assert_eq!(alt("alpha beta;gamma", "Unrelated", "beta gamma"), 0.0);
        // Primary-name exclusion still wins over segment matches
        assert_eq!(alt("new york;nyc;big apple", "New York City", "york"), 0.0);
        // Whole-word match inside one segment
        assert_eq!(
            alt("new york;nyc;big apple", "New York City", "apple"),
            0.95
        );
    }

    #[test]
    fn test_calculate_boosted_score() {
        // Population lowers (improves) the score; missing population gets
        // the flat penalty offset.
        let with_pop = calculate_boosted_score(-1.0, Some(1_000_000));
        let no_pop = calculate_boosted_score(-1.0, None);
        assert!(with_pop < no_pop);
        assert!((no_pop - (-1.0 - MISSING_POPULATION_PENALTY)).abs() < 1e-9);
    }
}
