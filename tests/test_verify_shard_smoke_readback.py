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
    db.execute(
        "CREATE TABLE router(token TEXT, shard_id TEXT, max_importance REAL)"
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
                "router": {**_record(router), "href": "./router.db"},
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
    assert report["router"]["shard_id"] == "MC"


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
