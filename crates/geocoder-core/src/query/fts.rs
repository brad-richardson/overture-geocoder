//! FTS5 query preparation.
//!
//! Transforms user input into FTS5 MATCH syntax.

/// Prepare an FTS5 query from user input.
///
/// Handles tokenization, escaping, and optional prefix wildcards for autocomplete.
///
/// # Examples
///
/// ```
/// use geocoder_core::query::prepare_fts_query;
///
/// // Basic query with autocomplete
/// assert_eq!(prepare_fts_query("boston", true), r#""boston"*"#);
///
/// // Multi-word query
/// assert_eq!(prepare_fts_query("boston ma", true), r#""boston" "ma"*"#);
///
/// // Without autocomplete
/// assert_eq!(prepare_fts_query("boston", false), r#""boston""#);
///
/// // Punctuation removal
/// assert_eq!(prepare_fts_query("new york, ny", true), r#""new" "york" "ny"*"#);
/// ```
const MAX_FTS_TOKENS: usize = 10;
const MIN_FTS_TOKEN_CHARS: usize = 2;

pub fn prepare_fts_query(query: &str, autocomplete: bool) -> String {
    let mut normalized = String::with_capacity(query.len());
    for c in query.chars() {
        if c.is_alphanumeric() || c.is_whitespace() || c == '-' {
            for lc in c.to_lowercase() {
                normalized.push(lc);
            }
        } else {
            normalized.push(' ');
        }
    }

    let mut tokens: Vec<String> = normalized
        .split_whitespace()
        .filter(|t| !t.is_empty())
        // A one-character exact term is a legitimate query and uses the FTS
        // term index. The expensive case is a one-character prefix scan.
        .filter(|t| !autocomplete || t.chars().count() >= MIN_FTS_TOKEN_CHARS)
        .map(|s| s.to_string())
        .collect();

    if tokens.is_empty() {
        return String::new();
    }

    if tokens.len() > MAX_FTS_TOKENS {
        tokens.truncate(MAX_FTS_TOKENS);
    }

    tokens
        .iter()
        .enumerate()
        .map(|(i, token)| {
            let escaped = token.replace('"', "\"\"");
            if autocomplete && i == tokens.len() - 1 {
                format!("\"{}\"*", escaped)
            } else {
                format!("\"{}\"", escaped)
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_query() {
        assert_eq!(prepare_fts_query("boston", true), r#""boston"*"#);
        assert_eq!(prepare_fts_query("boston", false), r#""boston""#);
    }

    #[test]
    fn test_multi_word_query() {
        assert_eq!(prepare_fts_query("boston ma", true), r#""boston" "ma"*"#);
        assert_eq!(prepare_fts_query("boston ma", false), r#""boston" "ma""#);
    }

    #[test]
    fn test_punctuation_removal() {
        assert_eq!(
            prepare_fts_query("new york, ny", true),
            r#""new" "york" "ny"*"#
        );
        assert_eq!(prepare_fts_query("st. louis", true), r#""st" "louis"*"#);
    }

    #[test]
    fn test_hyphenated_words() {
        assert_eq!(
            prepare_fts_query("winston-salem", true),
            r#""winston-salem"*"#
        );
    }

    #[test]
    fn test_empty_query() {
        assert_eq!(prepare_fts_query("", true), "");
        assert_eq!(prepare_fts_query("   ", true), "");
        assert_eq!(prepare_fts_query("...", true), "");
    }

    #[test]
    fn test_case_normalization() {
        assert_eq!(prepare_fts_query("BOSTON", true), r#""boston"*"#);
        assert_eq!(prepare_fts_query("BoStOn", true), r#""boston"*"#);
    }

    #[test]
    fn test_extra_whitespace() {
        assert_eq!(
            prepare_fts_query("  boston   ma  ", true),
            r#""boston" "ma"*"#
        );
    }

    #[test]
    fn test_unicode_lowercasing() {
        // Non-ASCII characters should be lowercased correctly
        assert_eq!(prepare_fts_query("MÜNCHEN", true), r#""münchen"*"#);
        assert_eq!(prepare_fts_query("ZÜRICH", true), r#""zürich"*"#);
        assert_eq!(prepare_fts_query("MALMÖ", true), r#""malmö"*"#);
        // Mixed ASCII and non-ASCII
        assert_eq!(prepare_fts_query("São Paulo", true), r#""são" "paulo"*"#);
        // Case where Unicode lowercasing uses an expansion (e.g., 'İ' → "i\u{307}")
        // The full to_lowercase() iterator is preserved, so the combining dot above remains.
        assert_eq!(prepare_fts_query("İSTANBUL", true), "\"i\u{0307}stanbul\"*");
    }

    #[test]
    fn test_short_token_guardrails_preserve_exact_search() {
        assert_eq!(prepare_fts_query("a", true), "");
        assert_eq!(prepare_fts_query("new y", true), r#""new"*"#);
        assert_eq!(prepare_fts_query("a", false), r#""a""#);
        assert_eq!(prepare_fts_query("route 1", false), r#""route" "1""#);
    }

    #[test]
    fn test_token_count_is_capped() {
        let query = "one two three four five six seven eight nine ten eleven twelve";
        assert_eq!(
            prepare_fts_query(query, true),
            r#""one" "two" "three" "four" "five" "six" "seven" "eight" "nine" "ten"*"#
        );
    }
}
