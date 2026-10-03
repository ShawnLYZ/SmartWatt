import json
import time

import pytest

from smartwatt_sim.cli import main


def test_no_command_exits_non_zero(capsys):
    assert main([]) != 0


def test_unknown_scenario_lists_the_valid_ones(capsys):
    code = main(["publish", "--scenario", "nope", "--dry-run"])
    assert code != 0
    err = capsys.readouterr().err
    assert "demo" in err and "cliff" in err


def test_publish_dry_run_reports_a_count(capsys):
    assert main(["publish", "--scenario", "single", "--dry-run"]) == 0
    assert "payloads" in capsys.readouterr().out


def test_record_then_replay_dry_run(tmp_path, capsys):
    out = tmp_path / "demo.jsonl"
    assert main(["record", "--scenario", "demo", "--out", str(out)]) == 0
    assert out.exists()
    assert main(["replay", str(out), "--dry-run"]) == 0


def test_replay_marks_source(tmp_path, capsys):
    out = tmp_path / "single.jsonl"
    main(["record", "--scenario", "single", "--out", str(out)])
    assert main(["replay", str(out), "--dry-run", "--print"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    sources = {json.loads(line)["source"] for line in printed if line.startswith("{")}
    assert sources == {"replay"}


def test_waveform_writes_fixture_pair(tmp_path):
    code = main([
        "waveform", "--fixture", "pure-resistive",
        "--active", "kettle", "--out", str(tmp_path),
    ])
    assert code == 0
    assert (tmp_path / "pure-resistive.csv").exists()
    assert (tmp_path / "pure-resistive.sidecar.json").exists()


def test_waveform_distorted_flag(tmp_path):
    main([
        "waveform", "--fixture", "distorted-v", "--active", "led_bulb",
        "--out", str(tmp_path), "--distorted-v",
    ])
    sidecar = json.loads((tmp_path / "distorted-v.sidecar.json").read_text())
    assert sidecar["cross_derivation_holds"] is False


def test_validate_accepts_a_recording(tmp_path, capsys):
    out = tmp_path / "demo.jsonl"
    main(["record", "--scenario", "demo", "--out", str(out)])
    assert main(["validate", str(out)]) == 0


def test_validate_rejects_a_corrupted_recording(tmp_path, capsys):
    out = tmp_path / "bad.jsonl"
    main(["record", "--scenario", "single", "--out", str(out)])
    lines = out.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["payload"]["electrical"]["range"] = "medium"
    lines[0] = json.dumps(entry)
    out.write_text("\n".join(lines) + "\n")
    assert main(["validate", str(out)]) != 0


def _printed_timestamps(capsys):
    lines = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line)["ts"] for line in lines if line.startswith("{")]


def test_publish_stamps_the_fixed_date_by_default(capsys):
    assert main(["publish", "--scenario", "single", "--dry-run", "--print"]) == 0
    assert min(_printed_timestamps(capsys)) == 1754035200.0


def test_publish_now_starts_the_clock_at_the_current_time(capsys):
    """So a first-time user watching the dashboard sees the run on the
    "last 15 minutes" chart, which a 2025 timestamp never reaches."""
    before = time.time()
    assert main(["publish", "--scenario", "single", "--dry-run", "--print", "--now"]) == 0
    after = time.time()
    assert before <= min(_printed_timestamps(capsys)) <= after
