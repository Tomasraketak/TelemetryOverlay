"""Turning a config plus a log into RGBA frames."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image

from .config import Config
from .data import Telemetry, load as load_data
from .events import build_events
from .expressions import add_derived
from .theme import parse_color
from .timeline import Timeline, build_timeline
from .widgets import RenderContext, Widget, build_widget

__all__ = ["OverlayRenderer", "build_renderer"]


@dataclass
class _Window:
    start: float
    end: float


def _resolve_window(config: Config, telemetry: Telemetry) -> _Window:
    start = config.time_start if config.time_start is not None else telemetry.t0
    end = config.time_end if config.time_end is not None else telemetry.t1
    if end <= start:
        raise ValueError(
            f"empty time window: start {start:g} s is not before end {end:g} s. "
            f"The log covers {telemetry.t0:g} s .. {telemetry.t1:g} s."
        )
    return _Window(float(start), float(end))


class OverlayRenderer:
    """Renders overlay frames for one config/telemetry pair.

    Constructing the renderer does all the frame-independent work: derived
    channels, event detection, widget construction and each widget's cache.
    After that :meth:`render_frame` is cheap enough to run in a worker pool.
    """

    def __init__(self, config: Config, telemetry: Telemetry):
        self.config = config
        self.telemetry = telemetry

        add_derived(telemetry, config.derived)
        window = _resolve_window(config, telemetry)
        self.timeline: Timeline = build_timeline(
            data_start=window.start,
            data_end=window.end,
            fps=config.fps,
            speed=config.speed,
            ramps=config.ramps,
            holds=config.holds,
            max_duration=config.max_duration,
        )
        self.events = build_events(telemetry, config.events)
        self.background = (parse_color(config.background)
                           if config.background else (0, 0, 0, 0))

        self.context = RenderContext(
            telemetry=telemetry,
            theme=config.theme,
            width=config.width,
            height=config.height,
            timeline=self.timeline,
            events=self.events,
            scale=config.scale,
            t0=config.t0,
        )

        self.widgets: list[Widget] = []
        for index, spec in enumerate(config.widgets):
            try:
                widget = build_widget(spec, self.context)
            except Exception as exc:
                name = spec.get("name", spec.get("type", f"#{index}")) \
                    if isinstance(spec, dict) else f"#{index}"
                raise ValueError(f"widget '{name}': {exc}") from exc
            if widget is not None:
                self.widgets.append(widget)

        for widget in self.widgets:
            try:
                widget.prepare()
            except Exception as exc:
                raise ValueError(f"widget '{widget.name}': {exc}") from exc

    # -- properties -------------------------------------------------------
    @property
    def frame_count(self) -> int:
        return self.timeline.frame_count

    @property
    def size(self) -> tuple[int, int]:
        return self.config.width, self.config.height

    @property
    def duration(self) -> float:
        return self.timeline.duration

    # -- rendering --------------------------------------------------------
    def blank(self) -> Image.Image:
        """A fresh frame - transparent unless a background colour was set."""
        return Image.new("RGBA", self.size, self.background)

    def render_frame(self, frame: int) -> Image.Image:
        """Render one output frame by its index."""
        if not 0 <= frame < self.frame_count:
            raise IndexError(
                f"frame {frame} out of range (0..{self.frame_count - 1})")
        return self.render_at(self.timeline.data_time(frame), frame)

    def render_at(self, data_t: float, frame: int = 0) -> Image.Image:
        """Render the overlay as it looks at a given data timestamp."""
        image = self.blank()
        for widget in self.widgets:
            try:
                widget.draw_on(image, data_t, frame)
            except Exception as exc:
                raise RuntimeError(
                    f"widget '{widget.name}' failed at t={data_t:.4f} s: {exc}"
                ) from exc
        return image

    def frames(self, start: int = 0, stop: int | None = None
               ) -> Iterator[tuple[int, Image.Image]]:
        """Iterate over rendered frames."""
        stop = self.frame_count if stop is None else min(stop, self.frame_count)
        for index in range(start, stop):
            yield index, self.render_frame(index)

    def describe(self) -> str:
        lines = [
            f"resolution    : {self.config.width}x{self.config.height}"
            f"  (layout scale {self.config.scale:.3f}x)",
            f"background    : {'transparent' if self.background[3] == 0 else self.background}",
            self.timeline.describe(),
            f"widgets       : {len(self.widgets)}"
            + (f" ({', '.join(w.name for w in self.widgets)})" if self.widgets else ""),
        ]
        if self.events:
            marks = ", ".join(f"{e['label']}@{e['time']:.3f}s" for e in self.events)
            lines.append(f"events        : {marks}")
        return "\n".join(lines)


def build_renderer(config: Config, data_path: str | Path | None = None,
                   telemetry: Telemetry | None = None) -> OverlayRenderer:
    """Load the data if needed and construct a renderer."""
    if telemetry is None:
        if data_path is None:
            raise ValueError("either a data path or a Telemetry object is required")
        telemetry = load_data(data_path, config.data)
    if telemetry.time.size == 0:
        raise ValueError("the log contains no samples")
    return OverlayRenderer(config, telemetry)
