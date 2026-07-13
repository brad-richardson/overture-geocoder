import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_shard_smoke_readback as verify  # noqa: E402


def _record(path):
    return {
        "href": f"./{path.name}",
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _fixtures(tmp_path):
    forward = tmp_path / "MC.db"
    db = sqlite3.connect(forward)
    db.executescript(
        """
        CREATE TABLE divisions(
          rowid INTEGER PRIMARY KEY, gers_id TEXT, primary_name TEXT,
          country TEXT, importance REAL
        );
        CREATE VIRTUAL TABLE divisions_fts USING fts5(
          search_name, content='divisions', content_rowid='rowid'
        );
        INSERT INTO divisions VALUES (1, 'forward-id', 'Monaco', 'MC', 1.0);
        INSERT INTO divisions_fts(rowid, search_name) VALUES (1, 'monaco');
        """
    )
    db.close()

    reverse = tmp_path / "reverse-MC.db"
    db = sqlite3.connect(reverse)
    db.executescript(
        """
        CREATE TABLE divisions_reverse(
          rowid INTEGER PRIMARY KEY, gers_id TEXT, primary_name TEXT,
          country TEXT, subtype TEXT, bbox_xmin REAL, bbox_ymin REAL,
          bbox_xmax REAL, bbox_ymax REAL, area REAL
        );
        CREATE VIRTUAL TABLE divisions_reverse_rtree USING rtree(
          id, xmin, xmax, ymin, ymax
        );
        INSERT INTO divisions_reverse VALUES
          (1, 'reverse-id', 'Monaco', 'MC', 'country', 7.4, 43.7, 7.5, 43.8, 1.0);
        INSERT INTO divisions_reverse_rtree VALUES (1, 7.4, 7.5, 43.7, 43.8);
        """
    )
    db.close()

    router = tmp_path / "router.db"
    db = sqlite3.connect(router)
    db.executescript(
        """
        CREATE TABLE router(
          token TEXT NOT NULL,
          shard_id TEXT NOT NULL,
          max_importance REAL NOT NULL,
          PRIMARY KEY(token, shard_id)
        );
        CREATE INDEX idx_token ON router(token);
        """
    )
    db.execute("INSERT INTO router VALUES ('monaco', 'MC', 0.8)")
    db.commit()
    db.close()

    collection = tmp_path / "collection.json"
    reverse_collection = tmp_path / "reverse-collection.json"
    collection.write_text(
        json.dumps(
            {
                "items": {
                    "MC": {**_record(forward), "href": "./shards/MC.db"}
                },
                "router": {
                    **_record(router),
                    "href": "./router.db",
                    "token_count": 1,
                    "pair_count": 1,
                },
            }
        )
    )
    reverse_collection.write_text(
        json.dumps(
            {
                "items": {
                    "MC": {**_record(reverse), "href": "./reverse/MC.db"}
                }
            }
        )
    )
    return collection, reverse_collection, forward, reverse, router


def test_verifies_hashes_and_queries_fresh_objects(tmp_path):
    report = verify.verify_readback(*_fixtures(tmp_path))
    assert report["forward"]["country"] == "MC"
    assert report["reverse"]["country"] == "MC"
    assert report["router"] == {
        "row_count": 1,
        "sample": {"token": "monaco", "shard_id": "MC", "importance": 0.8},
    }


def test_accepts_valid_empty_router(tmp_path):
    fixtures = _fixtures(tmp_path)
    collection_path, _, _, _, router_path = fixtures
    db = sqlite3.connect(router_path)
    db.execute("DELETE FROM router")
    db.commit()
    db.close()
    collection = json.loads(collection_path.read_text())
    collection["router"] = {
        **_record(router_path),
        "href": "./router.db",
        "token_count": 0,
        "pair_count": 0,
    }
    collection_path.write_text(json.dumps(collection))

    report = verify.verify_readback(*fixtures)

    assert report["router"] == {"row_count": 0, "sample": None}


def test_rejects_router_without_required_token_index(tmp_path):
    router = _fixtures(tmp_path)[-1]
    db = sqlite3.connect(router)
    db.execute("DROP INDEX idx_token")
    db.commit()
    db.close()

    with pytest.raises(RuntimeError, match="token or primary-key index"):
        verify._query_router(router, {"token_count": 1, "pair_count": 1})


def test_rejects_invalid_nonempty_router_sample(tmp_path):
    router = _fixtures(tmp_path)[-1]
    db = sqlite3.connect(router)
    db.execute("DELETE FROM router")
    db.execute("INSERT INTO router VALUES ('', 'MC', 0.8)")
    db.commit()
    db.close()

    with pytest.raises(RuntimeError, match="invalid sample token"):
        verify._query_router(router, {"token_count": 1, "pair_count": 1})


def test_rejects_router_metadata_count_mismatch(tmp_path):
    fixtures = _fixtures(tmp_path)
    collection_path = fixtures[0]
    collection = json.loads(collection_path.read_text())
    collection["router"]["pair_count"] = 2
    collection_path.write_text(json.dumps(collection))

    with pytest.raises(RuntimeError, match="metadata count mismatch"):
        verify.verify_readback(*fixtures)


@pytest.mark.parametrize(
    ("key", "value"),
    [("pair_count", True), ("token_count", -1), ("pair_count", 1.5)],
)
def test_rejects_invalid_router_metadata_counts(tmp_path, key, value):
    router = _fixtures(tmp_path)[-1]
    record = {"token_count": 1, "pair_count": 1, key: value}

    with pytest.raises(RuntimeError, match=f"no valid {key}"):
        verify._query_router(router, record)


def test_fails_on_readback_tampering(tmp_path):
    fixtures = _fixtures(tmp_path)
    fixtures[-1].write_bytes(fixtures[-1].read_bytes() + b"tamper")
    with pytest.raises(RuntimeError, match="readback mismatch"):
        verify.verify_readback(*fixtures)


@pytest.mark.parametrize(
    ("collection_index", "href", "message"),
    [
        (0, "./wrong/MC.db", "collection MC href"),
        (1, "./wrong/MC.db", "reverse collection MC href"),
    ],
)
def test_rejects_collection_href_that_does_not_match_readback_key(
    tmp_path, collection_index, href, message
):
    fixtures = _fixtures(tmp_path)
    collection_path = fixtures[collection_index]
    collection = json.loads(collection_path.read_text())
    collection["items"]["MC"]["href"] = href
    collection_path.write_text(json.dumps(collection))
    with pytest.raises(RuntimeError, match=message):
        verify.verify_readback(*fixtures)
