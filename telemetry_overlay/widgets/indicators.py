"""Bars, dial gauges, status lamps and image stamps."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..theme import (
    RGBA, Rect, composite_clipped, draw_text, mix, parse_color, rounded_rect,
    text_size, with_alpha,
)
from .base import RenderContext, Widget, format_value, register

__all__ = ["BarWidget", "GaugeWidget", "LedWidget", "ImageWidget"]

# Gauges and bars are drawn on a supersampled scratch layer; arcs and circles
# would otherwise show hard stair-stepping at any reasonable output size.
_SS = 3


def _zone_color(value: float, zones: list[dict[str, Any]], default: RGBA) -> RGBA:
    """Colour for a value from a list of ``{from, to, color}`` bands."""
    for zone in zones:
        lo = float(zone.get("from", -math.inf))
        hi = float(zone.get("to", math.inf))
        if lo <= value <= hi:
            return parse_color(zone.get("color", default))
    return default


def _parse_zones(spec: Any) -> list[dict[str, Any]]:
    if not spec:
        return []
    if not isinstance(spec, (list, tuple)):
        raise ValueError("'zones' must be a list of {from, to, color} mappings")
    out = []
    for zone in spec:
        if not isinstance(zone, dict):
            raise ValueError(f"zone must be a mapping, got {zone!r}")
        out.append(zone)
    return out


class _ValueWidget(Widget):
    """Shared plumbing for widgets that display one channel."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = spec.get("channel") or spec.get("expression")
        if not self.channel:
            raise ValueError(f"{self.type_name} needs a 'channel'")
        self.channel = self.resolve_channel(self.channel, self.type_name)
        source = ctx.telemetry[self.channel]
        lo, hi = source.range()
        self.v_min = float(spec.get("min", 0.0 if lo >= 0 else lo))
        self.v_max = float(spec.get("max", hi))
        if self.v_max <= self.v_min:
            self.v_max = self.v_min + 1.0
        self.label = str(spec.get("label", source.label))
        self.unit = str(spec.get("unit", source.unit))
        self.format = str(spec.get("format", "{:.1f}"))
        self.zones = _parse_zones(spec.get("zones"))

    def value(self, data_t: float) -> float:
        return float(self.ctx.value_at(str(self.channel), data_t))

    def fraction(self, value: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return float(np.clip((value - self.v_min) / (self.v_max - self.v_min), 0.0, 1.0))


@register("bar")
class BarWidget(_ValueWidget):
    """A level bar, horizontal or vertical."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.orientation = str(spec.get("orientation", "horizontal")).lower()
        if self.orientation not in ("horizontal", "vertical"):
            raise ValueError("bar orientation must be 'horizontal' or 'vertical'")
        self.thickness = float(spec.get("thickness", 22))
        self.show_value = bool(spec.get("show_value", True))
        self.show_label = bool(spec.get("show_label", True))
        self.value_size = float(spec.get("value_size", 30))
        self.label_size = float(spec.get("label_size", 22))
        self.radius = float(spec.get("radius", self.thickness / 2))
        self.track_opacity = float(spec.get("track_opacity", 0.22))
        self.peak_marker = bool(spec.get("peak_marker", False))
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                   self.value_size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)),
                                   self.label_size)
        self.ticks = int(spec.get("ticks", 0))

    # The base constructor resolves the rectangle before this subclass has
    # run, so the defaults read the spec rather than instance attributes.
    def _is_horizontal(self) -> bool:
        return str(self.spec.get("orientation", "horizontal")).lower() != "vertical"

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.2 if self._is_horizontal() else 90 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 70 * ctx.scale if self._is_horizontal() else ctx.height * 0.25

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        scale = self.ctx.scale
        value = self.value(data_t)
        frac = self.fraction(value)
        fill_color = _zone_color(value, self.zones, self.color("color", "accent"))
        track_color = with_alpha(self.color("track_color", "text"),
                                 self.track_opacity)
        draw = ImageDraw.Draw(image)
        shadow, offset, blur = self.text_shadow()
        thickness = self.thickness * scale
        radius = self.radius * scale

        rect = self.rect
        top = rect.y
        if self.show_label and self.label:
            draw_text(image, (rect.x, top), self.label.upper(), self.label_font,
                      self.color("label_color", "text_dim"), anchor="la",
                      shadow=shadow, shadow_offset=offset, shadow_blur=blur)
            top += text_size(self.label_font, "Xg")[1] * 1.5

        if self.orientation == "horizontal":
            track = Rect(rect.x, top, rect.w, thickness)
            rounded_rect(draw, track.as_tuple(), radius, fill=track_color)
            if frac > 0:
                filled = Rect(track.x, track.y, max(thickness * 0.4, track.w * frac),
                              thickness)
                rounded_rect(draw, filled.as_tuple(), radius, fill=fill_color)
            if self.peak_marker:
                self._draw_peak(draw, track, data_t, horizontal=True)
            if self.ticks > 1:
                self._draw_ticks(draw, track, horizontal=True)
            if self.show_value:
                text = format_value(value, self.format)
                if self.unit:
                    text = f"{text} {self.unit}"
                draw_text(image, (rect.x1, track.y1 + 6 * scale), text,
                          self.value_font, self.color("value_color", "text"),
                          anchor="ra", shadow=shadow, shadow_offset=offset,
                          shadow_blur=blur)
        else:
            bottom = rect.y1
            if self.show_value:
                bottom -= text_size(self.value_font, "Xg")[1] * 1.6
            track = Rect(rect.x + (rect.w - thickness) / 2, top, thickness,
                         max(1.0, bottom - top))
            rounded_rect(draw, track.as_tuple(), radius, fill=track_color)
            if frac > 0:
                height = max(thickness * 0.4, track.h * frac)
                filled = Rect(track.x, track.y1 - height, thickness, height)
                rounded_rect(draw, filled.as_tuple(), radius, fill=fill_color)
            if self.peak_marker:
                self._draw_peak(draw, track, data_t, horizontal=False)
            if self.show_value:
                text = format_value(value, self.format)
                if self.unit:
                    text = f"{text} {self.unit}"
                draw_text(image, (rect.cx, rect.y1), text, self.value_font,
                          self.color("value_color", "text"), anchor="md",
                          shadow=shadow, shadow_offset=offset, shadow_blur=blur)

    def _draw_peak(self, draw: ImageDraw.ImageDraw, track: Rect, data_t: float,
                   horizontal: bool) -> None:
        channel = self.ctx.telemetry.get(str(self.channel))
        if channel is None:
            return
        idx = self.ctx.telemetry.index_at(data_t)
        window = channel.values[: idx + 1]
        window = window[np.isfinite(window)]
        if window.size == 0:
            return
        frac = self.fraction(float(window.max()))
        color = self.color("peak_color", "text")
        w = max(1, int(round(2 * self.ctx.scale)))
        if horizontal:
            x = track.x + track.w * frac
            draw.line([(x, track.y), (x, track.y1)], fill=color, width=w)
        else:
            y = track.y1 - track.h * frac
            draw.line([(track.x, y), (track.x1, y)], fill=color, width=w)

    def _draw_ticks(self, draw: ImageDraw.ImageDraw, track: Rect,
                    horizontal: bool) -> None:
        color = with_alpha(self.color("tick_color", "text"), 0.35)
        w = max(1, int(round(self.ctx.scale)))
        for i in range(1, self.ticks):
            frac = i / self.ticks
            if horizontal:
                x = track.x + track.w * frac
                draw.line([(x, track.y + track.h * 0.25),
                           (x, track.y1 - track.h * 0.25)], fill=color, width=w)
            else:
                y = track.y1 - track.h * frac
                draw.line([(track.x + track.w * 0.25, y),
                           (track.x1 - track.w * 0.25, y)], fill=color, width=w)


@register("gauge")
class GaugeWidget(_ValueWidget):
    """A circular dial with an arc and a needle."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.start_angle = float(spec.get("start_angle", 135.0))
        self.sweep = float(spec.get("sweep", 270.0))
        self.thickness = float(spec.get("thickness", 16))
        self.needle = bool(spec.get("needle", True))
        self.show_value = bool(spec.get("show_value", True))
        self.show_label = bool(spec.get("show_label", True))
        self.show_scale = bool(spec.get("show_scale", False))
        self.scale_steps = int(spec.get("scale_steps", 5))
        self.value_size = float(spec.get("value_size", 40))
        self.label_size = float(spec.get("label_size", 20))
        self.unit_size = float(spec.get("unit_size", 20))
        self.track_opacity = float(spec.get("track_opacity", 0.20))
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                   self.value_size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)),
                                   self.label_size)
        self.unit_font = ctx.font(str(spec.get("unit_font", self.theme.font)),
                                  self.unit_size)
        self._track: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return 240.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 240.0 * ctx.scale

    def _dial_box(self) -> Rect:
        size = min(self.rect.w, self.rect.h)
        return Rect(self.rect.cx - size / 2, self.rect.y, size, size)

    def prepare(self) -> None:
        # The empty track never changes, so it is rasterised once.
        box = self._dial_box()
        w, h = box.size()
        thickness = self.thickness * self.ctx.scale
        layer = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        inset = thickness * _SS / 2 + _SS
        arc_box = (inset, inset, w * _SS - inset, h * _SS - inset)
        draw.arc(arc_box, self.start_angle, self.start_angle + self.sweep,
                 fill=with_alpha(self.color("track_color", "text"),
                                 self.track_opacity),
                 width=int(round(thickness * _SS)))
        if self.zones:
            for zone in self.zones:
                lo = self.fraction(float(zone.get("from", self.v_min)))
                hi = self.fraction(float(zone.get("to", self.v_max)))
                if hi <= lo:
                    continue
                draw.arc(arc_box,
                         self.start_angle + self.sweep * lo,
                         self.start_angle + self.sweep * hi,
                         fill=with_alpha(parse_color(zone.get("color", "#ffffff")), 0.30),
                         width=int(round(thickness * _SS)))
        self._track = layer.reduce(_SS)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        scale = self.ctx.scale
        box = self._dial_box()
        w, h = box.size()
        value = self.value(data_t)
        frac = self.fraction(value)
        color = _zone_color(value, self.zones, self.color("color", "accent"))
        thickness = self.thickness * scale

        if self._track is not None:
            image.alpha_composite(self._track,
                                  (int(round(box.x)), int(round(box.y))))

        # Active arc plus needle, supersampled together for clean edges.
        layer = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        inset = thickness * _SS / 2 + _SS
        arc_box = (inset, inset, w * _SS - inset, h * _SS - inset)
        if frac > 0.001:
            draw.arc(arc_box, self.start_angle,
                     self.start_angle + self.sweep * frac,
                     fill=color, width=int(round(thickness * _SS)))
        if self.needle:
            angle = math.radians(self.start_angle + self.sweep * frac)
            cx, cy = w * _SS / 2, h * _SS / 2
            radius = min(cx, cy) - thickness * _SS * 1.35
            tip = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            tail = (cx - radius * 0.16 * math.cos(angle),
                    cy - radius * 0.16 * math.sin(angle))
            draw.line([tail, tip], fill=color,
                      width=int(round(3.0 * scale * _SS)))
            hub = 4.5 * scale * _SS
            draw.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=color)
        image.alpha_composite(layer.reduce(_SS),
                              (int(round(box.x)), int(round(box.y))))

        shadow, offset, blur = self.text_shadow()
        if self.show_scale:
            self._draw_scale(image, box, thickness)
        if self.show_value:
            text = format_value(value, self.format)
            # Offset below the hub the needle pivots on, so the two never
            # overprint each other.
            value_y = box.cy + self.value_size * scale * 0.30
            draw_text(image, (box.cx, value_y), text, self.value_font,
                      self.color("value_color", "text"), anchor="mm",
                      shadow=shadow, shadow_offset=offset, shadow_blur=blur)
            if self.unit:
                draw_text(image, (box.cx, value_y + self.value_size * scale * 0.62),
                          self.unit, self.unit_font,
                          self.color("unit_color", "text_dim"), anchor="ma",
                          shadow=shadow, shadow_offset=offset, shadow_blur=blur)
        if self.show_label and self.label:
            draw_text(image, (box.cx, box.y1 - self.label_size * scale * 0.4),
                      self.label.upper(), self.label_font,
                      self.color("label_color", "text_dim"), anchor="mm",
                      shadow=shadow, shadow_offset=offset, shadow_blur=blur)

    def _draw_scale(self, image: Image.Image, box: Rect, thickness: float) -> None:
        scale = self.ctx.scale
        font = self.ctx.font(self.theme.font, self.label_size * 0.8)
        color = self.color("scale_color", "text_dim")
        cx, cy = box.cx, box.cy
        # Keep the numbers inside the ring: half the widest label plus a
        # margin, measured from the inner edge of the arc.
        widest = max(text_size(font, f"{self.v_min + (self.v_max - self.v_min) * i / max(1, self.scale_steps):g}")[0]
                     for i in range(self.scale_steps + 1))
        radius = min(box.w, box.h) / 2 - thickness - widest / 2 - 6 * scale
        radius = max(radius, min(box.w, box.h) * 0.18)
        for i in range(self.scale_steps + 1):
            frac = i / max(1, self.scale_steps)
            angle = math.radians(self.start_angle + self.sweep * frac)
            value = self.v_min + (self.v_max - self.v_min) * frac
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            draw_text(image, (x, y), f"{value:g}", font, color, anchor="mm",
                      shadow=None)


@register("led")
class LedWidget(Widget):
    """A status lamp driven by a boolean channel or a threshold."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = spec.get("channel")
        if not self.channel:
            raise ValueError("led needs a 'channel'")
        self.channel = self.resolve_channel(self.channel, "led")
        source = ctx.telemetry[self.channel]
        self.label = str(spec.get("label", source.label))
        self.threshold = float(spec.get("threshold", 0.5))
        self.invert = bool(spec.get("invert", False))
        self.on_text = str(spec.get("on_text", ""))
        self.off_text = str(spec.get("off_text", ""))
        self.size = float(spec.get("size", 18))
        self.label_size = float(spec.get("label_size", 24))
        self.glow = bool(spec.get("glow", True))
        self.off_opacity = float(spec.get("off_opacity", 0.28))
        self.label_font = ctx.font(str(spec.get("font", self.theme.font_bold)),
                                   self.label_size)

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        scale = self.ctx.scale
        raw = self.ctx.value_at(str(self.channel), data_t, interpolate=False)
        state = bool(math.isfinite(raw) and raw >= self.threshold)
        if self.invert:
            state = not state
        on_color = self.color("on_color", "good")
        off_color = self.color("off_color", "text_dim")
        color = on_color if state else with_alpha(off_color, self.off_opacity)

        radius = self.size * scale
        cx = self.rect.x + radius
        cy = self.rect.y + radius
        draw = ImageDraw.Draw(image)
        if state and self.glow:
            # Glow on a local scratch layer - blurring the frame would cost
            # more than everything else in the widget put together.
            blur = radius * 0.7
            extent = int(radius * 2.1 + blur * 3 + 2)
            size = extent * 2
            glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(glow_layer).ellipse(
                (extent - radius * 2.1, extent - radius * 2.1,
                 extent + radius * 2.1, extent + radius * 2.1),
                fill=with_alpha(on_color, 0.30))
            composite_clipped(
                image, glow_layer.filter(ImageFilter.GaussianBlur(blur)),
                int(round(cx - extent)), int(round(cy - extent)))
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=color)

        text = (self.on_text if state else self.off_text) or self.label
        if text:
            shadow, offset, blur = self.text_shadow()
            draw_text(image, (cx + radius * 1.9, cy), text.upper(),
                      self.label_font,
                      self.color("color", "text") if state
                      else with_alpha(self.color("color", "text"), 0.55),
                      anchor="lm", shadow=shadow, shadow_offset=offset,
                      shadow_blur=blur)


@register("image")
class ImageWidget(Widget):
    """A logo or watermark composited into the overlay."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        path = spec.get("path") or spec.get("src")
        if not path:
            raise ValueError("image widget needs a 'path'")
        self.path = Path(str(path)).expanduser()
        if not self.path.is_file():
            raise ValueError(f"image not found: {self.path}")
        self._img: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def prepare(self) -> None:
        img = Image.open(self.path).convert("RGBA")
        target_w = int(round(self.rect.w)) if self.rect.w > 0 else 0
        target_h = int(round(self.rect.h)) if self.rect.h > 0 else 0
        if target_w and not target_h:
            target_h = int(round(img.height * target_w / img.width))
        elif target_h and not target_w:
            target_w = int(round(img.width * target_h / img.height))
        if target_w and target_h:
            img = img.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)
        self._img = img

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._img is None:
            return
        image.alpha_composite(self._img,
                              (int(round(self.rect.x)), int(round(self.rect.y))))
