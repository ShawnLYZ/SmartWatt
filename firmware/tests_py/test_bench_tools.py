"""The bench tools under firmware/tools, tested without hardware.

Each tool reads something the firmware writes -- a raw capture, a status
line -- so the tests below pin the tool to the FIRMWARE's own source where
they can (its printf format, calibration.h's constants) rather than to a
copy typed here. A copy would keep passing after the firmware changed, which
is exactly the drift these tests exist to catch.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[1]
TOOLS = FIRMWARE / "tools"
MAIN_CPP = FIRMWARE / "src" / "main.cpp"
CALIBRATION_H = FIRMWARE / "lib" / "metrics" / "calibration.h"
FEATURES_CPP = FIRMWARE / "lib" / "features" / "features.cpp"
FINGERPRINTS_H = FIRMWARE / "lib" / "classify" / "fingerprints.h"
CONTRACT = FIRMWARE.parent / "contract"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"smartwatt_bench_{name}", TOOLS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before running it: dataclasses looks its module up here.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture_tool = _load("capture_to_fixture")
capture_stats = _load("capture_stats")
serial_stats = _load("serial_stats")
train_plugs = _load("train_plugs")


def _calibration_constant(name: str) -> int:
    match = re.search(rf"\b{name}\s*=\s*(\d+);", CALIBRATION_H.read_text())
    assert match, f"{name} not found in calibration.h"
    return int(match.group(1))


def _firmware_status_format() -> str:
    """main.cpp's status-line printf format, joined from its literals."""
    source = MAIN_CPP.read_text(encoding="utf-8")
    assert source.count('"Vrms=') == 1, "expected exactly one status format"
    literal = re.compile(r'\s*"((?:[^"\\]|\\.)*)"')
    parts, position = [], source.index('"Vrms=')
    while (match := literal.match(source, position)) is not None:
        parts.append(match.group(1))
        position = match.end()
    return "".join(parts).replace("\\n", "\n")


def _status_line(p=40.12, q1=0.05, overruns=0, worst=71, vclip=0, range_="low"):
    # Python's % takes C's length modifier and ignores it, and %u is %d, so
    # the firmware's format renders here unmodified.
    return _firmware_status_format() % (
        239.123, 0.1667, p, q1, 3.2, 0.999, 50.012, range_, overruns, worst, vclip
    )


# -- capture_to_fixture.py ---------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakePort:
    """Replays scripted device output: `replies[n]` follows the n-th write.

    An empty read advances the clock by the real port's 0.5 s timeout, so a
    silent device reaches the deadline instead of spinning forever.
    """

    def __init__(self, replies: list[list[str]], clock: FakeClock) -> None:
        self.replies = [list(reply) for reply in replies]
        self.pending: list[str] = []
        self.writes: list[bytes] = []
        self.clock = clock

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self.replies:
            self.pending.extend(self.replies.pop(0))
        return len(data)

    def readline(self) -> bytes:
        if self.pending:
            return (self.pending.pop(0) + "\r\n").encode("ascii")
        self.clock.now += 0.5
        return b""

    # What main() needs besides the protocol.
    def reset_input_buffer(self) -> None:
        pass

    def __enter__(self) -> FakePort:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


ROWS = [f"{2048 + n},{2040 + n},{2050 - n}" for n in range(5)]
COMPLETE = "CAPTURE complete -- sampler health counters cleared"


def _capture(replies, samples=5, timeout_s=10.0):
    clock = FakeClock()
    port = FakePort(replies, clock)
    return capture_tool.capture(port, samples, timeout_s, clock=clock), port


def test_rows_are_validated_and_normalised():
    assert capture_tool.parse_row("2048,1,4095") == "2048,1,4095"
    assert capture_tool.parse_row(" 2048 , 01 ,4095") == "2048,1,4095"
    for bad in ("4096,1,1", "-1,1,1", "1,2", "1,2,3,4", "a,b,c", "", _status_line()):
        assert capture_tool.parse_row(bad) is None, bad


def test_every_row_comes_back_in_order_and_status_lines_are_skipped():
    rows, port = _capture([[_status_line(), "WITHHOLDING PUBLISH: clock NOT set",
                            *ROWS, COMPLETE]])
    assert rows == ROWS
    assert port.writes == [b"CAPTURE\n"]


def test_a_short_capture_is_refused():
    with pytest.raises(capture_tool.CaptureError, match="4 intact rows"):
        _capture([[*ROWS[:4], COMPLETE]])


def test_an_extra_row_is_refused():
    with pytest.raises(capture_tool.CaptureError, match="6 intact rows"):
        _capture([[*ROWS, "2048,2048,2048", COMPLETE]])


def test_a_mangled_row_leaves_the_count_short_and_is_refused():
    mangled = [*ROWS[:2], "2048,20", *ROWS[3:], COMPLETE]
    with pytest.raises(capture_tool.CaptureError, match="not contiguous"):
        _capture([mangled])


def test_a_log_line_between_intact_rows_does_not_spoil_them():
    interleaved = [*ROWS[:2], "E (51234) wifi: sta disconnected", *ROWS[2:], COMPLETE]
    rows, _ = _capture([interleaved])
    assert rows == ROWS


def test_a_board_that_reboots_after_the_command_gets_it_again():
    boot = ["ESP-ROM:esp32s3-20210327", "SmartWatt bring-up",
            "Calibration: none stored in NVS -- using compile-time defaults"]
    rows, port = _capture([boot, [*ROWS, COMPLETE]])
    assert rows == ROWS
    assert port.writes == [b"CAPTURE\n", b"CAPTURE\n"]


def test_a_rebooting_board_gets_the_command_once_more_not_forever():
    banner = ["SmartWatt bring-up"]
    clock = FakeClock()
    port = FakePort([banner, banner, banner], clock)
    with pytest.raises(capture_tool.CaptureError):
        capture_tool.capture(port, 5, 3.0, clock=clock)
    assert port.writes == [b"CAPTURE\n", b"CAPTURE\n"]


def test_a_silent_port_times_out_on_time_with_what_to_check():
    clock = FakeClock()
    port = FakePort([[]], clock)
    with pytest.raises(capture_tool.CaptureError, match="UART port"):
        capture_tool.capture(port, 5, 3.0, clock=clock)
    # Gave up AT the deadline, not eventually: within one 0.5 s read of it.
    assert 3.0 <= clock.now <= 3.5


def test_the_fixture_is_written_with_lf_line_endings(tmp_path, monkeypatch):
    """The fixtures are -text in .gitattributes: the C side reads them byte
    for byte. On Windows a plain write_text would emit CRLF."""
    clock = FakeClock()
    port = FakePort([[*ROWS, COMPLETE]], clock)
    monkeypatch.setattr(capture_tool, "open_port", lambda name, baud: port)
    monkeypatch.setattr(capture_tool.time, "sleep", lambda seconds: None)
    out = tmp_path / "real-kettle.csv"

    code = capture_tool.main(["--port", "COM9", "--samples", "5", "--out", str(out)])

    assert code == 0
    assert out.read_bytes() == ("v,i_low,i_high\n" + "\n".join(ROWS) + "\n").encode()


def test_a_failed_capture_writes_nothing_and_exits_nonzero(tmp_path, monkeypatch):
    clock = FakeClock()
    port = FakePort([[*ROWS[:4], COMPLETE]], clock)
    monkeypatch.setattr(capture_tool, "open_port", lambda name, baud: port)
    monkeypatch.setattr(capture_tool.time, "sleep", lambda seconds: None)
    out = tmp_path / "real-kettle.csv"

    code = capture_tool.main(["--port", "COM9", "--samples", "5", "--out", str(out)])

    assert code == 1
    assert not out.exists()


# -- capture_stats.py --------------------------------------------------------


def test_capture_stats_uses_the_firmwares_own_adc_constants():
    assert capture_stats.ADC_MID == _calibration_constant("kAdcMid")
    assert capture_stats.ADC_MAX == _calibration_constant("kAdcMax")
    assert capture_stats.CLIP_MARGIN == _calibration_constant("kClipMargin")


def test_clipped_means_what_the_firmware_means():
    """cycle.cpp is_clipped(): raw <= kClipMargin || raw >= kAdcMax - kClipMargin."""
    low, high = capture_stats.CLIP_MARGIN, capture_stats.ADC_MAX - capture_stats.CLIP_MARGIN
    assert capture_stats.summarise("v", [2048, high]).clipped
    assert not capture_stats.summarise("v", [2048, high - 1]).clipped
    assert capture_stats.summarise("v", [low, 2048]).clipped
    assert not capture_stats.summarise("v", [low + 1, 2048]).clipped


def test_bias_and_both_peaks_are_reported():
    # A 30-count sine riding on a bias 2 counts above mid-scale, over exactly
    # four whole periods, so its mean is the bias.
    counts = [2050 + round(30 * math.sin(2 * math.pi * n / 80)) for n in range(320)]
    stats = capture_stats.summarise("i_low", counts)
    assert stats.bias_error == pytest.approx(2.0, abs=1e-9)
    assert stats.peak_from_mid == 32  # the spec's definition: from 2048
    assert stats.peak_from_mean == pytest.approx(30.0)  # the signal's own


def test_noise_is_the_population_standard_deviation():
    # Every sample in a capture is the population, not a draw from one: two
    # samples 4 counts apart spread 2 counts, where the sample formula says 2.83.
    assert capture_stats.summarise("i_low", [2046, 2050]).sd == 2.0


def test_a_capture_file_reads_back_by_channel(tmp_path):
    path = tmp_path / "idle.csv"
    path.write_text("v,i_low,i_high\n1,2,3\n4,5,6\n", encoding="utf-8")
    assert capture_stats.read_capture(path) == {
        "v": [1, 4], "i_low": [2, 5], "i_high": [3, 6]
    }
    assert capture_stats.main([str(path)]) == 0


# -- serial_stats.py ---------------------------------------------------------


def test_the_firmwares_status_line_parses_field_for_field():
    """The real printf format, rendered and read back. A renamed, reordered or
    dropped field in main.cpp fails here rather than blinding the tool."""
    rows = serial_stats.parse_lines(
        [_status_line(p=-3.5, q1=12.25, overruns=2, worst=88, vclip=7, range_="high")]
    )
    assert len(rows) == 1
    row = rows[0]
    assert (row.vrms, row.irms, row.p, row.q1) == (239.12, 0.1667, -3.5, 12.25)
    assert (row.d, row.pf, row.f) == (3.2, 0.999, 50.012)
    assert (row.range, row.overruns, row.worst_us, row.vclip) == ("high", 2, 88, 7)


def test_non_status_lines_are_ignored():
    lines = ["SmartWatt bring-up", "Clock: SET (ts=1758800000.000)", _status_line()]
    assert len(serial_stats.parse_lines(lines)) == 1


def test_the_summary_averages_and_derives_phi1_from_the_means():
    rows = serial_stats.parse_lines(
        [_status_line(p=p, q1=1.4, worst=w) for p, w in ((79.8, 60), (80.0, 70), (80.2, 90))]
    )
    summary = serial_stats.summarise(rows)
    assert summary.count == 3
    assert summary.mean["p"] == pytest.approx(80.0)
    assert summary.sd["p"] == pytest.approx(math.sqrt(0.08 / 3))
    assert summary.phi1_rad == pytest.approx(math.atan2(1.4, 80.0))
    assert summary.last.worst_us == 90  # soak counters come off the LAST line
    assert summary.ranges == {"low": 3}


@pytest.mark.parametrize(
    ("options", "mean_p"),
    [
        ([], "140.0000"),                          # all four lines
        (["--skip", "1"], " 20.0000"),             # the settling 500 W dropped
        (["--last", "2"], " 25.0000"),             # only the final two
        (["--skip", "1", "--last", "9"], " 20.0000"),  # --last past what is left
    ],
)
def test_skip_and_last_choose_the_lines_averaged(tmp_path, capsys, options, mean_p):
    log = tmp_path / "platformio-device-monitor.log"
    lines = [_status_line(p=float(p)) for p in (500, 10, 20, 30)]
    log.write_text("".join(lines), encoding="utf-8")

    assert serial_stats.main([str(log), *options]) == 0
    p_line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("   P:"))
    assert f"mean {mean_p:>11}" in p_line


def test_a_log_with_no_status_lines_says_so(tmp_path, capsys):
    log = tmp_path / "empty.log"
    log.write_text("SmartWatt bring-up\n", encoding="utf-8")
    assert serial_stats.main([str(log)]) == 1
    assert "no status lines" in capsys.readouterr().out


# -- train_plugs: the bench trainer's fingerprints.csv ------------------------
#
# FileFingerprintSource::load reads the fourteen columns after `edge` BY
# POSITION, so a reordered FEATURE_NAMES would load cleanly and classify on
# transposed dimensions. That failure is silent on the device, which is why
# these tests pin the tool to the C++ source rather than to a list typed here.


def test_train_feature_order_is_the_firmwares():
    body = FEATURES_CPP.read_text(encoding="utf-8")
    block = re.search(r"kFeatureNames\[kFeatureCount\]\s*=\s*\{(.*?)\};", body, re.S)
    assert block, "kFeatureNames not found in features.cpp"
    assert tuple(re.findall(r'"([a-z0-9_]+)"', block.group(1))) == train_plugs.FEATURE_NAMES


def test_train_features_are_the_event_contracts():
    import json

    schema = json.loads((CONTRACT / "schemas" / "event.schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]["features"]["properties"]) == set(train_plugs.FEATURE_NAMES)


def test_train_length_limits_are_fingerprint_rows():
    header = FINGERPRINTS_H.read_text(encoding="utf-8")
    label = int(re.search(r"char label\[(\d+)\]", header).group(1))
    training_id = int(re.search(r"char training_id\[(\d+)\]", header).group(1))
    # The loader refuses a label that would not fit WITH its NUL (size() >= sizeof).
    assert train_plugs.LABEL_MAX == label - 1
    assert train_plugs.TRAINING_ID_MAX == training_id - 1


@pytest.mark.parametrize("label", [
    "kettle", "desk_fan", "incandescent_lamp", "tv2", "a", "unknown_device",
    "unknown_", "unknown_1a", "washing_machine_2", "apple_charger", "phone_charger",
])
def test_train_admits_what_the_loader_admits(label):
    # The same lists as test_knn.cpp's test_is_emittable_label_matches_the_contract_pattern.
    assert train_plugs.label_problem(label) is None


@pytest.mark.parametrize("label", [
    "", "Desk Fan", "Kettle", "desk fan", "desk-fan", "2kettle", "_kettle", "kettle!",
    'kettle"s', "unknown_1", "unknown_5", "unknown_42", "unknown_0000", "a" * 24,
])
def test_train_refuses_what_the_loader_refuses(label):
    assert train_plugs.label_problem(label) is not None


def _golden_event() -> dict:
    import json

    return json.loads((CONTRACT / "examples" / "event-on-confident.json").read_text(encoding="utf-8"))


def test_train_row_carries_the_features_in_file_order():
    event = _golden_event()
    row = train_plugs.csv_row(event, "s202609271200-001", "apple_charger",
                              {"ts": event["ts"], "session_id": "s202609271200",
                               "background_w": 1.5, "concurrent_ids": "", "notes": "quiet"})
    assert len(row) == len(train_plugs.HEADER) == 3 + 14 + 7
    assert row[:3] == ["s202609271200-001", "apple_charger", "on"]
    assert [float(v) for v in row[3:17]] == [
        float(event["features"][n]) for n in train_plugs.FEATURE_NAMES]
    assert row[17] == repr(event["ts"])


@pytest.mark.parametrize(("change", "refused"), [
    ({}, False),
    ({"reason": "distance_threshold", "rejected": True, "label": "unknown_1"}, False),
    ({"reason": "below_floor"}, True),
    ({"reason": "no_settle"}, True),
    ({"reason": "overlapping_edges"}, True),
    ({"edge": "off"}, True),
])
def test_train_keeps_only_settled_edges_of_the_switched_direction(change, refused):
    event = {**_golden_event(), **change}
    assert (train_plugs.event_problem(event, "on") is not None) is refused


def test_train_refuses_a_non_finite_feature():
    event = _golden_event()
    event["features"] = {**event["features"], "h3_h1": float("nan")}
    assert "h3_h1" in train_plugs.event_problem(event, "on")


def test_train_refuses_a_separator_inside_a_field():
    with pytest.raises(ValueError):
        train_plugs.csv_row(_golden_event(), "id,with,commas", "apple_charger", {})
    with pytest.raises(ValueError):
        train_plugs.csv_row(_golden_event(), "s-001", "apple_charger", {"notes": "a,b"})
