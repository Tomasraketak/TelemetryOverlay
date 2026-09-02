"""Widget base class and the per-render context handed to every widget."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

from ..data import Telemetry
from ..fonts import get_font
from ..theme import (
    RGBA, Rect, Theme, anchor_offset, draw_text, parse_color, resolve_length,
    rounded_rect, text_size, with_alpha,
)
from ..timeline import Timeline

__all__ = ["RenderContext", "Widget", "register", "build_widget", "WIDGET_TYPES",
           "format_value", "ChannelNotFound", "peak_value"]


class ChannelNotFound(ValueError):
    """A widget asked for a channel the log does not contain."""


@dataclass
class RenderContext:
    """Everything a widget needs that does not change between frames."""

    telemetry: Telemetry
    theme: Theme
    width: int
    height: int
    timeline: Timeline
    events: list[dict[str, Any]] = field(default_factory=list)
    scale: float = 1.0          # design-size to output-size multiplier
    t0: float = 0.0             # data time treated as "T+0"

    def px_x(self, value: Any, default: float = 0.0) -> float:
        return resolve_length(value, self.width, default)

    def px_y(self, value: Any, default: float = 0.0) -> float:
        return resolve_length(value, self.height, default)

    def font(self, family: str, size: float):
        """Fonts are specified at design scale and grow with the output."""
        return get_font(family, max(1, int(round(size * self.scale))))

    def value_at(self, key: str, t: float, interpolate: bool = True) -> float:
        return self.telemetry.value_at(key, t, interpolate)


def peak_value(ctx: "RenderContext", key: str, data_t: float,
               mode: str = "max") -> float:
    """Largest (or smallest) value of a channel up to ``data_t``.

    Computed from the samples rather than accumulated across calls, so a frame
    rendered out of order - as happens in the worker pool - still shows the
    right number.
    """
    channel = ctx.telemetry.get(key)
    if channel is None:
        return float("nan")
    idx = ctx.telemetry.index_at(data_t)
    window = channel.values[: idx + 1]
    window = window[np.isfinite(window)]
    if window.size == 0:
        return float("nan")
    return float(window.min() if mode == "min" else window.max())


def format_value(value: float, fmt: str = "{:.1f}", nan_text: str = "--") -> str:
    """Format a number, keeping non-finite values readable."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return nan_text
    try:
        return fmt.format(value)
    except (ValueError, KeyError, IndexError):
        return str(value)


class Widget:
    """One element of the overlay.

    Subclasses implement :meth:`render`. Anything expensive that does not
    depend on the current frame belongs in :meth:`prepare`, which runs once
    before the first frame - that is what keeps a 4K render tractable.
    """

    type_name = "widget"

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        self.spec = spec
        self.ctx = ctx
        self.name = str(spec.get("name", self.type_name))
        self.visible = bool(spec.get("visible", True))
        self.opacity = float(spec.get("opacity", 1.0))
        # Optional time window in which the widget is shown at all.
        self.show_from = spec.get("show_from")
        self.show_to = spec.get("show_to")
        self.fade = float(spec.get("fade", 0.0))
        self.theme = ctx.theme
        self.rect = self._resolve_rect(spec, ctx)

    # -- geometry ---------------------------------------------------------
    def _resolve_rect(self, spec: dict[str, Any], ctx: RenderContext) -> Rect:
        w = ctx.px_x(spec.get("width"), self.default_width(ctx))
        h = ctx.px_y(spec.get("height"), self.default_height(ctx))
        x = ctx.px_x(spec.get("x", 0))
        y = ctx.px_y(spec.get("y", 0))
        dx, dy = anchor_offset(str(spec.get("anchor", "top-left")), w, h)
        return Rect(x + dx, y + dy, w, h)

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.2

    def default_height(self, ctx: RenderContext) -> float:
        return ctx.height * 0.1

    # -- helpers ----------------------------------------------------------
    def resolve_channel(self, value: Any, field: str = "channel") -> str:
        """Turn a name, or a list of candidate names, into a real channel key."""
        key = self.ctx.telemetry.resolve(value)
        if key is None:
            wanted = value if isinstance(value, str) else ", ".join(map(str, value))
            raise ChannelNotFound(
                f"unknown channel {wanted!r} for '{field}'; available: "
                f"{', '.join(sorted(self.ctx.telemetry.channels))}"
            )
        return key

    def color(self, key: str, default: Any = None) -> RGBA:
        """A colour from the spec, falling back to a theme entry."""
        if key in self.spec:
            return parse_color(self.spec[key])
        if isinstance(default, str) and hasattr(self.theme, default):
            return parse_color(getattr(self.theme, default))
        return parse_color(default) if default is not None else parse_color("#ffffff")

    def opacity_at(self, data_t: float) -> float:
        """Widget opacity for a frame, including show window and fades."""
        alpha = self.opacity
        if self.show_from is not None:
            start = float(self.show_from)
            if data_t < start:
                return 0.0
            if self.fade > 0:
                alpha *= min(1.0, (data_t - start) / self.fade)
        if self.show_to is not None:
            end = float(self.show_to)
            if data_t > end:
                return 0.0
            if self.fade > 0:
                alpha *= min(1.0, (end - data_t) / self.fade)
        return max(0.0, min(1.0, alpha))

    def draw_panel(self, image: Image.Image, rect: Rect | None = None) -> None:
        """Draw the widget's background plate, if it has one."""
        rect = rect or self.rect
        panel = self.spec.get("panel", True)
        if panel is False:
            return
        fill = parse_color(self.spec.get("panel_color", self.theme.panel))
        border = parse_color(self.spec.get("panel_border", self.theme.panel_border))
        if fill[3] == 0 and border[3] == 0:
            return
        radius = self.ctx.px_x(
            self.spec.get("corner_radius"), self.theme.corner_radius * self.ctx.scale
        )
        width = max(1, int(round(
            float(self.spec.get("border_width", 1)) * self.ctx.scale)))
        rounded_rect(ImageDraw.Draw(image), rect.as_tuple(), radius,
                     fill=fill, outline=border if border[3] else None, width=width)

    def text_shadow(self) -> tuple[RGBA | None, tuple[int, int], int]:
        """Shadow settings for text, honouring a per-widget override."""
        if self.spec.get("text_shadow", True) is False:
            return None, (0, 0), 0
        color = parse_color(self.spec.get("shadow_color", self.theme.shadow))
        scale = self.ctx.scale
        off = self.spec.get("shadow_offset", self.theme.shadow_offset)
        offset = (int(round(float(off[0]) * scale)), int(round(float(off[1]) * scale)))
        blur = int(round(float(self.spec.get("shadow_blur",
                                             self.theme.shadow_blur)) * scale))
        return color, offset, blur

    # -- lifecycle --------------------------------------------------------
    def prepare(self) -> None:
        """Build frame-independent caches. Called once before rendering."""

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        """Draw the widget onto ``image`` for a given data timestamp."""
        raise NotImplementedError

    def draw_on(self, image: Image.Image, data_t: float, frame: int) -> None:
        """Render with opacity handling; called by the frame renderer."""
        if not self.visible:
            return
        alpha = self.opacity_at(data_t)
        if alpha <= 0.0:
            return
        if alpha >= 0.999:
            self.render(image, data_t, frame)
            return
        # Partial opacity needs its own layer so overlapping strokes inside
        # the widget do not accumulate alpha against each other.
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        self.render(layer, data_t, frame)
        if alpha < 1.0:
            bands = layer.split()
            layer.putalpha(bands[3].point(lambda a: int(a * alpha)))
        image.alpha_composite(layer)


WIDGET_TYPES: dict[str, type[Widget]] = {}


def register(name: str) -> Callable[[type[Widget]], type[Widget]]:
    """Class decorator adding a widget to the config vocabulary."""
    def decorator(cls: type[Widget]) -> type[Widget]:
        cls.type_name = name
        WIDGET_TYPES[name] = cls
        return cls
    return decorator


def build_widget(spec: dict[str, Any], ctx: RenderContext) -> Widget | None:
    """Instantiate a widget from its config block."""
    if not isinstance(spec, dict):
        raise ValueError(f"each widget must be a mapping, got {type(spec).__name__}")
    kind = str(spec.get("type", "")).strip().lower()
    if not kind:
        raise ValueError(f"widget is missing a 'type': {spec!r}")
    cls = WIDGET_TYPES.get(kind)
    if cls is None:
        raise ValueError(
            f"unknown widget type {kind!r}; available: "
            f"{', '.join(sorted(WIDGET_TYPES))}"
        )
    try:
        return cls(spec, ctx)
    except ChannelNotFound:
        # A widget marked optional is dropped when its channel is absent, so
        # one preset can cover logs that do not all record the same things.
        if spec.get("optional"):
            return None
        raise
