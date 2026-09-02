"""Mapping data time to output frames, with speed ramps and holds.

The overlay does not have to run at wall-clock speed. A static fire lasts
four seconds but the interesting part - ignition - is over in 200 ms, so the
usual edit slows that part down and lets the rest play at full speed.

The mapping is built from a *speed profile*: a function giving, for every
instant of data time, how many seconds of data pass per second of video.
``speed = 1`` is real time, ``speed = 0.25`` is four times slower.
Integrating ``1 / speed`` over data time gives output time, which is
monotonically increasing and therefore invertible by interpolation - that is
how a frame number is turned back into a data timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Sequence

import numpy as np

__all__ = ["Ramp", "Hold", "Timeline", "build_timeline"]

# Sub-millisecond resolution for the integration grid; ramps shorter than
# this are not representable anyway.
_GRID_STEP = 1e-4
_MAX_GRID = 400_000
_MIN_SPEED = 1e-3


def _smoothstep(x: np.ndarray) -> np.ndarray:
    """Classic 3x^2 - 2x^3 ease, clamped to [0, 1]."""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


@dataclass
class Ramp:
    """A stretch of data time played at a different speed.

    ``ease`` is the width, in data seconds, of the transition blended in at
    each end so the speed change does not snap.
    """

    start: float
    end: float
    speed: float
    ease: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            self.start, self.end = self.end, self.start
        if self.speed <= 0:
            raise ValueError(
                f"ramp speed must be positive (got {self.speed}); "
                "use a hold to freeze time"
            )
        if self.ease < 0:
            raise ValueError("ramp ease must not be negative")


@dataclass
class Hold:
    """A freeze frame: the video keeps rolling, data time stands still."""

    at: float
    duration: float
    label: str = ""

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError("hold duration must not be negative")


@dataclass
class Timeline:
    """Frame-by-frame schedule produced by :func:`build_timeline`."""

    fps: float
    frame_times: np.ndarray          # data time for every output frame
    data_start: float
    data_end: float
    grid_data: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    grid_out: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    ramps: list[Ramp] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return int(self.frame_times.size)

    @property
    def duration(self) -> float:
        """Length of the rendered clip in seconds."""
        return self.frame_count / self.fps if self.fps else 0.0

    def data_time(self, frame: int) -> float:
        idx = int(np.clip(frame, 0, self.frame_count - 1))
        return float(self.frame_times[idx])

    def output_time(self, data_t: float) -> float:
        """Where a data timestamp lands in the finished clip."""
        if self.grid_data.size == 0:
            return 0.0
        return float(np.interp(data_t, self.grid_data, self.grid_out))

    def speed_at(self, data_t: float) -> float:
        """Effective playback speed at a data timestamp."""
        if self.grid_data.size < 2:
            return 1.0
        idx = int(np.clip(np.searchsorted(self.grid_data, data_t), 1,
                          self.grid_data.size - 1))
        d_data = self.grid_data[idx] - self.grid_data[idx - 1]
        d_out = self.grid_out[idx] - self.grid_out[idx - 1]
        if d_out <= 0:
            return 0.0
        return float(d_data / d_out)

    def __iter__(self) -> Iterator[tuple[int, float]]:
        for i, t in enumerate(self.frame_times):
            yield i, float(t)

    def describe(self) -> str:
        span = self.data_end - self.data_start
        avg = span / self.duration if self.duration else 0.0
        lines = [
            f"data window   : {self.data_start:.3f} s .. {self.data_end:.3f} s "
            f"({span:.3f} s of data)",
            f"output        : {self.frame_count} frames @ {self.fps:g} fps "
            f"= {self.duration:.2f} s of video",
            f"average speed : {avg:.3f}x",
        ]
        for ramp in self.ramps:
            lines.append(
                f"  ramp  {ramp.start:8.3f} .. {ramp.end:8.3f} s  "
                f"speed {ramp.speed:g}x  ease {ramp.ease:g} s"
                + (f"  [{ramp.label}]" if ramp.label else "")
            )
        for hold in self.holds:
            lines.append(
                f"  hold  {hold.at:8.3f} s  for {hold.duration:g} s of video"
                + (f"  [{hold.label}]" if hold.label else "")
            )
        return "\n".join(lines)


def _speed_profile(
    grid: np.ndarray, base_speed: float, ramps: Sequence[Ramp]
) -> np.ndarray:
    """Speed at every point of the integration grid.

    Later ramps win over earlier ones where they overlap, which makes a
    config read top-to-bottom like a list of overrides.
    """
    speed = np.full(grid.shape, float(base_speed), dtype=float)
    for ramp in ramps:
        target = np.full(grid.shape, float(ramp.speed), dtype=float)
        if ramp.ease > 0:
            # Blend weight: 0 outside the ramp, 1 in its core, eased at the edges.
            rise = _smoothstep((grid - (ramp.start - ramp.ease)) / ramp.ease)
            fall = _smoothstep(((ramp.end + ramp.ease) - grid) / ramp.ease)
            weight = np.clip(np.minimum(rise, fall), 0.0, 1.0)
        else:
            weight = ((grid >= ramp.start) & (grid <= ramp.end)).astype(float)
        speed = speed * (1.0 - weight) + target * weight
    return np.maximum(speed, _MIN_SPEED)


def build_timeline(
    data_start: float,
    data_end: float,
    fps: float = 60.0,
    speed: float = 1.0,
    ramps: Sequence[Ramp] | None = None,
    holds: Sequence[Hold] | None = None,
    max_duration: float | None = None,
) -> Timeline:
    """Turn a data window plus speed instructions into a frame schedule.

    Parameters
    ----------
    data_start, data_end:
        The slice of the log to cover, in data seconds.
    fps:
        Output frame rate; match the timeline you will drop the frames onto.
    speed:
        Base playback speed. ``0.25`` renders everything four times slower.
    ramps:
        Sections played at a different speed than the base.
    holds:
        Freeze frames inserted at given data timestamps.
    max_duration:
        Safety valve: refuse to build a clip longer than this many seconds.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    if speed <= 0:
        raise ValueError("base speed must be positive")
    if data_end <= data_start:
        raise ValueError(
            f"empty time window: start {data_start} is not before end {data_end}"
        )

    ramps = list(ramps or [])
    holds = list(holds or [])
    span = data_end - data_start

    # Integration grid: fine enough to resolve the shortest ease, capped so a
    # long log cannot allocate an unbounded array.
    n = int(min(_MAX_GRID, max(2048, span / _GRID_STEP)))
    grid = np.linspace(data_start, data_end, n)
    profile = _speed_profile(grid, speed, ramps)

    # out_time = integral of 1/speed over data time (cumulative trapezoid).
    inv = 1.0 / profile
    steps = np.diff(grid)
    increments = steps * 0.5 * (inv[:-1] + inv[1:])
    out = np.concatenate(([0.0], np.cumsum(increments)))

    # Holds add output time without advancing data time. Insert them as a pair
    # of samples at the same data timestamp so the mapping stays invertible.
    for hold in sorted(holds, key=lambda h: h.at):
        if hold.duration <= 0:
            continue
        at = float(np.clip(hold.at, data_start, data_end))
        idx = int(np.searchsorted(grid, at))
        at_out = float(np.interp(at, grid, out))
        # Everything after the hold shifts later in the finished clip.
        out[idx:] += hold.duration
        grid = np.insert(grid, idx, at)
        out = np.insert(out, idx, at_out)

    # np.interp needs a strictly increasing x; a hold makes two samples share
    # an output time only at the seam, so nudge duplicates apart.
    out = np.maximum.accumulate(out)
    duplicates = np.flatnonzero(np.diff(out) <= 0)
    if duplicates.size:
        out[duplicates + 1] = out[duplicates] + 1e-9
        out = np.maximum.accumulate(out)

    total = float(out[-1])
    if max_duration is not None and total > max_duration:
        raise ValueError(
            f"the requested settings produce {total:.1f} s of video, over the "
            f"{max_duration:.1f} s limit - raise --max-duration or speed things up"
        )

    frame_count = max(1, int(round(total * fps)))
    out_times = np.arange(frame_count, dtype=float) / fps
    # Invert: output time -> data time.
    frame_times = np.interp(out_times, out, grid)

    return Timeline(
        fps=float(fps),
        frame_times=frame_times,
        data_start=float(data_start),
        data_end=float(data_end),
        grid_data=grid,
        grid_out=out,
        ramps=ramps,
        holds=holds,
    )
