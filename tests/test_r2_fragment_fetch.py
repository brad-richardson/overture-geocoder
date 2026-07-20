from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import r2_fragment_fetch as fetcher


def test_safe_key_keeps_fragment_under_execution_prefix():
    assert fetcher.safe_key(
        "staging/global-v2/abc/immutable/map/addresses/objects",
        "fragments/sha256/abcd/body.bin",
    ) == (
        "staging/global-v2/abc/immutable/map/addresses/objects/"
        "fragments/sha256/abcd/body.bin"
    )


def test_safe_key_accepts_canonical_places_group_component():
    digest = "a" * 64
    assert fetcher.safe_key(
        "staging/global-v2/build/immutable/map/places/objects",
        f"fragments/group=0123/sha256/{digest}/part-00000.parquet",
    ).endswith(f"/fragments/group=0123/sha256/{digest}/part-00000.parquet")


@pytest.mark.parametrize(
    "value",
    [
        "../catalog.json",
        "/absolute",
        "a//b",
        "a/./b",
        "a/$bad",
        "a/group=0123;touch",
        "a/=leading",
    ],
)
def test_safe_key_rejects_escape_or_shell_metacharacters(value):
    with pytest.raises(ValueError):
        fetcher.safe_key("staging/global-v2/safe", value)


def test_fetch_uses_argv_without_shell_and_removes_failed_output(tmp_path, monkeypatch):
    output = tmp_path / "fragment.bin"
    observed = {}

    def run(command, *, check):
        observed["command"] = command
        observed["check"] = check
        output.write_bytes(b"body")

    monkeypatch.setattr(fetcher.subprocess, "run", run)
    fetcher.fetch(
        bucket="geocoder-shards",
        prefix="staging/global-v2/safe/immutable",
        object_key="fragment.bin",
        output=output,
        endpoint_url="https://account.r2.cloudflarestorage.com",
    )

    assert output.read_bytes() == b"body"
    assert observed["check"] is True
    assert observed["command"][0:2] == ["aws", "s3api"]
    assert observed["command"][observed["command"].index("--key") + 1].startswith(
        "staging/global-v2/safe/"
    )
