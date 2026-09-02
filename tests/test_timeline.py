"""Time remapping: speed, ramps, holds and their interaction."""

from __future__ import annotations

import numpy as np
import pytest

from telemetry_overlay.timeline import Hold, Ramp, build_timeline


def test_real_time_maps_one_to_one():
    timeline = build_timeline(0, 4, fps=60, speed=1.0)
    assert timeline.frame_count == 240
    assert timeline.duration == pytest.approx(4.0)
    assert timeline.data_time(0) == pytest.approx(0.0)


def test_global_slow_motion_stretches_the_clip():
    timeline = build_timeline(0, 4, fps=60, speed=0.25)
    assert timeline.duration == pytest.approx(16.0)
    assert timeline.frame_count == 960


def test_speeding_up_shortens_the_clip():
    timeline = build_timeline(0, 8, fps=30, speed=4.0)
    assert timeline.duration == pytest.approx(2.0)


def test_frame_times_never_run_backwards():
    timeline = build_timeline(-0.2, 6.0, fps=60, speed=1.0,
                              ramps=[Ramp(1.6, 2.4, 0.125, ease=0.15)],
                              holds=[Hold(at=2.3, duration=1.0)])
    assert np.all(np.diff(timeline.frame_times) >= -1e-9)


def test_ramp_only_slows_its_own_window():
    timeline = build_timeline(0, 4, fps=60, speed=1.0,
                              ramps=[Ramp(1.0, 2.0, 0.25)])
    # One second at quarter speed adds three seconds.
    assert timeline.duration == pytest.approx(7.0, abs=0.05)
    assert timeline.speed_at(1.5) == pytest.approx(0.25, rel=0.02)
    assert timeline.speed_at(3.0) == pytest.approx(1.0, rel=0.02)


def test_easing_blends_the_speed_change():
    timeline = build_timeline(0, 4, fps=60, speed=1.0,
                              ramps=[Ramp(1.0, 2.0, 0.25, ease=0.25)])
    # Mid-transition the speed sits between the base and the ramp target.
    mid = timeline.speed_at(0.875)
    assert 0.25 < mid < 1.0


def test_hold_adds_output_time_without_advancing_data():
    plain = build_timeline(0, 4, fps=60, speed=1.0)
    held = build_timeline(0, 4, fps=60, speed=1.0,
                          holds=[Hold(at=2.0, duration=2.0)])
    assert held.duration == pytest.approx(plain.duration + 2.0, abs=0.05)
    # During the hold the data timestamp stays put.
    start = held.output_time(2.0)
    frames = [held.data_time(int(round((start + d) * 60)))
              for d in (0.2, 0.8, 1.5)]
    assert max(frames) - min(frames) < 1e-3


def test_later_ramps_win_where_they_overlap():
    timeline = build_timeline(0, 4, fps=60, speed=1.0,
                              ramps=[Ramp(1.0, 3.0, 0.5), Ramp(2.0, 3.0, 0.1)])
    assert timeline.speed_at(1.5) == pytest.approx(0.5, rel=0.02)
    assert timeline.speed_at(2.5) == pytest.approx(0.1, rel=0.02)


def test_output_time_is_monotonic():
    timeline = build_timeline(0, 5, fps=60, speed=1.0,
                              ramps=[Ramp(2, 3, 0.2, ease=0.2)],
                              holds=[Hold(at=4.0, duration=0.5)])
    assert np.all(np.diff(timeline.grid_out) >= 0)


def test_round_trip_between_data_and_output_time():
    timeline = build_timeline(0, 5, fps=60, speed=0.5,
                              ramps=[Ramp(1, 2, 0.2, ease=0.1)])
    for t in (0.5, 1.5, 2.5, 4.0):
        assert np.interp(timeline.output_time(t), timeline.grid_out,
                         timeline.grid_data) == pytest.approx(t, abs=1e-3)


def test_rejects_an_empty_window():
    with pytest.raises(ValueError, match="empty time window"):
        build_timeline(2.0, 2.0, fps=60)


def test_rejects_non_positive_speed():
    with pytest.raises(ValueError, match="positive"):
        build_timeline(0, 1, fps=60, speed=0)
    with pytest.raises(ValueError, match="positive"):
        Ramp(0, 1, speed=0)


def test_max_duration_guards_against_a_runaway_render():
    with pytest.raises(ValueError, match="over the"):
        build_timeline(0, 10, fps=60, speed=0.01, max_duration=60)
