import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INVENTORY = ROOT / "benchmarks" / "address-rowgroup-inventory-report.json"
SELECTION = ROOT / ".github" / "address-sweep-selection.json"

_spec = importlib.util.spec_from_file_location(
    "select_address_sweep_tasks", ROOT / "scripts" / "select_address_sweep_tasks.py"
)
sel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sel)


# Indices the strata rules must reproduce against the checked-in inventory
# (the plan doc's sanity reference).
EXPECTED_INDEX_BY_NAME = {
    "us-full": 48,
    "mexico-full": 3,
    "japan-full": 117,
    "japan-pure": 121,
    "taiwan-full": 105,
    "brazil-full": 16,
    "italy-full": 84,
    "france-full": 75,
    "germany-full": 85,
    "sparse-tail": 126,
    "mixed-country": 8,
    "us-mid": 10,
}


@pytest.fixture(scope="module")
def report():
    return sel.load_inventory(INVENTORY)


def test_selection_reproduces_reference_indices(report):
    entries = sel.select_tasks(report)
    got = {e["name"]: e["task_index"] for e in entries}
    assert got == EXPECTED_INDEX_BY_NAME


def test_selection_is_ordered_and_unique(report):
    entries = sel.select_tasks(report)
    indices = [e["task_index"] for e in entries]
    names = [e["name"] for e in entries]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices) == 12
    assert len(set(names)) == 12


def test_selection_is_deterministic(report):
    a = sel.select_tasks(report)
    b = sel.select_tasks(report)
    assert a == b


def test_expected_rows_and_bytes_match_inventory(report):
    tasks_by_index = {t["index"]: t for t in report["plan"]["tasks"]}
    for entry in sel.select_tasks(report):
        source = tasks_by_index[entry["task_index"]]
        assert entry["expected_rows"] == source["rows"]
        assert (
            entry["expected_selected_compressed_bytes"]
            == source["selected_compressed_bytes"]
        )


def test_committed_selection_matches_generated(report):
    document = sel.build_selection_document(report)
    committed = json.loads(SELECTION.read_text())
    assert committed == document


def test_matrix_from_selection_is_name_and_index_only():
    committed = json.loads(SELECTION.read_text())
    matrix = sel.matrix_from_selection(committed)
    assert set(matrix) == {"include"}
    assert len(matrix["include"]) == 12
    for item in matrix["include"]:
        assert set(item) == {"name", "task_index"}


def test_matrix_from_selection_rejects_bool_index():
    bad = {"tasks": [{"name": "x", "task_index": True}]}
    with pytest.raises(sel.SelectionError):
        sel.matrix_from_selection(bad)


def test_validate_override_accepts_include_object():
    override = json.dumps(
        {"include": [{"name": "us-full", "task_index": 48}]}
    )
    matrix = sel.validate_override(override)
    assert matrix == {"include": [{"name": "us-full", "task_index": 48}]}


def test_validate_override_accepts_bare_list():
    matrix = sel.validate_override('[{"name": "us-full", "task_index": 48}]')
    assert matrix["include"][0]["task_index"] == 48


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "{}",
        '{"include": []}',
        '{"include": [{"name": "x"}]}',
        '{"include": [{"task_index": 1}]}',
        '{"include": [{"name": "UPPER", "task_index": 1}]}',
        '{"include": [{"name": "x", "task_index": -1}]}',
        '{"include": [{"name": "x", "task_index": true}]}',
        '{"include": [{"name": "x", "task_index": 1, "extra": 2}]}',
        '{"include": [{"name": "a", "task_index": 1}, {"name": "a", "task_index": 2}]}',
    ],
)
def test_validate_override_rejects_bad_input(bad):
    with pytest.raises(sel.SelectionError):
        sel.validate_override(bad)
