-- Resolve active_release_id once, cache it briefly in the serving tier,
-- then bind the concrete release ID below to preserve partition pruning.
SELECT active_release_id FROM places_planetscale_spike.catalog WHERE family = 'places';

-- name_prefix
SELECT gers_id, name, category, locality, region, country, lat, lon, confidence FROM places_planetscale_spike.places WHERE release_id = 'fixture-2025-12-17.0' AND normalized_name LIKE 'starb%' ORDER BY confidence DESC, gers_id LIMIT 10;

-- token_exact
SELECT gers_id, name, category, locality, region, country, lat, lon, confidence FROM places_planetscale_spike.places WHERE release_id = 'fixture-2025-12-17.0' AND search_document @@ plainto_tsquery('simple', 'warfield hotel') ORDER BY ts_rank_cd(search_document, plainto_tsquery('simple', 'warfield hotel')) DESC, confidence DESC, gers_id LIMIT 10;

-- token_prefix
SELECT gers_id, name, category, locality, region, country, lat, lon, confidence FROM places_planetscale_spike.places WHERE release_id = 'fixture-2025-12-17.0' AND search_document @@ to_tsquery('simple', 'golden & gat:*') ORDER BY ts_rank_cd(search_document, to_tsquery('simple', 'golden & gat:*')) DESC, confidence DESC, gers_id LIMIT 10;

-- category
SELECT gers_id, name, category, locality, region, country, lat, lon, confidence FROM places_planetscale_spike.places WHERE release_id = 'fixture-2025-12-17.0' AND category = 'hotel' ORDER BY confidence DESC, gers_id LIMIT 10;

-- context_token
SELECT gers_id, name, category, locality, region, country, lat, lon, confidence FROM places_planetscale_spike.places WHERE release_id = 'fixture-2025-12-17.0' AND country = 'US' AND region = 'CA' AND locality = 'San Francisco' AND search_document @@ to_tsquery('simple', 'cafe:*') ORDER BY ts_rank_cd(search_document, to_tsquery('simple', 'cafe:*')) DESC, confidence DESC, gers_id LIMIT 10;
