"""Text based widgets: labels, single readouts, stacked tables and clocks."""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageDraw

from ..theme import Rect, draw_text, parse_color, text_size
from .base import (
    RenderContext, Widget, format_value, peak_value, register,
)

__all__ = ["LabelWidget", "ReadoutWidget", "TableWidget", "TimerWidget"]


def _fmt_clock(seconds: float, style: str = "t+", decimals: int = 2) -> str:
    """Format a timestamp as a mission clock."""
    if not math.isfinite(seconds):
        return "--"
    sign = "-" if seconds < 0 else "+"
    value = abs(seconds)
    if style == "t+":
        return f"T{sign}{value:.{decimals}f}"
    if style == "t+mmss":
        minutes, rest = divmod(value, 60.0)
        return f"T{sign}{int(minutes):02d}:{rest:0{3 + decimals}.{decimals}f}"
    if style == "mmss":
        minutes, rest = divmod(value, 60.0)
        return f"{int(minutes):02d}:{rest:0{3 + decimals}.{decimals}f}"
    if style == "hhmmss":
        hours, rest = divmod(value, 3600.0)
        minutes, secs = divmod(rest, 60.0)
        return f"{int(hours):02d}:{int(minutes):02d}:{secs:0{3 + decimals}.{decimals}f}"
    return f"{sign if seconds < 0 else ''}{value:.{decimals}f}"


@register("label")
class LabelWidget(Widget):
    """Static text - a title, a motor designation, a date."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.text = str(spec.get("text", ""))
        self.size = float(spec.get("size", 34))
        self.align = str(spec.get("align", "left")).lower()
        self.font = ctx.font(str(spec.get("font", self.theme.font_bold)), self.size)
        self.tracking = float(spec.get("tracking", 0.0))
        self.uppercase = bool(spec.get("uppercase", False))

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def _pillow_anchor(self) -> str:
        return {"left": "la", "center": "ma", "centre": "ma", "right": "ra"}.get(
            self.align, "la")

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        text = self.text.upper() if self.uppercase else self.text
        if not text:
            return
        shadow, offset, blur = self.text_shadow()
        color = self.color("color", "text")
        x = self.rect.x
        if self.align in ("center", "centre"):
            x = self.rect.x + self.rect.w / 2 if self.rect.w else self.rect.x
        elif self.align == "right":
            x = self.rect.x1 if self.rect.w else self.rect.x

        if self.tracking:
            # Letter-spaced text has to be laid out glyph by glyph.
            spacing = self.tracking * self.ctx.scale
            widths = [text_size(self.font, ch)[0] for ch in text]
            total = sum(widths) + spacing * max(0, len(text) - 1)
            if self.align in ("center", "centre"):
                cursor = x - total / 2
            elif self.align == "right":
                cursor = x - total
            else:
                cursor = x
            for ch, w in zip(text, widths):
                draw_text(image, (cursor, self.rect.y), ch, self.font, color,
                          anchor="la", shadow=shadow, shadow_offset=offset,
                          shadow_blur=blur)
                cursor += w + spacing
            return

        draw_text(image, (x, self.rect.y), text, self.font, color,
                  anchor=self._pillow_anchor(), shadow=shadow,
                  shadow_offset=offset, shadow_blur=blur)


@register("readout")
class ReadoutWidget(Widget):
    """A big number with a caption and a unit - the workhorse of an overlay."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = spec.get("channel")
        self.expression = spec.get("expression")
        if not self.channel and not self.expression:
            raise ValueError("readout needs a 'channel' or an 'expression'")
        if self.channel:
            self.channel = self.resolve_channel(self.channel, "readout channel")
        source = ctx.telemetry.get(self.channel) if self.channel else None
        self.label = str(spec.get("label", source.label if source else ""))
        self.unit = str(spec.get("unit", source.unit if source else ""))
        self.format = str(spec.get("format", "{:.1f}"))
        self.value_size = float(spec.get("value_size", 62))
        self.label_size = float(spec.get("label_size", self.value_size * 0.30))
        self.unit_size = float(spec.get("unit_size", self.value_size * 0.42))
        self.align = str(spec.get("align", "left")).lower()
        self.interpolate = bool(spec.get("interpolate", True))
        self.peak_hold = bool(spec.get("peak_hold", False))
        self.value_font = ctx.font(
            str(spec.get("value_font", self.theme.font_mono)), self.value_size)
        self.label_font = ctx.font(
            str(spec.get("label_font", self.theme.font)), self.label_size)
        self.unit_font = ctx.font(
            str(spec.get("unit_font", self.theme.font)), self.unit_size)
        self.padding = ctx.px_x(spec.get("padding", 0.0))
        self._peak = -math.inf

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def prepare(self) -> None:
        self._peak = -math.inf

    def _value(self, data_t: float) -> float:
        if self.expression is not None:
            return float(self.ctx.value_at(str(self.expression), data_t,
                                           self.interpolate))
        return float(self.ctx.value_at(str(self.channel), data_t, self.interpolate))

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        value = self._value(data_t)
        if self.peak_hold and self.channel:
            value = peak_value(self.ctx, str(self.channel), data_t,
                               str(self.spec.get("peak_mode", "max")))

        shadow, offset, blur = self.text_shadow()
        text = format_value(value, self.format)
        color = self.color("color", "text")
        label_color = self.color("label_color", "text_dim")
        unit_color = self.color("unit_color", "text_dim")

        self.draw_panel(image)
        inner = self.rect.inset(self.padding) if self.padding else self.rect
        x = inner.x
        if self.align in ("center", "centre"):
            x = inner.cx if inner.w else inner.x
        elif self.align == "right":
            x = inner.x1 if inner.w else inner.x
        y = inner.y

        # Caption sits above the number.
        if self.label:
            anchor = {"left": "la", "center": "ma", "centre": "ma",
                      "right": "ra"}.get(self.align, "la")
            draw_text(image, (x, y), self.label.upper(), self.label_font,
                      label_color, anchor=anchor, shadow=shadow,
                      shadow_offset=offset, shadow_blur=blur)
            y += text_size(self.label_font, "Xg")[1] * 1.55

        value_w, value_h = text_size(self.value_font, text)
        unit_w = text_size(self.unit_font, self.unit)[0] if self.unit else 0
        gap = self.unit_size * 0.35 * self.ctx.scale if self.unit else 0
        total_w = value_w + gap + unit_w

        if self.align in ("center", "centre"):
            value_x = x - total_w / 2
        elif self.align == "right":
            value_x = x - total_w
        else:
            value_x = x

        draw_text(image, (value_x, y), text, self.value_font, color, anchor="la",
                  shadow=shadow, shadow_offset=offset, shadow_blur=blur)
        if self.unit:
            # The unit is baseline-aligned with the number, not top-aligned.
            unit_y = y + value_h - text_size(self.unit_font, "X")[1]
            draw_text(image, (value_x + value_w + gap, unit_y), self.unit,
                      self.unit_font, unit_color, anchor="la", shadow=shadow,
                      shadow_offset=offset, shadow_blur=blur)


@register("table")
class TableWidget(Widget):
    """Several channels stacked as label/value rows."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        rows = spec.get("rows") or spec.get("channels") or []
        if not rows:
            raise ValueError("table needs a non-empty 'rows' list")
        self.rows: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, (str, list, tuple)):
                row = {"channel": row}
            if not isinstance(row, dict):
                raise ValueError(
                    f"table row must be a mapping, a channel name, or a list of "
                    f"alternative names: {row!r}")
            channel = row.get("channel")
            if channel and not row.get("expression"):
                channel = self.resolve_channel(channel, "table row")
            source = ctx.telemetry.get(channel) if channel else None
            self.rows.append({
                "channel": channel,
                "expression": row.get("expression"),
                "label": str(row.get("label", source.label if source else channel or "")),
                "unit": str(row.get("unit", source.unit if source else "")),
                "format": str(row.get("format", "{:.2f}")),
                "color": parse_color(row["color"]) if "color" in row else None,
                "peak_hold": bool(row.get("peak_hold", False)),
                "peak_mode": str(row.get("peak_mode", "max")),
            })
        self.size = float(spec.get("size", 30))
        self.row_gap = float(spec.get("row_gap", self.size * 0.55))
        self.label_width = spec.get("label_width", "auto")
        self.padding = float(spec.get("padding", 18))
        self.label_font = ctx.font(str(spec.get("font", self.theme.font)), self.size)
        self.value_font = ctx.font(
            str(spec.get("value_font", self.theme.font_mono)), self.size)

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.22

    def default_height(self, ctx: RenderContext) -> float:
        # Read the spec, not instance state: the base constructor sizes the
        # widget before this subclass has parsed its rows.
        rows = self.spec.get("rows") or self.spec.get("channels") or []
        size = float(self.spec.get("size", 30))
        gap = float(self.spec.get("row_gap", size * 0.55))
        padding = float(self.spec.get("padding", 18))
        return (len(rows) * (size + gap) + padding * 2) * ctx.scale

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        shadow, offset, blur = self.text_shadow()
        pad = self.padding * self.ctx.scale
        label_color = self.color("label_color", "text_dim")
        value_color = self.color("color", "text")
        line_h = text_size(self.value_font, "Xg")[1]
        step = line_h + self.row_gap * self.ctx.scale
        y = self.rect.y + pad
        for row in self.rows:
            key = row["expression"] or row["channel"]
            if row["peak_hold"] and row["channel"]:
                value = peak_value(self.ctx, str(row["channel"]), data_t,
                                   row["peak_mode"])
            else:
                value = self.ctx.value_at(str(key), data_t)
            text = format_value(value, row["format"])
            if row["unit"]:
                text = f"{text} {row['unit']}"
            draw_text(image, (self.rect.x + pad, y), row["label"], self.label_font,
                      label_color, anchor="la", shadow=shadow,
                      shadow_offset=offset, shadow_blur=blur)
            draw_text(image, (self.rect.x1 - pad, y), text, self.value_font,
                      row["color"] or value_color, anchor="ra", shadow=shadow,
                      shadow_offset=offset, shadow_blur=blur)
            y += step


@register("timer")
class TimerWidget(Widget):
    """A mission clock driven by data time."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.style = str(spec.get("style", "t+")).lower()
        self.decimals = int(spec.get("decimals", 2))
        self.size = float(spec.get("size", 56))
        self.label = str(spec.get("label", ""))
        self.label_size = float(spec.get("label_size", self.size * 0.32))
        self.align = str(spec.get("align", "left")).lower()
        self.font = ctx.font(str(spec.get("font", self.theme.font_mono)), self.size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)),
                                   self.label_size)
        self.padding = float(spec.get("padding", 0))
        # An explicit zero overrides the global one, e.g. to clock from ignition.
        self.zero = spec.get("zero")

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        zero = float(self.zero) if self.zero is not None else self.ctx.t0
        text = _fmt_clock(data_t - zero, self.style, self.decimals)
        shadow, offset, blur = self.text_shadow()
        self.draw_panel(image)
        pad = self.padding * self.ctx.scale
        anchor = {"left": "la", "center": "ma", "centre": "ma",
                  "right": "ra"}.get(self.align, "la")
        x = self.rect.x + pad
        if self.align in ("center", "centre"):
            x = self.rect.cx if self.rect.w else self.rect.x
        elif self.align == "right":
            x = self.rect.x1 - pad
        y = self.rect.y + pad
        if self.label:
            draw_text(image, (x, y), self.label.upper(), self.label_font,
                      self.color("label_color", "text_dim"), anchor=anchor,
                      shadow=shadow, shadow_offset=offset, shadow_blur=blur)
            y += text_size(self.label_font, "Xg")[1] * 1.55
        draw_text(image, (x, y), text, self.font, self.color("color", "text"),
                  anchor=anchor, shadow=shadow, shadow_offset=offset,
                  shadow_blur=blur)
