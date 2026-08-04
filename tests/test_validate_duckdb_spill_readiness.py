import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import address_construction_v1 as construction  # noqa: E402
import places_construction_v1 as places  # noqa: E402


def load_validator():
    path = ROOT / "scripts" / "validate_duckdb_spill_readiness.py"
    spec = importlib.util.spec_from_file_location("duckdb_spill_readiness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["duckdb_spill_readiness"] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()
ALLOWANCES = ROOT / "benchmarks/duckdb-spill-allowances-v1.json"


def allowances(stages):
    return {
        "schema": validator.ALLOWANCES_SCHEMA,
        "headroom_fraction": 0.25,
        "stages": stages,
    }


def observation(stage, peak, *, observations=64, cap=4_563_402_752, failed=False):
    return {
        "schema": validator.OBSERVATION_SCHEMA,
        "stage": stage,
        "peak_duckdb_temp_bytes": peak,
        "duckdb_temp_cap_bytes": cap,
        "observations": observations,
        "peak_sweep_seconds": 0.01,
        "stage_failed": failed,
    }


def write(path, value):
    path.write_text(json.dumps(value))
    return path


# --- the measurement itself --------------------------------------------------


def test_watchdog_separates_duckdb_spill_from_the_rest_of_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "output.parquet").write_bytes(b"a" * 5_000)
    (workspace / "duckdb_temp_storage_S32K-0.tmp").write_bytes(b"b" * 3_000)
    (workspace / "duckdb_temp_storage_S64K-1.tmp").write_bytes(b"c" * 1_000)

    total, spill = construction.StageWatchdog.measure_disk([workspace])
    assert total == 9_000
    assert spill == 4_000
    # The pre-existing single-value API must be unchanged for every caller.
    assert construction.StageWatchdog.disk_bytes([workspace]) == 9_000


def test_spill_peak_reaches_the_evidence_and_the_observation_line(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "duckdb_temp_storage_S32K-0.tmp").write_bytes(b"x" * 2_048)

    limits = construction.Limits(
        max_rss_bytes=1 << 62,
        max_scratch_bytes=1 << 40,
        wall_seconds=600,
    )
    with construction.StageWatchdog(
        [workspace],
        limits,
        stage="test.stage",
        duckdb_temp_cap_bytes=8_192,
    ) as watchdog:
        time.sleep(0.05)

    assert watchdog.evidence()["peak_duckdb_temp_bytes"] == 2_048
    # Additive only: the pre-existing keys must all survive.
    for key in (
        "peak_rss_bytes",
        "peak_scratch_and_output_bytes",
        "wall_seconds",
        "observations",
        "peak_sweep_seconds",
    ):
        assert key in watchdog.evidence()

    printed = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if validator.OBSERVATION_KEY in line
    ]
    assert len(printed) == 1
    record = printed[0][validator.OBSERVATION_KEY]
    assert record["stage"] == "test.stage"
    assert record["peak_duckdb_temp_bytes"] == 2_048
    assert record["duckdb_temp_cap_bytes"] == 8_192
    assert record["used_fraction_of_cap"] == pytest.approx(0.25)
    assert record["stage_failed"] is False


def test_an_unlabelled_watchdog_stays_silent(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    limits = construction.Limits(
        max_rss_bytes=1 << 62, max_scratch_bytes=1 << 40, wall_seconds=600
    )
    with construction.StageWatchdog([workspace], limits):
        pass
    assert validator.OBSERVATION_KEY not in capsys.readouterr().out


def test_a_failed_stage_still_emits_its_spill_number(tmp_path, capsys):
    """The dying run is the one whose peak matters most."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "duckdb_temp_storage_S32K-0.tmp").write_bytes(b"x" * 4_096)
    limits = construction.Limits(
        max_rss_bytes=1,  # trips the RSS cap immediately
        max_scratch_bytes=1 << 40,
        wall_seconds=600,
    )
    with pytest.raises(RuntimeError):
        with construction.StageWatchdog(
            [workspace], limits, stage="test.dying", duckdb_temp_cap_bytes=4_096
        ):
            time.sleep(0.05)
    printed = [
        json.loads(line)[validator.OBSERVATION_KEY]
        for line in capsys.readouterr().out.splitlines()
        if validator.OBSERVATION_KEY in line
    ]
    assert len(printed) == 1
    assert printed[0]["stage_failed"] is True
    assert printed[0]["peak_duckdb_temp_bytes"] == 4_096


def test_integer_and_string_temp_limits_agree():
    cap = 18_253_611_008
    assert construction.duckdb_temp_limit(cap) == (
        f"{construction.duckdb_temp_limit_bytes(cap)}B"
    )
    assert places.map_duckdb_temp_limit(cap) == (
        f"{places.map_duckdb_temp_limit_bytes(cap)}B"
    )
    with pytest.raises(ValueError):
        construction.duckdb_temp_limit_bytes(0)
    with pytest.raises(ValueError):
        places.map_duckdb_temp_limit_bytes(0)


# --- the readiness check -----------------------------------------------------


def test_accepts_a_stage_whose_allowance_clears_peak_plus_headroom(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances([{"stage": "places.reduce", "allowance_bytes": 1_250}]),
    )
    log = write(tmp_path / "obs.json", [observation("places.reduce", 1_000)])
    report = validator.validate(path, [log])
    assert report["blockers"] == []
    assert report["ready"] is True
    assert report["stages"]["places.reduce"]["required_bytes"] == 1_250


def test_rejects_a_stage_one_byte_short_of_the_headroom(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances([{"stage": "places.reduce", "allowance_bytes": 1_249}]),
    )
    log = write(tmp_path / "obs.json", [observation("places.reduce", 1_000)])
    report = validator.validate(path, [log])
    assert report["ready"] is False
    assert any("plus 25% headroom" in blocker for blocker in report["blockers"])


def test_fails_closed_when_a_stage_has_no_measurement_at_all(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances(
            [
                {
                    "stage": "places.head.tree_merge",
                    "allowance_bytes": 13_690_208_256,
                    "measured_peak_bytes": None,
                    "measured_peak_lower_bound_bytes": 9_126_805_504,
                    "measurement_note": "run one head to completion",
                }
            ]
        ),
    )
    report = validator.validate(path, [])
    assert report["ready"] is False
    blocker = report["blockers"][0]
    assert "NO measured spill peak" in blocker
    assert "LOWER BOUND of 9126805504" in blocker
    assert "run one head to completion" in blocker


def test_a_blind_sampler_is_not_a_measurement(tmp_path):
    """Peak 0 over one sweep is 'unobserved', not 'did not spill'."""
    path = write(
        tmp_path / "allowances.json",
        allowances([{"stage": "places.reduce", "allowance_bytes": 1_000}]),
    )
    log = write(
        tmp_path / "obs.json", [observation("places.reduce", 0, observations=1)]
    )
    report = validator.validate(path, [log])
    assert report["ready"] is False
    assert any("unobserved stage" in blocker for blocker in report["blockers"])


def test_the_worst_run_wins_across_repeated_observations(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances([{"stage": "places.reduce", "allowance_bytes": 1_250}]),
    )
    log = write(
        tmp_path / "obs.json",
        [
            observation("places.reduce", 400),
            observation("places.reduce", 1_100),
            observation("places.reduce", 900),
        ],
    )
    report = validator.validate(path, [log])
    assert report["stages"]["places.reduce"]["measured_peak_bytes"] == 1_100
    assert report["stages"]["places.reduce"]["runs"] == 3
    assert report["ready"] is False


def test_an_observation_beats_a_stale_checked_in_peak(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances(
            [
                {
                    "stage": "places.reduce",
                    "allowance_bytes": 1_250,
                    "measured_peak_bytes": 100,
                }
            ]
        ),
    )
    log = write(tmp_path / "obs.json", [observation("places.reduce", 2_000)])
    report = validator.validate(path, [log])
    assert report["ready"] is False
    assert report["stages"]["places.reduce"]["measurement_source"] == "observations"


def test_an_undeclared_spilling_stage_is_a_blocker(tmp_path):
    path = write(
        tmp_path / "allowances.json",
        allowances(
            [
                {
                    "stage": "places.reduce",
                    "allowance_bytes": 1_250,
                    "measured_peak_bytes": 100,
                }
            ]
        ),
    )
    log = write(tmp_path / "obs.json", [observation("mystery.stage", 5)])
    report = validator.validate(path, [log])
    assert report["ready"] is False
    assert any("is not declared" in blocker for blocker in report["blockers"])


def test_observations_are_scraped_out_of_a_raw_run_log(tmp_path):
    log = tmp_path / "job.log"
    log.write_text(
        "2026-08-04T00:00:01.0000000Z building head\n"
        "2026-08-04T00:00:02.0000000Z "
        + json.dumps({validator.OBSERVATION_KEY: observation("places.reduce", 1_000)})
        + "\nnot json at all\n"
    )
    path = write(
        tmp_path / "allowances.json",
        allowances([{"stage": "places.reduce", "allowance_bytes": 1_250}]),
    )
    report = validator.validate(path, [log])
    assert report["observation_records"] == 1
    assert report["ready"] is True


def test_a_malformed_allowances_document_is_a_hard_error(tmp_path):
    path = write(tmp_path / "allowances.json", {"schema": "something-else"})
    with pytest.raises(SystemExit):
        validator.validate(path, [])

    path = write(
        tmp_path / "bad-headroom.json",
        {
            "schema": validator.ALLOWANCES_SCHEMA,
            "headroom_fraction": 0,
            "stages": [{"stage": "a", "allowance_bytes": 1}],
        },
    )
    with pytest.raises(SystemExit):
        validator.validate(path, [])


# --- the checked-in declaration ---------------------------------------------


def test_the_checked_in_allowances_declare_every_instrumented_stage():
    document = json.loads(ALLOWANCES.read_text())
    declared = {entry["stage"] for entry in document["stages"]}
    sources = (
        (ROOT / "scripts/places_construction_v1.py").read_text()
        + (ROOT / "scripts/address_construction_v1.py").read_text()
    )
    instrumented = set()
    for line in sources.splitlines():
        stripped = line.strip()
        if stripped.startswith('stage="') and stripped.endswith('",'):
            instrumented.add(stripped[len('stage="') : -len('",')])
    assert instrumented, "no instrumented StageWatchdog call sites were found"
    missing = instrumented - declared
    assert not missing, (
        f"instrumented DuckDB stages with no stated allowance: {sorted(missing)}"
    )


def test_the_checked_in_state_is_honestly_not_ready():
    """Today every stage is unmeasured, so the gate must NOT pass.

    A green result here would mean the check had been made permissive, which is
    the one outcome that would make it worse than not existing.
    """
    report = validator.validate(ALLOWANCES, [])
    assert report["ready"] is False
    assert len(report["blockers"]) == len(report["stages"])
    assert all("NO measured spill peak" in blocker for blocker in report["blockers"])


def test_the_head_allowance_matches_the_value_that_completed_v4():
    document = json.loads(ALLOWANCES.read_text())
    head = next(
        entry
        for entry in document["stages"]
        if entry["stage"] == "places.head.tree_merge"
    )
    assert head["allowance_bytes"] == 13_690_208_256
    assert document["hosted_max_scratch_bytes"] == 18_253_611_008


def test_cli_exits_nonzero_and_writes_its_report(tmp_path, capsys):
    output = tmp_path / "report.json"
    code = validator.main(
        ["--allowances", str(ALLOWANCES), "--output", str(output)]
    )
    assert code == 1
    written = json.loads(output.read_text())
    assert written["schema"] == validator.SCHEMA
    assert written["ready"] is False
    assert "BLOCKER" in capsys.readouterr().out
