import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bounded_fragment_prefetch as prefetch  # noqa: E402


def wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for prefetch fixture")


def fixture(tmp_path: Path, count: int = 6):
    sources = tmp_path / "sources"
    sources.mkdir()
    mapping = {}
    entries = []
    for index in range(count):
        key = f"map/address-fragments/task-{index:03d}.bin"
        source = sources / f"{index}.bin"
        source.write_bytes(bytes([index + 1]) * (1_000 + index))
        mapping[key] = str(source)
        entries.append({"object_key": key, "bytes": source.stat().st_size})
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(entries))
    log = tmp_path / "fetch.log"
    fetcher = tmp_path / "fetcher.py"
    fetcher.write_text(
        "import json, pathlib, shutil, sys, time\n"
        "mapping = json.load(open(sys.argv[1]))\n"
        "with open(sys.argv[2], 'a') as log:\n"
        "    log.write('start ' + sys.argv[3] + '\\n')\n"
        "    log.flush()\n"
        "time.sleep(0.15)\n"
        "shutil.copyfile(mapping[sys.argv[3]], pathlib.Path(sys.argv[4]))\n"
    )
    command = [
        sys.executable,
        str(fetcher),
        str(mapping_path),
        str(log),
        "{object_key}",
        "{output}",
    ]
    return entries, manifest, log, command


def manager(tmp_path: Path, entries, manifest, command):
    return prefetch.PrefetchManager(
        prefetch._load_manifest(manifest),
        cache_dir=tmp_path / "cache",
        fetch_command=prefetch._load_fetch_command(json.dumps(command)),
        workers=3,
        max_cache_bytes=sum(item["bytes"] for item in entries),
    )


def test_prefetch_overlaps_reads_but_releases_exact_manifest_order(tmp_path):
    entries, manifest, log, command = fixture(tmp_path)
    transport = manager(tmp_path, entries, manifest, command)
    try:
        wait_for(lambda: log.exists() and len(log.read_text().splitlines()) >= 3)
        outputs = tmp_path / "outputs"
        for index, entry in enumerate(entries):
            output = outputs / f"{index}.bin"
            transport.provide(entry["object_key"], output)
            assert output.stat().st_size == entry["bytes"]
        assert transport.done
        assert not list((tmp_path / "cache").iterdir())
    finally:
        transport.close()


def test_prefetch_rejects_out_of_order_consumer_without_advancing(tmp_path):
    entries, manifest, _, command = fixture(tmp_path, count=2)
    transport = manager(tmp_path, entries, manifest, command)
    try:
        with pytest.raises(ValueError, match="departed from manifest order"):
            transport.provide(entries[1]["object_key"], tmp_path / "wrong.bin")
        for index, entry in enumerate(entries):
            transport.provide(
                entry["object_key"], tmp_path / f"accepted-{index}.bin"
            )
        assert transport.done
    finally:
        transport.close()


def test_prefetch_rejects_wrong_download_length_and_cleans_partial(tmp_path):
    entries, manifest, _, command = fixture(tmp_path, count=1)
    entries[0]["bytes"] += 1
    manifest.write_text(json.dumps(entries))
    transport = manager(tmp_path, entries, manifest, command)
    try:
        with pytest.raises(ValueError, match="byte length differs"):
            transport.provide(entries[0]["object_key"], tmp_path / "wrong-size.bin")
        assert not list((tmp_path / "cache").iterdir())
    finally:
        transport.close()


def test_prefetch_server_and_client_complete_and_remove_socket(tmp_path):
    entries, manifest, _, command = fixture(tmp_path, count=3)
    socket_path = Path("/tmp") / f"og-prefetch-{os.getpid()}-{time.time_ns()}.sock"
    errors = []

    def run_server():
        try:
            prefetch.serve(
                manifest=manifest,
                socket_path=socket_path,
                cache_dir=tmp_path / "protocol-cache",
                fetch_command_json=json.dumps(command),
                workers=2,
                max_cache_bytes=sum(item["bytes"] for item in entries),
            )
        except BaseException as exc:  # surfaced in the test thread below
            errors.append(exc)

    server = threading.Thread(target=run_server)
    server.start()
    try:
        wait_for(lambda: socket_path.exists() or errors)
        if errors and isinstance(errors[0], PermissionError):
            pytest.skip("local sandbox forbids Unix-domain socket binding")
        assert not errors
        for index, entry in enumerate(entries):
            output = tmp_path / f"protocol-{index}.bin"
            prefetch.fetch(
                socket_path=socket_path,
                object_key=entry["object_key"],
                output=output,
            )
            assert output.stat().st_size == entry["bytes"]
        server.join(timeout=10)
        assert not server.is_alive()
        assert not errors
        assert not socket_path.exists()
    finally:
        socket_path.unlink(missing_ok=True)
        server.join(timeout=10)
