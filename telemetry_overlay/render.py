"""Turning a config plus a log into RGBA frames."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

from .config import Config
from .data import Telemetry, load as load_data, load_many
from .events import build_events
from .expressions import add_derived
from .formats.enrichment import apply_filters
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

    Constructing the renderer does all the frame-independent work: filters,
    derived channels, event detection, widget construction and each widget's
    pre-rendered static caches. After that :meth:`render_frame` is cheap enough
    to run in a worker pool.
    """

    def __init__(self, config: Config, telemetry: Telemetry, *, strict: bool = True):
        """Build the renderer.

        With ``strict=False`` (used by the interactive Studio) a broken widget,
        derived channel or event rule never takes the whole overlay down: it is
        skipped and the reason is recorded in :attr:`widget_errors` (keyed by the
        widget's index in ``config.widgets``) or :attr:`warnings`.
        """
        self.config = config
        self.telemetry = telemetry
        self.strict = strict
        self.widget_errors: dict[int, str] = {}
        self.warnings: list[str] = []
        self.widget_index: dict[int, int] = {}  # id(widget) -> spec index

        if config.filters:
            if strict:
                apply_filters(telemetry, config.filters)
            else:
                try:
                    apply_filters(telemetry, config.filters)
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"filters: {exc}")
        if strict:
            add_derived(telemetry, config.derived)
        else:
            for item in config.derived or []:
                try:
                    add_derived(telemetry, [item])
                except Exception as exc:  # noqa: BLE001
                    key = item.get("key", "?") if isinstance(item, dict) else "?"
                    self.warnings.append(f"derived '{key}': {exc}")
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
        if strict:
            self.events = build_events(telemetry, config.events)
        else:
            self.events = []
            rules = config.events if isinstance(config.events, (list, tuple)) else [config.events]
            for rule in rules:
                try:
                    self.events.extend(build_events(telemetry, [rule] if isinstance(rule, (dict, str)) else rule))
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"event: {exc}")
            self.events.sort(key=lambda e: float(e.get("time", 0.0)))
        self.background = (
            parse_color(config.background) if config.background else (0, 0, 0, 0)
        )

        self.context = RenderContext(
            telemetry=telemetry,
            theme=config.theme,
            width=config.width,
            height=config.height,
            timeline=self.timeline,
            events=self.events,
            scale=config.scale,
            t0=config.t0,
            source_dir=config.source_path.parent if config.source_path else None,
        )

        self.widgets: list[Widget] = []
        for index, spec in enumerate(config.widgets):
            name = (
                spec.get("name", spec.get("type", f"#{index}"))
                if isinstance(spec, dict)
                else f"#{index}"
            )
            try:
                widget = build_widget(spec, self.context)
                if widget is not None:
                    widget.prepare()
            except Exception as exc:
                if strict:
                    raise ValueError(f"widget '{name}': {exc}") from exc
                self.widget_errors[index] = str(exc)
                continue
            if widget is not None:
                self.widgets.append(widget)
                self.widget_index[id(widget)] = index

        # Pre-allocate the base canvas once; .copy() is a fast C memcpy.
        self._blank_template = Image.new("RGBA", self.size, self.background)

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
        return self._blank_template.copy()

    def render_frame(self, frame: int) -> Image.Image:
        """Render one output frame by its index."""
        if not 0 <= frame < self.frame_count:
            raise IndexError(
                f"frame {frame} out of range (0..{self.frame_count - 1})"
            )
        return self.render_at(self.timeline.data_time(frame), frame)

    def render_at(
        self, data_t: float, frame: int = 0, *, skip: set[int] | None = None
    ) -> Image.Image:
        """Render the overlay as it looks at a given data timestamp.

        ``skip`` holds spec indices (positions in ``config.widgets``) of widgets
        to leave out - the Studio uses it to hide a widget while it is dragged.
        """
        image = self.blank()
        for widget in self.widgets:
            if skip and self.widget_index.get(id(widget)) in skip:
                continue
            try:
                widget.draw_on(image, data_t, frame)
            except Exception as exc:
                if not self.strict:
                    idx = self.widget_index.get(id(widget), -1)
                    self.widget_errors.setdefault(idx, f"t={data_t:.3f} s: {exc}")
                    continue
                raise RuntimeError(
                    f"widget '{widget.name}' failed at t={data_t:.4f} s: {exc}"
                ) from exc
        return image

    def frames(
        self, start: int = 0, stop: int | None = None
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
            marks = ", ".join(f"{e['label']}@{e['time']:.3f}s" for e in self.events[:10])
            if len(self.events) > 10:
                marks += f" (+{len(self.events) - 10} more)"
            lines.append(f"events        : {marks}")
        return "\n".join(lines)


def build_renderer(
    config: Config,
    data_path: str | Path | list[str | Path] | None = None,
    telemetry: Telemetry | None = None,
) -> OverlayRenderer:
    """Load the data if needed and construct a renderer."""
    if telemetry is None:
        if data_path is None:
            raise ValueError("either a data path or a Telemetry object is required")
        if isinstance(data_path, (list, tuple)):
            telemetry = load_many(data_path, config.data)
        else:
            telemetry = load_data(data_path, config.data)
    if telemetry.time.size == 0:
        raise ValueError("the log contains no samples")
    return OverlayRenderer(config, telemetry)
