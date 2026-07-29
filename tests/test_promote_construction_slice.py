"""Contract tests for scripts/promote_construction_slice.py.

The synthetic builder reproduces exactly the construction-v1 publication shape
the tool consumes: a create-only source tree (family manifest, slice manifest,
content-addressed serving objects, per-record packs, finalize marker written
with the exact published set) plus the local reduction records that carry the
partition -> object bindings. Everything is tiny but structurally faithful, so
plan/execute/verify run end to end against a local destination with no
credentials.

A live-harness end-to-end pass (real Monaco Places / Seattle Addresses output
from scripts/run_slice_construction_v1.py) is gated on PROMOTE_E2E_WORK_PLACES /
PROMOTE_E2E_WORK_ADDRESSES pointing at harness --work directories, because the
harness needs network access and the release Rust binaries.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import global_build_manifest as gbm
import promote_construction_slice as promote

REQUEST_SHA = "ab" * 32
RELEASE = "2026-07-22.0"
VERSION = "slice-2026-07-28.0"
PRODUCER_COMMIT = "0123abc"
UINT64_MAX = (1 << 64) - 1


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _obj(payload: bytes, store_class: str, extension: str) -> dict:
    digest = _sha(payload)
    return {
        "key": f"{store_class}/sha256/{digest}{extension}",
        "sha256": digest,
        "bytes": len(payload),
        "payload": payload,
        "name": f"{digest}{extension}",
    }


def _identity(obj: dict) -> dict:
    return {"key": obj["key"], "sha256": obj["sha256"], "bytes": obj["bytes"]}


class SyntheticSlice:
    """One family's construction-v1 publication plus its reduction records."""

    def __init__(self, root: Path, family: str):
        self.family = family
        self.root = root
        self.slice_root = f"construction-v1/test-{family}/slice/slice-1/"
        self.markers_root = f"construction-v1/test-{family}/markers/"
        self.family_prefix = f"{self.slice_root}families/{family}/"
        self.reductions_dir = root / f"reductions-{family}"
        self.reductions_dir.mkdir(parents=True, exist_ok=True)
        self.serving: list[dict] = []
        self.positions: list[dict] = []
        self.reductions: list[dict] = []
        self.head_block = None

    def _write(self, key: str, payload: bytes) -> None:
        path = self.root / "source" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def add_reduction(self, partition: dict, payload: bytes, serving_key: str) -> dict:
        store_class, extension = (
            ("serve/places-v1/routed", ".plrv")
            if self.family == "places"
            else ("reduce/address/artifacts", ".av1")
        )
        obj = _obj(payload, store_class, extension)
        self.serving.append(obj)
        record = {
            "schema": (
                "overture-places-selective-reduce-v1"
                if self.family == "places"
                else "overture-address-selective-reduce-v1"
            ),
            "partition": partition,
            serving_key: _identity(obj),
        }
        self.reductions.append(record)
        return obj

    def add_head(self, shard_payloads: list[bytes]) -> None:
        shards = []
        shard_objects = []
        for shard_id, payload in enumerate(shard_payloads):
            obj = _obj(payload, "serve/places-v1/head", ".plhd")
            shard_objects.append(obj)
            shards.append(
                {
                    "shard_id": shard_id,
                    "path": obj["name"],
                    "sha256": obj["sha256"],
                    "bytes": obj["bytes"],
                    "records": 1,
                    "index_entries": 1,
                }
            )
        manifest = {
            "schema": "overture-places-global-head-sharded-v2",
            "shard_count": 16,
            "shard_bits": 4,
            "populated_shards": len(shards),
            "total_records": len(shards),
            "shards": shards,
        }
        payload = json.dumps(manifest, sort_keys=True).encode()
        manifest_obj = _obj(payload, "serve/places-v1/head-manifest", ".json")
        self.serving.extend(shard_objects)
        self.serving.append(manifest_obj)
        self.head_block = {
            "shard_count": 16,
            "shard_bits": 4,
            "populated_shards": len(shards),
            "total_records": len(shards),
            "manifest": {
                "object": manifest_obj["name"],
                "sha256": manifest_obj["sha256"],
                "bytes": manifest_obj["bytes"],
            },
        }

    def add_positions(self, payload: bytes) -> None:
        self.positions.append(_obj(payload, "map/per-record", ".parquet"))

    def publish(self) -> None:
        """Write objects, manifests and the finalize marker, marker last."""
        for index, record in enumerate(self.reductions):
            (self.reductions_dir / f"{index:04d}.json").write_text(
                json.dumps(record, sort_keys=True)
            )
        for obj in self.serving:
            self._write(f"{self.family_prefix}objects/{obj['name']}", obj["payload"])
        prefix = "positions" if self.family == "places" else "records"
        for obj in self.positions:
            self._write(f"{self.family_prefix}{prefix}/{obj['name']}", obj["payload"])

        family_manifest = {
            "schema": "construction-v1-family-manifest-v1",
            "family": self.family,
            "request_sha256": REQUEST_SHA,
            "reconciles": True,
            "binding": {"records": 1},
            "partitions": len(self.reductions),
            "artifacts": sorted(
                (_identity(obj) for obj in self.serving), key=lambda item: item["key"]
            ),
            "head": self.head_block,
            "positions": (
                {
                    "schema": "per-record-v1",
                    "records": 1,
                    "objects": [_identity(obj) for obj in self.positions],
                }
                if self.positions
                else None
            ),
        }
        family_manifest_bytes = json.dumps(family_manifest, sort_keys=True).encode()
        self._write(f"{self.family_prefix}family-manifest.json", family_manifest_bytes)
        slice_manifest = {
            "schema": "construction-v1-slice-manifest-v1",
            "request_sha256": REQUEST_SHA,
            "family": self.family,
            "family_manifest_sha256": _sha(family_manifest_bytes),
            "object_count": len(self.serving) + len(self.positions),
            "positions_object_count": len(self.positions),
            "non_promoting": True,
        }
        slice_manifest_bytes = json.dumps(slice_manifest, sort_keys=True).encode()
        self._write(f"{self.family_prefix}slice-manifest.json", slice_manifest_bytes)

        marker_artifacts = [
            {
                "key": f"{self.family_prefix}family-manifest.json",
                "sha256": _sha(family_manifest_bytes),
                "bytes": len(family_manifest_bytes),
            },
            {
                "key": f"{self.family_prefix}slice-manifest.json",
                "sha256": _sha(slice_manifest_bytes),
                "bytes": len(slice_manifest_bytes),
            },
        ]
        for obj in self.serving:
            marker_artifacts.append(
                {
                    "key": f"{self.family_prefix}objects/{obj['name']}",
                    "sha256": obj["sha256"],
                    "bytes": obj["bytes"],
                }
            )
        for obj in self.positions:
            marker_artifacts.append(
                {
                    "key": f"{self.family_prefix}{prefix}/{obj['name']}",
                    "sha256": obj["sha256"],
                    "bytes": obj["bytes"],
                }
            )
        marker = {
            "schema": "overture-construction-v1-create-only-marker-v1",
            "request_sha256": REQUEST_SHA,
            "artifacts": sorted(marker_artifacts, key=lambda item: item["key"]),
            "exact_keys_sha256": "00" * 32,
        }
        self._write(
            f"{self.markers_root}finalize/{self.family}.json",
            json.dumps(marker, sort_keys=True).encode(),
        )


def _places_partition(cell: str, depth: int = 0, prefix: int = 0) -> dict:
    suffix = "" if depth == 0 else f"-h{prefix:0{depth}x}"
    return {
        "id": f"p-{cell}{suffix}",
        "partition_cell": cell,
        "ownership": {
            "kind": "token-sha256-nibble-prefix-v1",
            "depth": depth,
            "prefix": prefix,
        },
        "binding": {"records": 1},
    }


def _address_partition(country: str, start: int, end: int, suffix: str = "") -> dict:
    return {
        "id": f"a-{country}{suffix}",
        "country": country,
        "hash_start": start,
        "hash_end": end,
        "binding": {"records": 1},
    }


def build_places(root: Path) -> SyntheticSlice:
    built = SyntheticSlice(root, "places")
    built.add_reduction(
        _places_partition("5292"), b"plrv-5292", "routed_object"
    )
    built.add_reduction(
        _places_partition("5293"), b"plrv-5293", "routed_object"
    )
    for nibble in range(16):
        built.add_reduction(
            _places_partition("5e5e", depth=1, prefix=nibble),
            b"plrv-5e5e-%d" % nibble,
            "routed_object",
        )
    built.add_head([b"plhd-0", b"plhd-1"])
    built.add_positions(b"positions-pack")
    built.publish()
    return built


def build_addresses(root: Path) -> SyntheticSlice:
    built = SyntheticSlice(root, "addresses")
    built.add_reduction(
        _address_partition("mc", 0, UINT64_MAX), b"av1-mc", "artifact"
    )
    half = UINT64_MAX // 2
    built.add_reduction(
        _address_partition("us", 0, half, "-h-0"), b"av1-us-0", "artifact"
    )
    built.add_reduction(
        _address_partition("us", half + 1, UINT64_MAX, "-h-1"), b"av1-us-1", "artifact"
    )
    built.add_positions(b"records-pack")
    built.publish()
    return built


def plan_args(
    root: Path,
    slices: list[SyntheticSlice],
    output: Path,
    reverse_catalogs: dict[str, Path] | None = None,
) -> list[str]:
    args = ["plan", "--source", f"local:{root / 'source'}"]
    for built in slices:
        args += [
            "--family", built.family,
            "--slice-root", f"{built.family}={built.slice_root}",
            "--markers-root", f"{built.family}={built.markers_root}",
            "--reductions-dir", f"{built.family}={built.reductions_dir}",
        ]
        if reverse_catalogs and built.family in reverse_catalogs:
            args += [
                "--reverse-catalog",
                f"{built.family}={reverse_catalogs[built.family]}",
            ]
    args += [
        "--version", VERSION,
        "--release", RELEASE,
        "--producer-commit", PRODUCER_COMMIT,
        "--output", str(output),
    ]
    return args


@pytest.fixture()
def both(tmp_path):
    return tmp_path, [build_places(tmp_path), build_addresses(tmp_path)]


def run_plan(root: Path, slices, output: Path) -> dict:
    assert promote.main(plan_args(root, slices, output)) == 0
    return json.loads(output.read_text())


def direct_reverse_publication(
    root: Path, family: str, destination: Path, *, version: str = VERSION
) -> Path:
    prefix = f"{version}/families/{family}"
    claim_payload = promote.canonical(
        {
            "schema": promote.SLICE_CLAIM_SCHEMA,
            "version": version,
            "family": family,
            "request_sha256": REQUEST_SHA,
            "overture_release": RELEASE,
        }
    )
    claim_key = f"{version}/claims/{family}.json"
    claim_path = destination / claim_key
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    if claim_path.exists():
        assert claim_path.read_bytes() == claim_payload
    else:
        claim_path.write_bytes(claim_payload)
    claim = {
        "key": claim_key,
        "bytes": len(claim_payload),
        "sha256": _sha(claim_payload),
    }

    def write_content(relative: str, payload: bytes, suffix: str) -> dict:
        sha = _sha(payload)
        key = f"{prefix}/{relative}/sha256/{sha}{suffix}"
        path = destination / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"key": key, "bytes": len(payload), "sha256": sha}

    data = write_content("reverse/shards", f"{family}-data".encode(), ".plrx")
    family_code = 1 if family == "places" else 2
    cell = 0xC085 if family == "places" else 0xC328
    shard_id = cell >> 12
    catalog_shards = []
    for index in range(16):
        count = 1 if index == shard_id else 0
        payload = bytearray(
            promote.REVERSE_CATALOG_HEADER.pack(
                b"RCAS0001", family_code, 8, index, 0, count
            )
        )
        if count:
            payload.extend(
                promote.REVERSE_CATALOG_ENTRY.pack(
                    cell,
                    0,
                    0,
                    1,
                    data["bytes"],
                    1,
                    bytes.fromhex(data["sha256"]),
                )
            )
        catalog_shards.append(
            write_content(
                "reverse/catalog-shards", bytes(payload), ".rcas"
            )
        )
    y, x = cell >> 8, cell & 0xFF
    root_payload = bytearray(
        promote.REVERSE_ROOT_HEADER.pack(
            b"RCAT0001",
            family_code,
            8,
            16,
            0,
            2_000 if family == "places" else 500,
            x * promote.REVERSE_LON_E7_PER_CELL
            - promote.REVERSE_LON_E7_ORIGIN,
            y * promote.REVERSE_LAT_E7_PER_CELL
            - promote.REVERSE_LAT_E7_ORIGIN,
            (x + 1) * promote.REVERSE_LON_E7_PER_CELL
            - promote.REVERSE_LON_E7_ORIGIN,
            (y + 1) * promote.REVERSE_LAT_E7_PER_CELL
            - promote.REVERSE_LAT_E7_ORIGIN,
            1,
            1,
            0,
        )
    )
    for catalog in catalog_shards:
        root_payload.extend(
            promote.REVERSE_ROOT_SHARD.pack(
                catalog["bytes"], bytes.fromhex(catalog["sha256"])
            )
        )
    assert len(root_payload) == promote.REVERSE_ROOT_BYTES
    root_key = f"{prefix}/reverse-catalog.rcat"
    root_path = destination / root_key
    root_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.write_bytes(root_payload)
    root_identity = {
        "key": root_key,
        "bytes": len(root_payload),
        "sha256": _sha(root_payload),
    }
    publication = {
        "schema": promote.REVERSE_CATALOG_SCHEMA,
        "family": family,
        "request_sha256": REQUEST_SHA,
        "records": 1,
        "cells": 1,
        "root": root_identity,
        "catalog_shards": catalog_shards,
        "slice_claim": claim,
        "artifacts": sorted(
            [data, *catalog_shards, root_identity],
            key=lambda item: item["key"],
        ),
    }
    publication_path = root / f"{family}-reverse-publication.json"
    publication_path.write_text(json.dumps(publication, sort_keys=True))
    return publication_path


def run_execute(root: Path, plan_path: Path, destination: Path) -> None:
    assert (
        promote.main(
            [
                "execute",
                "--plan", str(plan_path),
                "--source", f"local:{root / 'source'}",
                "--destination", f"local:{destination}",
            ]
        )
        == 0
    )


def run_verify(plan_path: Path, destination: Path) -> None:
    assert (
        promote.main(
            [
                "verify",
                "--plan", str(plan_path),
                "--destination", f"local:{destination}",
            ]
        )
        == 0
    )


# ---------------------------------------------------------------------------
# plan


def test_plan_is_deterministic(both):
    root, slices = both
    first, second = root / "plan-a.json", root / "plan-b.json"
    run_plan(root, slices, first)
    run_plan(root, slices, second)
    assert first.read_bytes() == second.read_bytes()


def test_plan_runs_via_cli_subprocess(both):
    root, slices = both
    script = Path(__file__).parent.parent / "scripts/promote_construction_slice.py"
    output = root / "plan-cli.json"
    result = subprocess.run(
        [sys.executable, str(script), *plan_args(root, slices, output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["schema"] == promote.PLAN_SCHEMA


def test_plan_fails_without_finalize_marker(tmp_path):
    built = build_places(tmp_path)
    marker = tmp_path / "source" / built.markers_root / "finalize/places.json"
    marker.unlink()
    with pytest.raises(SystemExit, match="finalize marker is missing"):
        run_plan(tmp_path, [built], tmp_path / "plan.json")


def test_plan_fails_on_tampered_family_manifest(tmp_path):
    built = build_places(tmp_path)
    manifest = tmp_path / "source" / built.family_prefix / "family-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(SystemExit, match="do not match the slice manifest"):
        run_plan(tmp_path, [built], tmp_path / "plan.json")


def test_plan_fails_when_marker_identity_differs(tmp_path):
    built = build_addresses(tmp_path)
    marker_path = (
        tmp_path / "source" / built.markers_root / "finalize/addresses.json"
    )
    marker = json.loads(marker_path.read_text())
    for artifact in marker["artifacts"]:
        if artifact["key"].endswith(".av1"):
            artifact["sha256"] = "11" * 32
            break
    marker_path.write_text(json.dumps(marker, sort_keys=True))
    with pytest.raises(SystemExit, match="marker identity differs"):
        run_plan(tmp_path, [built], tmp_path / "plan.json")


def test_plan_fails_on_missing_nibble_subpartition(tmp_path):
    built = build_places(tmp_path)
    # Drop one of the sixteen 5e5e subpartition records: the nibble space no
    # longer tiles and every token hashing into the gap would be unroutable.
    victim = next(
        path
        for path in sorted(built.reductions_dir.glob("*.json"))
        if json.loads(path.read_text())["partition"]["id"] == "p-5e5e-h7"
    )
    victim.unlink()
    with pytest.raises(SystemExit, match="do not tile|do not cover"):
        run_plan(tmp_path, [built], tmp_path / "plan.json")


def test_plan_fails_on_overlapping_address_ranges(tmp_path):
    built = build_addresses(tmp_path)
    for path in sorted(built.reductions_dir.glob("*.json")):
        record = json.loads(path.read_text())
        if record["partition"]["id"] == "a-us-h-1":
            record["partition"]["hash_start"] -= 10  # overlap a-us-h-0
            path.write_text(json.dumps(record, sort_keys=True))
    with pytest.raises(SystemExit, match="ranges overlap"):
        run_plan(tmp_path, [built], tmp_path / "plan.json")


def test_plan_rejects_bad_version(both):
    root, slices = both
    args = plan_args(root, slices, root / "plan.json")
    args[args.index(VERSION)] = "2026-07-28.0"  # a PROMOTING core version shape
    with pytest.raises(SystemExit, match="slice-YYYY-MM-DD.N"):
        promote.main(args)


# ---------------------------------------------------------------------------
# routing derivation


def test_places_routing_derivation(tmp_path):
    built = build_places(tmp_path)
    plan = run_plan(tmp_path, [built], tmp_path / "plan.json")
    routing = plan["families"]["places"]["routing"]
    assert routing["schema"] == promote.PLACES_ROUTING_SCHEMA
    by_partition = {
        record["partition"]["id"]: record["routed_object"]
        for record in (
            json.loads(path.read_text())
            for path in sorted(built.reductions_dir.glob("*.json"))
        )
    }
    # Unsplit cell: one entry with the empty prefix, naming its .plrv object.
    expected_5292 = f"{by_partition['p-5292']['sha256']}.plrv"
    assert routing["cells"]["5292"] == [["", expected_5292]]
    # Split cell: sixteen depth-1 entries in nibble order.
    entries = routing["cells"]["5e5e"]
    assert [prefix for prefix, _ in entries] == [f"{n:x}" for n in range(16)]
    assert entries[7][1] == f"{by_partition['p-5e5e-h7']['sha256']}.plrv"
    # Head pointer names the copied head routing manifest and its geometry.
    assert routing["head"]["shard_bits"] == 4
    assert routing["head"]["populated_shards"] == 2
    assert routing["head"]["manifest_object"] == built.head_block["manifest"]["object"]


def test_address_routing_derivation(tmp_path):
    built = build_addresses(tmp_path)
    plan = run_plan(tmp_path, [built], tmp_path / "plan.json")
    routing = plan["families"]["addresses"]["routing"]
    assert routing["schema"] == promote.ADDRESS_ROUTING_SCHEMA
    rows = routing["partitions"]
    assert [row["country"] for row in rows] == ["mc", "us", "us"]
    assert rows[0]["hash_start"] == 0 and rows[0]["hash_end"] == UINT64_MAX
    assert rows[1]["hash_end"] + 1 == rows[2]["hash_start"]
    names = {obj["name"] for obj in built.serving}
    assert {row["object"] for row in rows} == names


# ---------------------------------------------------------------------------
# execute / verify


def test_end_to_end_local(both):
    root, slices = both
    plan_path = root / "plan.json"
    plan = run_plan(root, slices, plan_path)
    destination = root / "dest"
    run_execute(root, plan_path, destination)
    run_verify(plan_path, destination)

    for built in slices:
        family_dir = destination / VERSION / "families" / built.family
        listed = {
            path.relative_to(destination / VERSION).as_posix()
            for path in family_dir.rglob("*")
            if path.is_file()
        }
        expected = {
            f"families/{built.family}/objects/{obj['name']}" for obj in built.serving
        } | {
            f"families/{built.family}/routing.json",
            f"families/{built.family}/family-manifest.json",
        }
        # Exactly the worker-expected keys: serving objects + routing + #107
        # manifest. Per-record positions/records packs are NOT promoted.
        assert listed == expected
        manifest = gbm.validate_family_manifest(
            json.loads((family_dir / "family-manifest.json").read_text())
        )
        assert manifest["lineage"]["build_id"] == REQUEST_SHA
        assert manifest["lineage"]["overture_release"] == RELEASE
        # Every routed object exists on disk with the planned bytes.
        routing = json.loads((family_dir / "routing.json").read_text())
        for name in promote._routing_object_names(routing):
            assert (family_dir / "objects" / name).is_file()

    # Re-running execute is a clean resume (create-only, byte-identical), and
    # verify still passes.
    run_execute(root, plan_path, destination)
    run_verify(plan_path, destination)
    assert plan["version"] == VERSION


def test_direct_reverse_artifacts_are_attested_without_copy(tmp_path):
    built = build_places(tmp_path)
    destination = tmp_path / "dest"
    publication = direct_reverse_publication(
        tmp_path, "places", destination
    )
    plan_path = tmp_path / "plan.json"
    assert (
        promote.main(
            plan_args(
                tmp_path,
                [built],
                plan_path,
                reverse_catalogs={"places": publication},
            )
        )
        == 0
    )
    plan = json.loads(plan_path.read_text())
    family = plan["families"]["places"]
    assert family["totals"]["prepositioned_objects"] == 18
    assert family["totals"]["copied_objects"] == len(built.serving)

    reverse_before = {
        item["destination_key"]: (destination / item["destination_key"]).stat().st_ino
        for item in family["prepositioned"]
    }
    run_execute(tmp_path, plan_path, destination)
    run_verify(plan_path, destination)
    reverse_after = {
        key: (destination / key).stat().st_ino for key in reverse_before
    }
    assert reverse_after == reverse_before

    manifest = json.loads(
        (
            destination
            / VERSION
            / "families/places/family-manifest.json"
        ).read_text()
    )
    attested = {artifact["object_key"] for artifact in manifest["artifacts"]}
    assert "families/places/reverse-catalog.rcat" in attested
    assert sum(
        key.startswith("families/places/reverse/")
        for key in attested
    ) == 17


def test_combined_plan_can_publish_reverse_for_one_family_only(both):
    root, slices = both
    publication = direct_reverse_publication(
        root, "places", root / "dest"
    )
    plan_path = root / "plan-partial-reverse.json"
    assert (
        promote.main(
            plan_args(
                root,
                slices,
                plan_path,
                reverse_catalogs={"places": publication},
            )
        )
        == 0
    )
    plan = json.loads(plan_path.read_text())
    assert len(plan["families"]["places"]["prepositioned"]) == 18
    assert plan["families"]["addresses"]["prepositioned"] == []


def test_execute_refuses_missing_prepositioned_reverse_artifact(tmp_path):
    built = build_addresses(tmp_path)
    destination = tmp_path / "dest"
    publication = direct_reverse_publication(
        tmp_path, "addresses", destination
    )
    plan_path = tmp_path / "plan.json"
    assert (
        promote.main(
            plan_args(
                tmp_path,
                [built],
                plan_path,
                reverse_catalogs={"addresses": publication},
            )
        )
        == 0
    )
    plan = json.loads(plan_path.read_text())
    missing = plan["families"]["addresses"]["prepositioned"][0][
        "destination_key"
    ]
    (destination / missing).unlink()
    with pytest.raises(SystemExit, match="prepositioned destination"):
        run_execute(tmp_path, plan_path, destination)
    assert not (
        destination
        / VERSION
        / "families/addresses/family-manifest.json"
    ).exists()


def test_execute_refuses_malformed_reverse_catalog_graph(tmp_path):
    built = build_places(tmp_path)
    destination = tmp_path / "dest"
    publication_path = direct_reverse_publication(
        tmp_path, "places", destination
    )
    publication = json.loads(publication_path.read_text())
    root_key = publication["root"]["key"]
    root_path = destination / root_key
    malformed = bytearray(root_path.read_bytes())
    malformed[9] = 0  # cell level must be 8
    root_path.write_bytes(malformed)
    identity = {
        "key": root_key,
        "bytes": len(malformed),
        "sha256": _sha(malformed),
    }
    publication["root"] = identity
    publication["artifacts"] = [
        identity if item["key"] == root_key else item
        for item in publication["artifacts"]
    ]
    publication_path.write_text(json.dumps(publication, sort_keys=True))

    plan_path = tmp_path / "plan.json"
    assert (
        promote.main(
            plan_args(
                tmp_path,
                [built],
                plan_path,
                reverse_catalogs={"places": publication_path},
            )
        )
        == 0
    )
    with pytest.raises(SystemExit, match="reverse root contract differs"):
        run_execute(tmp_path, plan_path, destination)
    assert not (
        destination
        / VERSION
        / "families/places/family-manifest.json"
    ).exists()


def test_execute_fails_on_tampered_source_bytes(tmp_path):
    built = build_places(tmp_path)
    plan_path = tmp_path / "plan.json"
    run_plan(tmp_path, [built], plan_path)
    victim = built.serving[0]
    path = tmp_path / "source" / built.family_prefix / "objects" / victim["name"]
    path.write_bytes(b"tampered-but-same-name")
    with pytest.raises(SystemExit, match="does not match the planned identity"):
        run_execute(tmp_path, plan_path, tmp_path / "dest")


def test_execute_fails_on_conflicting_destination(tmp_path):
    built = build_addresses(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan = run_plan(tmp_path, [built], plan_path)
    destination = tmp_path / "dest"
    squatted = destination / plan["families"]["addresses"]["objects"][0][
        "destination_key"
    ]
    squatted.parent.mkdir(parents=True)
    squatted.write_bytes(b"squatter")
    with pytest.raises(SystemExit, match="exists with different bytes"):
        run_execute(tmp_path, plan_path, destination)


def test_execute_fails_on_orphan_routing_entry(tmp_path):
    built = build_addresses(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan = run_plan(tmp_path, [built], plan_path)
    family = plan["families"]["addresses"]
    family["routing"]["partitions"].append(
        {"country": "zz", "hash_start": 0, "hash_end": 1, "object": "ff" * 32 + ".av1"}
    )
    # Recompute the recorded routing identity so the orphan check itself -- not
    # the plan-tamper identity check -- is what fires.
    routing_bytes = promote.canonical(family["routing"])
    family["routing_sha256"] = _sha(routing_bytes)
    family["routing_bytes"] = len(routing_bytes)
    plan_path.write_bytes(promote.canonical(plan))
    with pytest.raises(SystemExit, match="orphan"):
        run_execute(tmp_path, plan_path, tmp_path / "dest")


def test_execute_fails_on_plan_tamper(tmp_path):
    built = build_addresses(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan = run_plan(tmp_path, [built], plan_path)
    plan["families"]["addresses"]["routing"]["partitions"][0]["country"] = "xx"
    plan_path.write_bytes(promote.canonical(plan))
    with pytest.raises(SystemExit, match="identities disagree"):
        run_execute(tmp_path, plan_path, tmp_path / "dest")


def test_verify_fails_on_missing_destination_object(both):
    root, slices = both
    plan_path = root / "plan.json"
    plan = run_plan(root, slices, plan_path)
    destination = root / "dest"
    run_execute(root, plan_path, destination)
    victim = plan["families"]["places"]["objects"][0]["destination_key"]
    (destination / victim).unlink()
    with pytest.raises(SystemExit, match="not the exact planned set"):
        run_verify(plan_path, destination)


def test_verify_fails_on_tampered_destination_object(both):
    root, slices = both
    plan_path = root / "plan.json"
    plan = run_plan(root, slices, plan_path)
    destination = root / "dest"
    run_execute(root, plan_path, destination)
    victim = plan["families"]["addresses"]["objects"][0]["destination_key"]
    (destination / victim).write_bytes(b"corrupted")
    with pytest.raises(SystemExit, match="identity differs"):
        run_verify(plan_path, destination)


def test_r2_open_tree_scopes_the_long_timeout_to_copy(monkeypatch):
    import r2_verified_store

    seen = {}

    class Store:
        bucket = "test-bucket"

    def fake_store(bucket, endpoint, **kwargs):
        seen.update(bucket=bucket, endpoint=endpoint, **kwargs)
        return Store()

    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(r2_verified_store, "s3_object_store", fake_store)

    tree = promote.open_tree("r2:test-bucket", "test tree")
    assert isinstance(tree, promote.R2Tree)
    assert seen == {
        "bucket": "test-bucket",
        "endpoint": "https://example.invalid",
        "copy_read_timeout_seconds": promote.COPY_READ_TIMEOUT_SECONDS,
    }


# ---------------------------------------------------------------------------
# R2 leg over a stubbed boto3 client (no credentials, no boto3 import): proves
# the HEAD -> create-only copy -> HEAD proof sequence, the content_md5
# byte-fidelity gate, paginated listing in verify, and error propagation.


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeR2Client:
    """In-memory ListObjectsV2/HeadObject/PutObject/GetObject/CopyObject.

    CopyObject copies metadata VERBATIM (the sha256 echo) while the ETag is
    always computed from the stored destination bytes -- exactly the R2
    contract the content_md5 check exists for. `corrupt_copies` flips one byte
    of every copy WITHOUT changing length or metadata, so only the ETag can
    see it; `head_overrides` lets a test make HEAD lie relative to GET.
    """

    def __init__(self, page_size=2):
        self.objects = {}
        self.log = []
        self.page_size = page_size
        self.fail_copy = None
        self.corrupt_copies = False
        self.head_overrides = {}
        self.list_pages_served = 0

    @staticmethod
    def _etag(payload):
        return '"%s"' % hashlib.md5(payload).hexdigest()

    def seed(self, key, payload):
        self.objects[key] = {"payload": payload, "meta": {"sha256": _sha(payload)}}

    def head_object(self, Bucket, Key):
        self.log.append(("head", Key))
        if Key in self.head_overrides:
            return self.head_overrides[Key]
        if Key not in self.objects:
            raise FakeClientError("404")
        item = self.objects[Key]
        return {
            "ContentLength": len(item["payload"]),
            "Metadata": dict(item["meta"]),
            "ETag": self._etag(item["payload"]),
        }

    def put_object(self, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch):
        assert IfNoneMatch == "*"
        self.log.append(("put", Key))
        if Key in self.objects:
            raise FakeClientError("412")
        payload = Body.read()
        assert len(payload) == ContentLength
        self.objects[Key] = {"payload": payload, "meta": dict(Metadata)}

    def get_object(self, Bucket, Key):
        self.log.append(("get", Key))
        if Key not in self.objects:
            raise FakeClientError("404")
        item = self.objects[Key]
        return {
            "ContentLength": len(item["payload"]),
            "Body": io.BytesIO(item["payload"]),
            "Metadata": dict(item["meta"]),
        }

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective):
        self.log.append(("copy", CopySource["Key"], Key))
        if self.fail_copy is not None:
            raise self.fail_copy
        assert MetadataDirective == "COPY"
        source = self.objects[CopySource["Key"]]
        payload = source["payload"]
        if self.corrupt_copies:
            payload = payload[:-1] + bytes([payload[-1] ^ 1])
        self.objects[Key] = {"payload": payload, "meta": dict(source["meta"])}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        self.log.append(("list", Prefix, ContinuationToken))
        self.list_pages_served += 1
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        payload = {"Contents": [{"Key": key} for key in page], "IsTruncated": truncated}
        if truncated:
            payload["NextContinuationToken"] = str(start + self.page_size)
        return payload


def _fake_store(client):
    import r2_verified_store as rvs

    store = rvs.Boto3Store.__new__(rvs.Boto3Store)
    store.client = client
    store.copy_client = client
    store.bucket = "test-bucket"
    store._client_error = FakeClientError
    store._stream_retry_error = type("NeverRetry", (Exception,), {})
    return store


@pytest.fixture()
def r2_world(tmp_path, monkeypatch):
    built = build_places(tmp_path)
    client = FakeR2Client()
    for path in sorted((tmp_path / "source").rglob("*")):
        if path.is_file():
            client.seed(
                path.relative_to(tmp_path / "source").as_posix(), path.read_bytes()
            )
    tree = promote.R2Tree(_fake_store(client))
    original = promote.open_tree
    monkeypatch.setattr(
        promote,
        "open_tree",
        lambda spec, what: tree if spec == "r2:test-bucket" else original(spec, what),
    )
    return tmp_path, built, client


def _r2_plan(tmp_path, built):
    plan_path = tmp_path / "plan.json"
    args = plan_args(tmp_path, [built], plan_path)
    args[args.index(f"local:{tmp_path / 'source'}")] = "r2:test-bucket"
    assert promote.main(args) == 0
    return plan_path, json.loads(plan_path.read_text())


def test_r2_plan_execute_verify_with_content_md5_and_pagination(r2_world):
    tmp_path, built, client = r2_world
    plan_path, plan = _r2_plan(tmp_path, built)
    # (a) the plan records the store-computed content MD5 of every source object.
    payloads = {obj["name"]: obj["payload"] for obj in built.serving}
    for item in plan["families"]["places"]["objects"]:
        name = item["destination_key"].rsplit("/", 1)[1]
        assert item["content_md5"] == hashlib.md5(payloads[name]).hexdigest()

    execute_start = len(client.log)
    assert (
        promote.main(
            ["execute", "--plan", str(plan_path),
             "--source", "r2:test-bucket", "--destination", "r2:test-bucket"]
        )
        == 0
    )
    # Per-object sequence: HEAD source (unchanged since plan), HEAD destination
    # (create-only conflict check), server-side copy, HEAD destination (proof).
    item = plan["families"]["places"]["objects"][0]
    source_key, destination_key = item["source_key"], item["destination_key"]
    touching = [
        entry
        for entry in client.log[execute_start:]
        if source_key in entry or destination_key in entry
    ]
    assert touching == [
        ("head", source_key),
        ("head", destination_key),
        ("copy", source_key, destination_key),
        ("head", destination_key),
    ]
    # Derived documents go through create-only PutObject, manifest last.
    puts = [entry[1] for entry in client.log[execute_start:] if entry[0] == "put"]
    family = plan["families"]["places"]
    assert puts == [family["routing_key"], family["family_manifest_key"]]

    client.list_pages_served = 0
    assert (
        promote.main(
            ["verify", "--plan", str(plan_path), "--destination", "r2:test-bucket"]
        )
        == 0
    )
    # 23 destination keys at 2 keys per page: verify's listing is paginated.
    assert client.list_pages_served > 1


def test_r2_copy_corruption_is_caught_by_content_md5(r2_world):
    tmp_path, built, client = r2_world
    plan_path, _ = _r2_plan(tmp_path, built)
    # The copy flips one byte but keeps length AND the copied sha256 metadata
    # echo, so ONLY the destination's own ETag (content MD5) can catch it.
    client.corrupt_copies = True
    with pytest.raises(SystemExit, match="post-copy identity proof"):
        promote.main(
            ["execute", "--plan", str(plan_path),
             "--source", "r2:test-bucket", "--destination", "r2:test-bucket"]
        )


def test_r2_copy_client_error_propagates(r2_world):
    tmp_path, built, client = r2_world
    plan_path, _ = _r2_plan(tmp_path, built)
    client.fail_copy = FakeClientError("500")
    # A transport failure must escape loudly -- the create-only FileExistsError
    # handler around the copy must not swallow it.
    with pytest.raises(FakeClientError):
        promote.main(
            ["execute", "--plan", str(plan_path),
             "--source", "r2:test-bucket", "--destination", "r2:test-bucket"]
        )


def test_r2_execute_uses_bounded_copy_pool(r2_world, monkeypatch):
    tmp_path, built, client = r2_world
    plan_path, _ = _r2_plan(tmp_path, built)
    original_copy = client.copy_object
    lock = threading.Lock()
    release = threading.Event()
    active = peak = entered = 0

    def observed_copy(**kwargs):
        nonlocal active, peak, entered
        with lock:
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == promote.COPY_WORKERS:
                release.set()
        assert release.wait(timeout=2)
        try:
            return original_copy(**kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(client, "copy_object", observed_copy)
    assert (
        promote.main(
            ["execute", "--plan", str(plan_path),
             "--source", "r2:test-bucket", "--destination", "r2:test-bucket"]
        )
        == 0
    )
    assert peak == promote.COPY_WORKERS


def test_r2_verify_hashes_downloaded_routing_bytes(r2_world):
    tmp_path, built, client = r2_world
    plan_path, plan = _r2_plan(tmp_path, built)
    assert (
        promote.main(
            ["execute", "--plan", str(plan_path),
             "--source", "r2:test-bucket", "--destination", "r2:test-bucket"]
        )
        == 0
    )
    # Freeze HEAD at the honest answer, then tamper the stored bytes: the
    # per-key metadata proof cannot see it, only the download-and-hash pass can.
    routing_key = plan["families"]["places"]["routing_key"]
    client.head_overrides[routing_key] = client.head_object("bucket", routing_key)
    payload = client.objects[routing_key]["payload"]
    client.objects[routing_key]["payload"] = payload[:-2] + b"~" + payload[-1:]
    with pytest.raises(SystemExit, match="routing.json bytes do not hash"):
        promote.main(
            ["verify", "--plan", str(plan_path), "--destination", "r2:test-bucket"]
        )


# ---------------------------------------------------------------------------
# live harness end-to-end (opt-in: needs run_slice_construction_v1.py output)


@pytest.mark.parametrize("family", ["places", "addresses"])
def test_harness_end_to_end(tmp_path, family):
    work = os.environ.get(f"PROMOTE_E2E_WORK_{family.upper()}")
    if not work:
        pytest.skip(f"set PROMOTE_E2E_WORK_{family.upper()} to a harness --work dir")
    work_path = Path(work)
    plan_path = tmp_path / "plan.json"
    args = [
        "plan",
        "--source", f"local:{work_path / 'remote'}",
        "--family", family,
        "--slice-root", f"construction-v1/slice-{family}/slice/slice-1/",
        "--markers-root", f"construction-v1/slice-{family}/markers/",
        "--reductions-dir", str(work_path / "reductions"),
        "--version", VERSION,
        "--release", RELEASE,
        "--producer-commit", PRODUCER_COMMIT,
        "--output", str(plan_path),
    ]
    assert promote.main(args) == 0
    destination = tmp_path / "dest"
    assert (
        promote.main(
            [
                "execute",
                "--plan", str(plan_path),
                "--source", f"local:{work_path / 'remote'}",
                "--destination", f"local:{destination}",
            ]
        )
        == 0
    )
    assert (
        promote.main(
            ["verify", "--plan", str(plan_path), "--destination", f"local:{destination}"]
        )
        == 0
    )
    plan = json.loads(plan_path.read_text())
    family_dir = destination / VERSION / "families" / family
    assert (family_dir / "family-manifest.json").is_file()
    routing = json.loads((family_dir / "routing.json").read_text())
    for name in promote._routing_object_names(routing):
        assert (family_dir / "objects" / name).is_file()
    assert plan["families"][family]["totals"]["objects"] > 0


# ---------------------------------------------------------------------------
# slice-manifest: the #107 slice source document consumed by
# v2_release_manifest.py assemble / _validate_family_source.


import importlib.util  # noqa: E402

_V2_SPEC = importlib.util.spec_from_file_location(
    "v2_release_manifest_for_promote",
    Path(__file__).parent.parent / "scripts" / "v2_release_manifest.py",
)
assert _V2_SPEC and _V2_SPEC.loader
v2 = importlib.util.module_from_spec(_V2_SPEC)
_V2_SPEC.loader.exec_module(v2)

_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "v2_release_manifest_fixtures_for_promote",
    Path(__file__).parent / "test_v2_release_manifest.py",
)
assert _FIXTURE_SPEC and _FIXTURE_SPEC.loader
v2_fixtures = importlib.util.module_from_spec(_FIXTURE_SPEC)
_FIXTURE_SPEC.loader.exec_module(v2_fixtures)

LEGACY = "2026-07-18.0"


def _run_slice_manifest(plan_paths, destination, output, execute=True):
    args = ["slice-manifest"]
    for path in plan_paths:
        args += ["--plan", str(path)]
    args += ["--destination", f"local:{destination}", "--output", str(output)]
    if execute:
        args.append("--execute")
    return promote.main(args)


def _promoted_world(both):
    """plan+execute+verify both families, one plan per family (the workflow
    shape: two construction runs with different producer commits)."""
    root, slices = both
    destination = root / "dest"
    plan_paths = []
    for built in slices:
        plan_path = root / f"plan-{built.family}.json"
        run_plan(root, [built], plan_path)
        run_execute(root, plan_path, destination)
        run_verify(plan_path, destination)
        plan_paths.append(plan_path)
    return root, destination, plan_paths


def test_slice_manifest_end_to_end_feeds_v2_assemble(both):
    root, destination, plan_paths = _promoted_world(both)
    output = root / "slice-manifest.json"
    assert _run_slice_manifest(plan_paths, destination, output) == 0

    key = destination / VERSION / "slice-manifest.json"
    assert key.read_bytes() == output.read_bytes()
    document = json.loads(output.read_text())
    assert document["is_slice"] is True
    assert document["promotion_eligible"] is False
    assert document["slice_version"] == VERSION
    assert sorted(document["families"]) == ["addresses", "places"]

    # The document must satisfy the #194 source-manifest boundary exactly.
    for family in ("addresses", "places"):
        plan = json.loads(
            (root / f"plan-{family}.json").read_text()
        )["families"][family]
        family_manifest = gbm.validate_family_manifest(plan["family_manifest"])
        v2._validate_family_source(
            family,
            document,
            hashlib.sha256(output.read_bytes()).hexdigest(),
            family_manifest,
            RELEASE,
        )

    # And the full #194 assemble must run against the promoted tree alone.
    legacy = v2_fixtures.legacy_release(LEGACY, release=RELEASE)
    legacy_path = destination / LEGACY / "release-manifest.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(v2.gbm.canonical_json(legacy))
    release_out = root / "release.json"
    v2.main(
        [
            "assemble",
            "--store", f"local:{destination}",
            "--geocoder-build", "2026-07-28.0",
            "--overture-release", RELEASE,
            "--slice-version", VERSION,
            "--legacy-core", LEGACY,
            "--output", str(release_out),
        ]
    )
    release = json.loads(release_out.read_text())
    v2.validate_release_manifest(release)
    for family in ("addresses", "places"):
        assert release["families"][family]["source"] == {
            "kind": "family_slice",
            "version": VERSION,
            "manifest_key": f"{VERSION}/slice-manifest.json",
            "manifest_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }


def test_no_copy_reverse_publication_feeds_v2_assemble(both):
    root, slices = both
    destination = root / "dest"
    plan_paths = []
    for built in slices:
        publication = direct_reverse_publication(
            root, built.family, destination
        )
        plan_path = root / f"plan-reverse-{built.family}.json"
        assert (
            promote.main(
                plan_args(
                    root,
                    [built],
                    plan_path,
                    reverse_catalogs={built.family: publication},
                )
            )
            == 0
        )
        run_execute(root, plan_path, destination)
        run_verify(plan_path, destination)
        plan_paths.append(plan_path)

    source_output = root / "slice-manifest-reverse.json"
    assert _run_slice_manifest(
        plan_paths, destination, source_output
    ) == 0
    legacy = v2_fixtures.legacy_release(LEGACY, release=RELEASE)
    legacy_path = destination / LEGACY / "release-manifest.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(v2.gbm.canonical_json(legacy))
    release_out = root / "release-reverse.json"
    v2.main(
        [
            "assemble",
            "--store", f"local:{destination}",
            "--geocoder-build", "2026-07-29.0",
            "--overture-release", RELEASE,
            "--slice-version", VERSION,
            "--legacy-core", LEGACY,
            "--output", str(release_out),
        ]
    )
    release = json.loads(release_out.read_text())
    v2.validate_release_manifest(release)
    for family, forward_operation in (
        ("places", "forward"),
        ("addresses", "structured_forward"),
    ):
        assert release["families"][family]["operations"] == sorted(
            [forward_operation, "reverse"]
        )
        assert release["families"][family]["entrypoints"]["reverse"][
            "object_key"
        ] == f"{VERSION}/families/{family}/reverse-catalog.rcat"


def test_manifest_only_release_keeps_forward_data_in_its_existing_slice(both):
    root, destination, plan_paths = _promoted_world(both)
    source_output = root / "base-slice-manifest.json"
    assert _run_slice_manifest(plan_paths, destination, source_output) == 0

    reverse_version = "slice-2026-07-29.0"
    reverse_publications = {
        family: direct_reverse_publication(
            root, family, destination, version=reverse_version
        )
        for family in ("addresses", "places")
    }
    legacy = v2_fixtures.legacy_release(LEGACY, release=RELEASE)
    legacy_path = destination / LEGACY / "release-manifest.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(v2.gbm.canonical_json(legacy))
    release_out = root / "manifest-only-release.json"
    v2.main(
        [
            "assemble",
            "--store", f"local:{destination}",
            "--geocoder-build", "2026-07-29.0",
            "--overture-release", RELEASE,
            "--slice-version", VERSION,
            "--legacy-core", LEGACY,
            "--reverse-publication", f"places={reverse_publications['places']}",
            "--reverse-publication", f"addresses={reverse_publications['addresses']}",
            "--output", str(release_out),
        ]
    )

    release = v2.validate_release_manifest(json.loads(release_out.read_text()))
    for family, forward_operation in (
        ("places", "forward"),
        ("addresses", "structured_forward"),
    ):
        reference = release["families"][family]
        assert reference["source"]["version"] == VERSION
        assert reference["entrypoints"][forward_operation]["object_key"].startswith(
            f"{VERSION}/"
        )
        assert reference["entrypoints"]["reverse"]["object_key"] == (
            f"{reverse_version}/families/{family}/reverse-catalog.rcat"
        )
        assert reference["operation_sources"]["reverse"]["version"] == reverse_version
        assert not (
            destination / reverse_version / "families" / family / "family-manifest.json"
        ).exists()
        assert not (
            destination / reverse_version / "families" / family / "routing.json"
        ).exists()

    v2.main(
        [
            "publish-release",
            "--store", f"local:{destination}",
            "--release", str(release_out),
            "--execute",
        ]
    )
    claim_path = destination / reverse_version / "claims" / "places.json"
    claim_payload = claim_path.read_bytes()
    claim_path.write_bytes(claim_payload + b" ")
    with pytest.raises(SystemExit, match="slice claim"):
        v2.main(
            [
                "promote",
                "--store", f"local:{destination}",
                "--build", "2026-07-29.0",
                "--expect-absent",
            ]
        )
    claim_path.write_bytes(claim_payload)
    v2.main(
        [
            "promote",
            "--store", f"local:{destination}",
            "--build", "2026-07-29.0",
            "--expect-absent",
            "--execute",
        ]
    )
    catalog = v2.validate_catalog(
        json.loads((destination / "v2" / "catalog.json").read_text())
    )
    assert catalog["latest"] == "2026-07-29.0"


def test_slice_manifest_is_deterministic_and_republish_is_benign(both):
    root, destination, plan_paths = _promoted_world(both)
    first = root / "first.json"
    second = root / "second.json"
    assert _run_slice_manifest(plan_paths, destination, first) == 0
    # A second execute over the same plans must emit byte-identical content
    # and accept the already-published document.
    assert _run_slice_manifest(plan_paths, destination, second) == 0
    assert first.read_bytes() == second.read_bytes()


def test_slice_manifest_dry_run_writes_nothing(both):
    root, destination, plan_paths = _promoted_world(both)
    output = root / "slice-manifest.json"
    assert _run_slice_manifest(plan_paths, destination, output, execute=False) == 0
    assert not (destination / VERSION / "slice-manifest.json").exists()
    assert output.is_file()


def test_slice_manifest_fails_before_execute(both):
    root, slices = both
    plan_path = root / "plan.json"
    run_plan(root, slices, plan_path)
    with pytest.raises(
        SystemExit, match="missing object|run execute and verify first"
    ):
        _run_slice_manifest(
            [plan_path], root / "never-executed", root / "out.json"
        )


def test_slice_manifest_fails_on_conflicting_published_document(both):
    root, destination, plan_paths = _promoted_world(both)
    conflicting = destination / VERSION / "slice-manifest.json"
    conflicting.parent.mkdir(parents=True, exist_ok=True)
    conflicting.write_bytes(b'{"other": "document"}')
    for execute in (False, True):
        with pytest.raises(SystemExit, match="immutable"):
            _run_slice_manifest(
                plan_paths, destination, root / "out.json", execute=execute
            )


def test_slice_manifest_rejects_disagreeing_plans(both):
    root, destination, plan_paths = _promoted_world(both)
    doctored = json.loads(plan_paths[0].read_text())
    doctored["version"] = "slice-2026-07-29.0"
    doctored_path = root / "doctored.json"
    doctored_path.write_text(json.dumps(doctored))
    with pytest.raises(SystemExit, match="disagree on version or release"):
        _run_slice_manifest(
            [doctored_path, plan_paths[1]], destination, root / "out.json"
        )


def test_slice_manifest_rejects_duplicate_family_plans(both):
    root, destination, plan_paths = _promoted_world(both)
    with pytest.raises(SystemExit, match="more than one plan"):
        _run_slice_manifest(
            [plan_paths[0], plan_paths[0]], destination, root / "out.json"
        )


def test_slice_manifest_fails_on_tampered_plan_manifest(both):
    root, destination, plan_paths = _promoted_world(both)
    doctored = json.loads(plan_paths[1].read_text())
    family = next(iter(doctored["families"]))
    doctored["families"][family]["family_manifest_sha256"] = "0" * 64
    doctored_path = root / "doctored.json"
    doctored_path.write_text(json.dumps(doctored))
    with pytest.raises(SystemExit, match="identity disagrees"):
        _run_slice_manifest([doctored_path], destination, root / "out.json")
