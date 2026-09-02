"""The command line surface and the export paths."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from telemetry_overlay.cli import main
from telemetry_overlay.config import load_config
from telemetry_overlay.data import load
from telemetry_overlay.events import auto_events, build_events
from telemetry_overlay.export import (
    _chunks, _segment_ranges, codec_for_path, export_frames, find_ffmpeg,
)

from conftest import CSV_SAMPLE, PRESETS, XLSX_SAMPLE


def test_inspect_lists_channels(capsys):
    assert main(["inspect", str(CSV_SAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "thrust" in out and "pyro_active" in out
    assert "IGNITION" in out and "PEAK" in out


def test_init_writes_a_loadable_config(tmp_path, capsys):
    out = tmp_path / "overlay.json"
    assert main(["init", str(CSV_SAMPLE), "-o", str(out)]) == 0
    config = load_config(out)
    assert config.widgets
    assert config.width == 1920


def test_init_refuses_to_clobber(tmp_path):
    out = tmp_path / "overlay.json"
    out.write_text("{}", encoding="utf-8")
    assert main(["init", str(CSV_SAMPLE), "-o", str(out)]) == 2


def test_preview_writes_a_transparent_png(tmp_path):
    out = tmp_path / "preview.png"
    assert main(["preview", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "--at", "peak", "-r", "640x360"]) == 0
    image = Image.open(out)
    assert image.size == (640, 360)
    alpha = np.array(image.getchannel("A"))
    assert alpha.max() == 255 and (alpha == 0).mean() > 0.5


@pytest.mark.parametrize("at", ["2.3", "50%", "peak", "ignition", "burnout"])
def test_preview_accepts_every_at_form(tmp_path, at):
    out = tmp_path / f"p_{at.strip('%')}.png"
    assert main(["preview", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "--at", at, "-r", "320x180"]) == 0


def test_preview_rejects_an_unknown_event(tmp_path, capsys):
    out = tmp_path / "p.png"
    assert main(["preview", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "--at", "splashdown"]) == 2
    assert "Known events" in capsys.readouterr().err


def test_render_writes_a_png_sequence(tmp_path):
    out = tmp_path / "frames"
    assert main(["render", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "-r", "320x180", "--start", "2.0", "--end", "2.2",
                 "--speed", "1", "-j", "1", "--quiet"]) == 0
    frames = sorted(out.glob("overlay_*.png"))
    assert len(frames) == 12          # 0.2 s at 60 fps
    assert Image.open(frames[0]).size == (320, 180)


def test_render_refuses_to_mix_with_existing_frames(tmp_path):
    out = tmp_path / "frames"
    args = ["render", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
            "-r", "160x90", "--start", "2.0", "--end", "2.1", "--speed", "1",
            "-j", "1", "--quiet"]
    assert main(args) == 0
    assert main(args) == 2            # second run without --overwrite
    assert main(args + ["--overwrite"]) == 0


def test_render_dry_run_writes_nothing(tmp_path, capsys):
    out = tmp_path / "frames"
    assert main(["render", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert not out.exists()


def test_ramp_and_hold_flags_lengthen_the_clip(capsys):
    base = ["render", str(CSV_SAMPLE), "-c", "minimal", "--dry-run",
            "--start", "0", "--end", "4", "--speed", "1"]
    main(base)
    plain = capsys.readouterr().out
    main(base + ["--ramp", "1:2:0.25", "--hold", "2.5:1.0"])
    ramped = capsys.readouterr().out

    def frames(text):
        line = next(l for l in text.splitlines() if "frames @" in l)
        return int(line.split()[2])

    assert frames(ramped) > frames(plain)


@pytest.mark.parametrize("bad", ["1:2", "a:b:c", "1:2:3:4:5"])
def test_bad_ramp_syntax_is_reported(bad, capsys):
    assert main(["render", str(CSV_SAMPLE), "-c", "minimal", "--dry-run",
                 "--ramp", bad]) == 2
    assert "--ramp" in capsys.readouterr().err


def test_presets_command_lists_the_bundled_files(capsys):
    assert main(["presets"]) == 0
    out = capsys.readouterr().out
    for name in ("rocket_thrust", "minimal", "hud", "broadcast"):
        assert name in out


def test_unknown_preset_names_the_available_ones(capsys):
    assert main(["render", str(CSV_SAMPLE), "-c", "nonesuch",
                 "--dry-run"]) == 2
    assert "Bundled presets" in capsys.readouterr().err


def test_xlsx_end_to_end(tmp_path):
    out = tmp_path / "p.png"
    assert main(["preview", str(XLSX_SAMPLE), "-c", "minimal", "-o", str(out),
                 "-r", "320x180", "--at", "peak"]) == 0


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "COMMAND" in capsys.readouterr().out


# -- export helpers -------------------------------------------------------

def test_chunks_cover_every_frame_exactly_once():
    for total in (1, 7, 100, 793):
        for workers in (1, 3, 8):
            ranges = _chunks(total, workers)
            assert ranges[0][0] == 0 and ranges[-1][1] == total
            covered = [i for a, b in ranges for i in range(a, b)]
            assert covered == list(range(total))


def test_segment_ranges_cover_every_frame_exactly_once():
    for total in (1, 9, 250):
        for workers in (1, 4, 8):
            ranges = _segment_ranges(total, workers)
            covered = [i for a, b in ranges for i in range(a, b)]
            assert covered == list(range(total))


def test_codec_is_chosen_from_the_extension():
    assert codec_for_path("a.mov").name == "prores4444"
    assert codec_for_path("a.webm").name == "vp9"
    assert codec_for_path("a.mp4").name == "h264"
    assert codec_for_path("a.mp4").alpha is False
    assert codec_for_path("a.mov", "qtrle").name == "qtrle"


def test_unknown_codec_is_rejected():
    with pytest.raises(ValueError, match="unknown codec"):
        codec_for_path("a.mov", "divx")


def test_parallel_and_serial_frames_are_identical(tmp_path):
    """Worker processes must produce byte-identical frames."""
    config = load_config(PRESETS / "minimal.yaml",
                         {"resolution": "320x180", "start": 2.0, "end": 2.3,
                          "speed": 1.0})
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"
    export_frames(config, CSV_SAMPLE, serial, workers=1, overwrite=True)
    export_frames(config, CSV_SAMPLE, parallel, workers=3, overwrite=True)
    a = sorted(serial.glob("*.png"))
    b = sorted(parallel.glob("*.png"))
    assert len(a) == len(b) > 0
    for x, y in zip(a, b):
        assert x.read_bytes() == y.read_bytes()


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg is not available")
def test_render_writes_a_video_with_alpha(tmp_path):
    out = tmp_path / "clip.mov"
    assert main(["render", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out),
                 "-r", "320x180", "--start", "2.0", "--end", "2.2",
                 "--speed", "1", "--codec", "qtrle", "-j", "1",
                 "--quiet"]) == 0
    assert out.exists() and out.stat().st_size > 1000


# -- events ---------------------------------------------------------------

def test_detected_events_match_the_workbook_summary():
    """The xlsx ships its own analysis; ours must agree with it."""
    telemetry = load(XLSX_SAMPLE)
    events = {e["key"]: e["time"] for e in auto_events(telemetry, "thrust")}
    assert events["ignition"] == pytest.approx(0.292, abs=0.002)   # 5 % of peak
    assert events["peak"] == pytest.approx(2.705, abs=0.002)       # time to peak
    assert events["burnout"] == pytest.approx(4.063, abs=0.002)    # burn time


def test_explicit_events_are_kept_in_order():
    telemetry = load(CSV_SAMPLE)
    events = build_events(telemetry, [
        {"time": 3.0, "label": "LATER"},
        {"time": 1.0, "label": "EARLIER"},
    ])
    assert [e["label"] for e in events] == ["EARLIER", "LATER"]


def test_events_can_be_switched_off():
    assert build_events(load(CSV_SAMPLE), False) == []


def test_malformed_event_is_reported():
    with pytest.raises(ValueError, match="needs 'time'"):
        build_events(load(CSV_SAMPLE), [{"label": "nowhere"}])


def test_optional_event_rule_is_skipped_when_absent():
    telemetry = load(CSV_SAMPLE)
    events = build_events(telemetry, [
        {"edges": "no_such_flag", "optional": True},
        {"detect": "thrust"},
    ])
    assert [e["key"] for e in events] == ["ignition", "peak", "burnout"]


def test_non_optional_event_rule_still_reports_a_missing_channel():
    with pytest.raises(ValueError, match="available"):
        build_events(load(CSV_SAMPLE), [{"edges": "no_such_flag"}])


def test_event_detect_accepts_an_alias_list():
    telemetry = load(XLSX_SAMPLE)
    events = build_events(telemetry, [
        {"detect": ["thrust", "force"], "only": ["peak"]},
    ])
    assert events[0]["time"] == pytest.approx(2.705, abs=0.002)
