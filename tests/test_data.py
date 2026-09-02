"""Loading logs in the shapes real loggers produce."""

from __future__ import annotations

import numpy as np
import pytest

from telemetry_overlay.data import LoadOptions, load

from conftest import CSV_SAMPLE, XLSX_SAMPLE, write_csv


def test_reads_semicolon_and_comma_decimals(tmp_path):
    """The bundled sample uses ';' fields and ',' decimals - both sniffed."""
    telemetry = load(CSV_SAMPLE)
    assert telemetry.time.size == 566
    assert "thrust" in telemetry
    assert telemetry["thrust"].unit == "N"
    assert telemetry["thrust"].values.max() == pytest.approx(269.44, abs=0.01)


def test_prefers_the_seconds_column_over_milliseconds():
    """The sample carries Time(ms) and Time(s); seconds must win."""
    telemetry = load(CSV_SAMPLE)
    assert telemetry.t0 == pytest.approx(-0.149)
    assert telemetry.t1 == pytest.approx(5.991)


def test_boolean_columns_are_detected():
    telemetry = load(CSV_SAMPLE)
    assert telemetry["pyro_active"].is_boolean
    assert set(np.unique(telemetry["pyro_active"].values)) <= {0.0, 1.0}


def test_comma_separated_file(tmp_path):
    path = write_csv(tmp_path / "plain.csv",
                     "Time [s],Speed [km/h]\n0,0\n0.1,12.5\n0.2,25\n")
    telemetry = load(path)
    assert telemetry.time.tolist() == pytest.approx([0.0, 0.1, 0.2])
    assert telemetry["speed"].values.tolist() == pytest.approx([0.0, 12.5, 25.0])
    assert telemetry["speed"].unit == "km/h"


def test_tab_separated_file(tmp_path):
    path = write_csv(tmp_path / "tabs.tsv",
                     "Time\tAltitude\n0\t100\n1\t250\n2\t410\n")
    telemetry = load(path)
    assert telemetry["altitude"].values.tolist() == pytest.approx([100, 250, 410])


def test_millisecond_time_is_converted_to_seconds(tmp_path):
    path = write_csv(tmp_path / "ms.csv",
                     "Time [ms];Thrust [N]\n0;0\n100;5\n200;9\n")
    telemetry = load(path)
    assert telemetry.time.tolist() == pytest.approx([0.0, 0.1, 0.2])


def test_bare_millisecond_column_is_guessed_from_the_step(tmp_path):
    """No unit in the header: a ~10 ms step must not be read as 10 seconds."""
    path = write_csv(tmp_path / "bare.csv",
                     "Time;Value\n" + "".join(f"{i * 10};{i}\n" for i in range(20)))
    telemetry = load(path)
    assert telemetry.duration == pytest.approx(0.19)


def test_explicit_time_unit_overrides_the_guess(tmp_path):
    path = write_csv(tmp_path / "bare.csv",
                     "Time;Value\n" + "".join(f"{i * 10};{i}\n" for i in range(20)))
    telemetry = load(path, LoadOptions(time_unit="s"))
    assert telemetry.duration == pytest.approx(190.0)


def test_unknown_time_column_lists_the_alternatives(tmp_path):
    path = write_csv(tmp_path / "x.csv", "Time;Value\n0;1\n1;2\n")
    with pytest.raises(ValueError, match="not found"):
        load(path, LoadOptions(time_column="nope"))


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "absent.csv")


def test_reads_xlsx_and_its_metadata_sheet():
    telemetry = load(XLSX_SAMPLE)
    assert telemetry.time.size == 1205
    # The workbook's own Summary reports a 120.41 N peak, which is the
    # rest-corrected channel rather than the raw one.
    assert telemetry["thrust_above_rest"].values.max() == pytest.approx(120.41,
                                                                        abs=0.01)
    assert telemetry["thrust"].values.max() == pytest.approx(120.43, abs=0.01)
    # The Summary sheet is picked up as metadata.
    assert telemetry.meta.get("Motor designation") == "H59"
    assert telemetry.meta.get("Total impulse") == "223.96 N.s"


def test_time_column_of_only_zeros_and_ones_is_still_time(tmp_path):
    """A two-row log looks boolean; the header must still win."""
    path = write_csv(tmp_path / "short.csv", "Time [s];Flag\n0;0\n1;1\n")
    telemetry = load(path)
    assert telemetry.time.tolist() == pytest.approx([0.0, 1.0])
    assert "flag" in telemetry


def test_value_at_interpolates_and_clamps(tmp_path):
    path = write_csv(tmp_path / "v.csv", "Time [s];V\n0;0\n1;10\n2;20\n")
    telemetry = load(path)
    assert telemetry.value_at("v", 0.5) == pytest.approx(5.0)
    assert telemetry.value_at("v", -5) == pytest.approx(0.0)     # before the log
    assert telemetry.value_at("v", 99) == pytest.approx(20.0)    # after the log


def test_booleans_are_not_interpolated(tmp_path):
    path = write_csv(tmp_path / "b.csv", "Time [s];Flag\n0;0\n1;1\n")
    telemetry = load(path)
    # Halfway between 0 and 1 a flag must still read as a flag.
    assert telemetry.value_at("flag", 0.5) in (0.0, 1.0)


def test_duplicate_column_names_do_not_collide(tmp_path):
    path = write_csv(tmp_path / "d.csv", "Time [s];V [N];V [m]\n0;1;2\n1;3;4\n")
    telemetry = load(path)
    assert "v" in telemetry and "v_2" in telemetry


def test_sample_rate_is_robust_to_a_dropped_sample(tmp_path):
    rows = "".join(f"{i * 0.01:.2f};{i}\n" for i in range(50))
    path = write_csv(tmp_path / "r.csv", "Time [s];V\n" + rows + "5.0;99\n")
    telemetry = load(path)
    assert telemetry.sample_rate == pytest.approx(100.0, rel=0.05)
