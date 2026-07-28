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
import json
import os
import subprocess
import sys
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


def plan_args(root: Path, slices: list[SyntheticSlice], output: Path) -> list[str]:
    args = ["plan", "--source", f"local:{root / 'source'}"]
    for built in slices:
        args += [
            "--family", built.family,
            "--slice-root", f"{built.family}={built.slice_root}",
            "--markers-root", f"{built.family}={built.markers_root}",
            "--reductions-dir", f"{built.family}={built.reductions_dir}",
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
