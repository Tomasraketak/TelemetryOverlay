"""Widget base class and the per-render context handed to every widget."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from ..data import Channel, Telemetry
from ..fonts import get_font
from ..theme import (
    RGBA, Rect, Theme, anchor_offset, composite_clipped, make_panel_image,
    parse_color, resolve_length, scale_alpha,
)
from ..timeline import Timeline

__all__ = [
    "RenderContext", "Widget", "register", "build_widget", "WIDGET_TYPES",
    "format_value", "ChannelNotFound", "peak_value", "zone_color", "parse_zones",
]


class ChannelNotFound(ValueError):
    """A widget asked for a channel the log does not contain."""


def parse_zones(spec: Any) -> list[dict[str, Any]]:
    if not spec:
        return []
    if not isinstance(spec, (list, tuple)):
        raise ValueError("'zones' must be a list of {from, to, color} mappings")
    out: list[dict[str, Any]] = []
    for zone in spec:
        if not isinstance(zone, dict):
            raise ValueError(f"zone must be a mapping, got {zone!r}")
        out.append({
            "from": float(zone.get("from", -math.inf)),
            "to": float(zone.get("to", math.inf)),
            "color": parse_color(zone.get("color", "#ffffff")),
        })
    return out


def zone_color(value: float, zones: list[dict[str, Any]], default: RGBA) -> RGBA:
    """Colour for a value from a pre-parsed list of ``{from, to, color}`` bands."""
    if not zones or not math.isfinite(value):
        return default
    for zone in zones:
        if zone["from"] <= value <= zone["to"]:
            col = zone["color"]
            return col if isinstance(col, tuple) else parse_color(col, default)
    return default


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
    source_dir: Path | None = None

    def px_x(self, value: Any, default: float = 0.0) -> float:
        return resolve_length(value, self.width, default)

    def px_y(self, value: Any, default: float = 0.0) -> float:
        return resolve_length(value, self.height, default)

    def font(self, family: str, size: float):
        """Fonts are specified at design scale and grow with the output."""
        return get_font(family, max(1, int(round(size * self.scale))))

    def value_at(self, key: str, t: float, interpolate: bool = True) -> float:
        return self.telemetry.value_at(key, t, interpolate)

    def text_at(self, key: str, t: float) -> str:
        return self.telemetry.text_at(key, t)

    def ensure_expression(self, expression: str, unit: str = "", label: str = "") -> str:
        """Evaluate an inline widget ``expression:`` once and register a cached channel."""
        expr = str(expression).strip()
        if expr in self.telemetry.channels:
            return expr
        digest = hashlib.sha1(expr.encode("utf-8")).hexdigest()[:12]
        key = f"__expr_{digest}"
        if key not in self.telemetry.channels:
            from ..expressions import evaluate
            values = evaluate(expr, self.telemetry)
            self.telemetry.add(
                Channel(key=key, label=label or expr, unit=unit, values=values, kind="numeric")
            )
        return key


def peak_value(
    ctx: RenderContext, key: str, data_t: float, mode: str = "max"
) -> float:
    """O(1) largest (or smallest) value of a channel up to ``data_t``."""
    channel = ctx.telemetry.get(key)
    if channel is None:
        return float("nan")
    idx = ctx.telemetry.index_at(data_t)
    if mode == "min":
        return channel.running_min_at(idx)
    return channel.running_max_at(idx)


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
    is_static = False  # subclasses that never vary with time can set True

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        self.spec = spec
        self.ctx = ctx
        self.name = str(spec.get("name", self.type_name))
        self.visible = bool(spec.get("visible", True))
        self.opacity = float(spec.get("opacity", 1.0))
        self.show_from = spec.get("show_from")
        self.show_to = spec.get("show_to")
        self.fade = float(spec.get("fade", 0.0))
        self.theme = ctx.theme
        self.rect = self._resolve_rect(spec, ctx)
        self._panel_cache: dict[tuple[int, int], Image.Image | None] = {}
        self._shadow_cache: tuple[RGBA | None, tuple[int, int], int] | None = None

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

    def resolve_optional_channel(self, value: Any) -> str | None:
        if value is None:
            return None
        return self.ctx.telemetry.resolve(value)

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

    def build_panel_layer(self, w: float, h: float) -> Image.Image | None:
        """Build (or retrieve cached) pre-rendered RGBA panel layer."""
        if self.spec.get("panel", True) is False:
            return None
        iw, ih = int(round(w)), int(round(h))
        if iw <= 0 or ih <= 0:
            return None
        cache_key = (iw, ih)
        if cache_key not in self._panel_cache:
            fill = parse_color(self.spec.get("panel_color", self.theme.panel))
            border = parse_color(self.spec.get("panel_border", self.theme.panel_border))
            radius = self.ctx.px_x(
                self.spec.get("corner_radius"),
                self.theme.corner_radius * self.ctx.scale,
            )
            bw = max(1, int(round(float(self.spec.get("border_width", 1)) * self.ctx.scale)))
            self._panel_cache[cache_key] = make_panel_image(iw, ih, radius, fill, border, bw)
        return self._panel_cache[cache_key]

    def draw_panel(self, image: Image.Image, rect: Rect | None = None) -> None:
        """Composite the widget's background plate using proper alpha blending."""
        rect = rect or self.rect
        panel_img = self.build_panel_layer(rect.w, rect.h)
        if panel_img is not None:
            composite_clipped(image, panel_img, int(round(rect.x)), int(round(rect.y)))

    def text_shadow(self) -> tuple[RGBA | None, tuple[int, int], int]:
        """Shadow settings for text, cached per widget."""
        if self._shadow_cache is None:
            if self.spec.get("text_shadow", True) is False:
                self._shadow_cache = (None, (0, 0), 0)
            else:
                color = parse_color(self.spec.get("shadow_color", self.theme.shadow))
                scale = self.ctx.scale
                off = self.spec.get("shadow_offset", self.theme.shadow_offset)
                offset = (int(round(float(off[0]) * scale)), int(round(float(off[1]) * scale)))
                blur = int(round(float(self.spec.get("shadow_blur",
                                                     self.theme.shadow_blur)) * scale))
                self._shadow_cache = (color, offset, blur)
        return self._shadow_cache

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
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        self.render(layer, data_t, frame)
        image.alpha_composite(scale_alpha(layer, alpha))


WIDGET_TYPES: dict[str, type[Widget]] = {}
WIDGET_ALIASES: dict[str, str] = {}


def register(name: str, *aliases: str) -> Callable[[type[Widget]], type[Widget]]:
    """Class decorator adding a widget (and optional aliases) to the config vocabulary."""
    def decorator(cls: type[Widget]) -> type[Widget]:
        cls.type_name = name
        WIDGET_TYPES[name] = cls
        for alias in aliases:
            WIDGET_ALIASES[alias] = name
        return cls
    return decorator


def build_widget(spec: dict[str, Any], ctx: RenderContext) -> Widget | None:
    """Instantiate a widget from its config block."""
    if not isinstance(spec, dict):
        raise ValueError(f"each widget must be a mapping, got {type(spec).__name__}")
    kind = str(spec.get("type", "")).strip().lower()
    if not kind:
        raise ValueError(f"widget is missing a 'type': {spec!r}")
    canonical = WIDGET_ALIASES.get(kind, kind)
    cls = WIDGET_TYPES.get(canonical)
    if cls is None:
        raise ValueError(
            f"unknown widget type {kind!r}; available: "
            f"{', '.join(sorted(WIDGET_TYPES))}"
        )
    try:
        return cls(spec, ctx)
    except ChannelNotFound:
        if spec.get("optional"):
            return None
        raise
