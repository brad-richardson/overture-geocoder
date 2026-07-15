from __future__ import annotations

import importlib.util
import struct
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "experiment_address_division_index.py"
SPEC = importlib.util.spec_from_file_location(
    "experiment_address_division_index", SCRIPT
)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_uvarint_boundaries():
    assert experiment.encode_uvarint(0) == b"\x00"
    assert experiment.encode_uvarint(127) == b"\x7f"
    assert experiment.encode_uvarint(128) == b"\x80\x01"
    assert experiment.encode_uvarint(16_384) == b"\x80\x80\x01"


def test_text_encoding_preserves_unicode():
    encoded = experiment.encode_text("Montréal")
    assert encoded[0] == len("Montréal".encode())
    assert encoded[1:].decode() == "Montréal"


def test_chain_encoding_uses_compact_uuid_bytes():
    signature = (
        "region:00000000-0000-0000-0000-000000000001|"
        "locality:00000000-0000-0000-0000-000000000002"
    )
    encoded = experiment.encode_chain(signature)
    assert len(encoded) == 1 + 2 * 17
    assert encoded[0] == 2
    assert encoded[1] == experiment.SUBTYPE_CODES["region"]
    assert encoded[18] == experiment.SUBTYPE_CODES["locality"]


def test_feature_id_fallback_is_stable_and_marked_invalid():
    first, first_valid = experiment.encode_feature_id("not-a-uuid")
    second, second_valid = experiment.encode_feature_id("not-a-uuid")
    assert first == second
    assert len(first) == 16
    assert not first_valid
    assert not second_valid


def test_coordinate_encoding_fits_signed_i32():
    lon = experiment.encode_coordinate(-71.0589, longitude=True)
    lat = experiment.encode_coordinate(42.3601, longitude=False)
    assert struct.pack("<ii", lon, lat)


def test_indexed_blob_writes_terminal_offset(tmp_path):
    index_path = tmp_path / "index.bin"
    blob_path = tmp_path / "blob.bin"
    with index_path.open("wb") as index_file, blob_path.open("wb") as blob_file:
        count = experiment.write_indexed_blob([b"abc", b"defgh"], index_file, blob_file)
    assert count == 2
    assert struct.unpack("<QQQ", index_path.read_bytes()) == (0, 3, 8)
    assert blob_path.read_bytes() == b"abcdefgh"
