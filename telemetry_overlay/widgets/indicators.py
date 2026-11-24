"""Bars, dial gauges (with shift lights & peak ticks), status lamps and image stamps."""

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
from .base import (
    RenderContext, Widget, format_value, parse_zones, peak_value, register,
    zone_color,
)

__all__ = ["BarWidget", "GaugeWidget", "LedWidget", "ImageWidget"]


class _ValueWidget(Widget):
    """Shared plumbing for widgets that display one numeric channel or expression."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        expr = spec.get("expression")
        if expr is not None:
            self.channel = ctx.ensure_expression(
                str(expr), unit=str(spec.get("unit", "")), label=str(spec.get("label", ""))
            )
        else:
            raw_ch = spec.get("channel")
            if not raw_ch:
                raise ValueError(f"{self.type_name} needs a 'channel' or 'expression'")
            self.channel = self.resolve_channel(raw_ch, self.type_name)
        source = ctx.telemetry[self.channel]
        lo, hi = source.range()
        self.v_min = float(spec.get("min", 0.0 if lo >= 0 else lo))
        if "max" in spec:
            self.v_max = float(spec["max"])
        else:
            self.v_max = self._nice_ceil(hi, self.v_min)
        if self.v_max <= self.v_min:
            self.v_max = self.v_min + 1.0
        self.label = str(spec.get("label", source.label))
        self.unit = str(spec.get("unit", source.unit))
        self.format = str(spec.get("format", "{:.1f}"))
        self.zones = parse_zones(spec.get("zones"))

    @staticmethod
    def _nice_ceil(hi: float, lo: float) -> float:
        span = hi - lo
        if span <= 0 or not math.isfinite(span):
            return hi
        exp = math.floor(math.log10(span))
        mag = 10.0 ** exp
        for step in (0.1, 0.2, 0.25, 0.5, 1.0, 2.0):
            s = step * mag
            cand = math.ceil(hi / s - 1e-9) * s
            if cand >= hi and (cand - hi) <= span * 0.25:
                return cand
        return math.ceil(hi / mag) * mag

    def value(self, data_t: float) -> float:
        return float(self.ctx.value_at(str(self.channel), data_t))

    def fraction(self, value: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return float(np.clip((value - self.v_min) / (self.v_max - self.v_min), 0.0, 1.0))


@register("bar")
class BarWidget(_ValueWidget):
    """A level bar (horizontal, vertical, or center-zero bidirectional) with cached static plate."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.orientation = str(spec.get("orientation", "horizontal")).lower()
        if self.orientation not in ("horizontal", "vertical"):
            raise ValueError("bar orientation must be 'horizontal' or 'vertical'")
        self.thickness = float(spec.get("thickness", 20))
        self.show_value = bool(spec.get("show_value", True))
        self.show_label = bool(spec.get("show_label", True))
        self.value_size = float(spec.get("value_size", 26))
        self.label_size = float(spec.get("label_size", 20))
        self.radius = float(spec.get("radius", self.thickness / 2))
        self.track_opacity = float(spec.get("track_opacity", 0.22))
        self.peak_marker = bool(spec.get("peak_marker", False))
        self.bidirectional = bool(spec.get("bidirectional", False))
        self.ticks = int(spec.get("ticks", 0))
        self.padding = float(spec.get("padding", 12 if spec.get("panel", True) is not False else 0))
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), self.value_size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)), self.label_size)
        self._static: Image.Image | None = None
        self._track_local: Rect = Rect(0, 0, 1, 1)

    def _is_horizontal(self) -> bool:
        return str(self.spec.get("orientation", "horizontal")).lower() != "vertical"

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.22 if self._is_horizontal() else 96 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        if self._is_horizontal():
            pad = float(self.spec.get("padding", 12 if self.spec.get("panel", True) is not False else 0))
            thickness = float(self.spec.get("thickness", 20))
            lbl_h = float(self.spec.get("label_size", 20)) * 1.45 if self.spec.get("show_label", True) else 0
            val_h = float(self.spec.get("value_size", 26)) * 1.45 if self.spec.get("show_value", True) else 0
            return (pad * 2 + lbl_h + thickness + val_h + 6) * ctx.scale
        return ctx.height * 0.28

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            layer.alpha_composite(panel)

        pad = self.padding * scale
        thickness = self.thickness * scale
        radius = self.radius * scale
        shadow, offset, blur = self.text_shadow()

        top = pad
        if self.show_label and self.label:
            lx = pad if self.orientation == "horizontal" else w / 2.0
            l_anchor = "la" if self.orientation == "horizontal" else "ma"
            draw_text(
                layer, (lx, top), self.label.upper(), self.label_font,
                self.color("label_color", "text_dim"), anchor=l_anchor,
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
            top += text_size(self.label_font, "Xg")[1] * 1.45

        track_color = with_alpha(self.color("track_color", "text"), self.track_opacity)
        if self.orientation == "horizontal":
            track = Rect(pad, top, max(4.0, w - 2 * pad), thickness)
        else:
            bottom = h - pad
            if self.show_value:
                bottom -= text_size(self.value_font, "Xg")[1] * 1.55
            track = Rect((w - thickness) / 2.0, top, thickness, max(4.0, bottom - top))
        self._track_local = track

        # Alpha-safe track plate
        t_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(t_layer)
        rounded_rect(tdraw, track.as_tuple(), radius, fill=track_color)
        if self.bidirectional and self.v_min < 0 < self.v_max:
            zero_f = (0.0 - self.v_min) / (self.v_max - self.v_min)
            zc = with_alpha(self.color("tick_color", "text"), 0.55)
            zw = max(1, int(round(1.5 * scale)))
            if self.orientation == "horizontal":
                zx = track.x + track.w * zero_f
                tdraw.line([(zx, track.y - 2 * scale), (zx, track.y1 + 2 * scale)], fill=zc, width=zw)
            else:
                zy = track.y1 - track.h * zero_f
                tdraw.line([(track.x - 2 * scale, zy), (track.x1 + 2 * scale, zy)], fill=zc, width=zw)
        layer.alpha_composite(t_layer)
        self._static = layer

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(
                image, self._static, int(round(self.rect.x)), int(round(self.rect.y))
            )

        scale = self.ctx.scale
        value = self.value(data_t)
        frac = self.fraction(value)
        fill_color = zone_color(value, self.zones, self.color("color", "accent"))
        radius = self.radius * scale
        thickness = self.thickness * scale
        shadow, offset, blur = self.text_shadow()

        w, h = self.rect.size()
        dyn = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ddraw = ImageDraw.Draw(dyn)
        track = self._track_local

        if self.bidirectional and self.v_min < 0 < self.v_max:
            zero_f = (0.0 - self.v_min) / (self.v_max - self.v_min)
            lo_f, hi_f = min(zero_f, frac), max(zero_f, frac)
            if hi_f - lo_f > 0.002:
                if self.orientation == "horizontal":
                    filled = Rect(track.x + track.w * lo_f, track.y,
                                  track.w * (hi_f - lo_f), thickness)
                else:
                    y0 = track.y1 - track.h * hi_f
                    y1 = track.y1 - track.h * lo_f
                    filled = Rect(track.x, y0, thickness, max(1.0, y1 - y0))
                rounded_rect(ddraw, filled.as_tuple(), radius, fill=fill_color)
        elif frac > 0.001:
            if self.orientation == "horizontal":
                filled = Rect(track.x, track.y, max(thickness * 0.4, track.w * frac), thickness)
            else:
                bh = max(thickness * 0.4, track.h * frac)
                filled = Rect(track.x, track.y1 - bh, thickness, bh)
            rounded_rect(ddraw, filled.as_tuple(), radius, fill=fill_color)

        if self.ticks > 1:
            tc = with_alpha(self.color("tick_color", "text"), 0.42)
            tw = max(1, int(round(scale)))
            for i in range(1, self.ticks):
                tf = i / self.ticks
                if self.orientation == "horizontal":
                    tx = track.x + track.w * tf
                    ddraw.line([(tx, track.y + track.h * 0.22), (tx, track.y1 - track.h * 0.22)],
                               fill=tc, width=tw)
                else:
                    ty = track.y1 - track.h * tf
                    ddraw.line([(track.x + track.w * 0.22, ty), (track.x1 - track.w * 0.22, ty)],
                               fill=tc, width=tw)

        if self.peak_marker:
            pk = peak_value(self.ctx, str(self.channel), data_t, "max")
            if math.isfinite(pk):
                pf = self.fraction(pk)
                pc = self.color("peak_color", "text")
                pw = max(1, int(round(2.0 * scale)))
                if self.orientation == "horizontal":
                    px = track.x + track.w * pf
                    ddraw.line([(px, track.y), (px, track.y1)], fill=pc, width=pw)
                else:
                    py = track.y1 - track.h * pf
                    ddraw.line([(track.x, py), (track.x1, py)], fill=pc, width=pw)

        composite_clipped(image, dyn, int(round(self.rect.x)), int(round(self.rect.y)))

        if self.show_value:
            text = format_value(value, self.format)
            if self.unit:
                text = f"{text} {self.unit}"
            pad = self.padding * scale
            if self.orientation == "horizontal":
                draw_text(
                    image, (self.rect.x1 - pad, self.rect.y + track.y1 + 6 * scale),
                    text, self.value_font, self.color("value_color", "text"),
                    anchor="ra", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
                )
            else:
                draw_text(
                    image, (self.rect.cx, self.rect.y1 - pad),
                    text, self.value_font, self.color("value_color", "text"),
                    anchor="md", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
                )


@register("gauge", "dial", "speedometer", "tachometer")
class GaugeWidget(_ValueWidget):
    """A circular dial with cached track, scale, ticks, peak marker & optional shift lights."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.start_angle = float(spec.get("start_angle", 135.0))
        self.sweep = float(spec.get("sweep", 270.0))
        self.thickness = float(spec.get("thickness", 16))
        self.needle = bool(spec.get("needle", True))
        self.peak_marker = bool(spec.get("peak_marker", False))
        self.shift_lights = bool(spec.get("shift_lights", False))
        self.show_value = bool(spec.get("show_value", True))
        self.show_label = bool(spec.get("show_label", True))
        self.show_scale = bool(spec.get("show_scale", False))
        self.scale_steps = int(spec.get("scale_steps", 5))
        self.value_size = float(spec.get("value_size", 40))
        self.label_size = float(spec.get("label_size", 18))
        self.unit_size = float(spec.get("unit_size", 18))
        self.track_opacity = float(spec.get("track_opacity", 0.22))
        self.padding = float(spec.get("padding", 10 if spec.get("panel", True) is not False else 0))
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), self.value_size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)), self.label_size)
        self.unit_font = ctx.font(str(spec.get("unit_font", self.theme.font)), self.unit_size)
        self._static: Image.Image | None = None
        self._dial_local: Rect = Rect(0, 0, 1, 1)
        self._ss = 2
        self._dyn_key: tuple[int, int, RGBA] | None = None
        self._dyn_img: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return 250.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 250.0 * ctx.scale

    def prepare(self) -> None:
        """Bake panel, empty track, coloured zone bands, ticks, scale numbers & label ONCE."""
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        inner_w = max(20.0, w - 2 * pad)
        inner_h = max(20.0, h - 2 * pad)
        dial_sz = min(inner_w, inner_h)
        # 2x supersampling is crisp and 2.25x faster than 3x for >= 160px dials
        self._ss = 2 if dial_sz >= 160 else 3
        self._dyn_key = None
        self._dyn_img = None
        ss = self._ss

        dx = (w - dial_sz) / 2.0
        dy = (h - dial_sz) / 2.0
        self._dial_local = Rect(dx, dy, dial_sz, dial_sz)
        dw, dh = self._dial_local.size()

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        thickness = self.thickness * scale
        ring = Image.new("RGBA", (dw * ss, dh * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ring)
        inset = thickness * ss / 2.0 + ss * 2
        arc_box = (inset, inset, dw * ss - inset, dh * ss - inset)

        draw.arc(
            arc_box, self.start_angle, self.start_angle + self.sweep,
            fill=with_alpha(self.color("track_color", "text"), self.track_opacity),
            width=max(1, int(round(thickness * ss))),
        )
        if self.zones:
            for zone in self.zones:
                lo = self.fraction(float(zone["from"]))
                hi = self.fraction(float(zone["to"]))
                if hi <= lo:
                    continue
                draw.arc(
                    arc_box,
                    self.start_angle + self.sweep * lo,
                    self.start_angle + self.sweep * hi,
                    fill=with_alpha(zone["color"], 0.34),
                    width=max(1, int(round(thickness * ss))),
                )

        if self.show_scale and self.scale_steps >= 1:
            cx_s, cy_s = dw * ss / 2.0, dh * ss / 2.0
            r_out = min(cx_s, cy_s) - thickness * ss * 0.95 - ss
            r_in = r_out - 6.0 * scale * ss
            tick_col = with_alpha(self.color("scale_color", "text_dim"), 0.75)
            tw = max(1, int(round(1.6 * scale * ss)))
            for i in range(self.scale_steps + 1):
                ang = math.radians(self.start_angle + self.sweep * (i / self.scale_steps))
                ca, sa = math.cos(ang), math.sin(ang)
                draw.line([(cx_s + r_in * ca, cy_s + r_in * sa),
                           (cx_s + r_out * ca, cy_s + r_out * sa)], fill=tick_col, width=tw)

        composite_clipped(static, ring.reduce(ss), int(round(dx)), int(round(dy)))

        if self.show_scale and self.scale_steps >= 1:
            sfont = self.ctx.font(self.theme.font, self.label_size * 0.76)
            scolor = self.color("scale_color", "text_dim")
            cx, cy = self._dial_local.cx, self._dial_local.cy
            span_v = abs(self.v_max - self.v_min)
            s_fmt = "{:.0f}" if span_v >= 10 else ("{:.1f}" if span_v >= 1 else "{:.2f}")
            labels = [
                s_fmt.format(self.v_min + (self.v_max - self.v_min) * i / self.scale_steps)
                for i in range(self.scale_steps + 1)
            ]
            widest = max(text_size(sfont, lbl)[0] for lbl in labels)
            radius = dial_sz / 2.0 - thickness - widest / 2.0 - 9 * scale
            radius = max(radius, dial_sz * 0.24)
            for i, lbl in enumerate(labels):
                frac = i / self.scale_steps
                ang = math.radians(self.start_angle + self.sweep * frac)
                draw_text(
                    static, (cx + radius * math.cos(ang), cy + radius * math.sin(ang)),
                    lbl, sfont, scolor, anchor="mm", shadow=None,
                )

        if self.show_label and self.label:
            shadow, offset, blur = self.text_shadow()
            lbl_y = h - pad - self.label_size * scale * 0.45
            draw_text(
                static, (self._dial_local.cx, lbl_y), self.label.upper(),
                self.label_font, self.color("label_color", "text_dim"),
                anchor="mm", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )

        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(
                image, self._static, int(round(self.rect.x)), int(round(self.rect.y))
            )

        scale = self.ctx.scale
        box = self._dial_local
        dw, dh = box.size()
        ss = self._ss
        value = self.value(data_t)
        frac = self.fraction(value)
        color = zone_color(value, self.zones, self.color("color", "accent"))
        thickness = self.thickness * scale

        pfrac = -1.0
        if self.peak_marker:
            pk = peak_value(self.ctx, str(self.channel), data_t, "max")
            if math.isfinite(pk):
                pfrac = self.fraction(pk)

        dyn_key = (int(round(frac * 1000)), int(round(pfrac * 1000)), color)
        if self._dyn_key != dyn_key or self._dyn_img is None:
            layer = Image.new("RGBA", (dw * ss, dh * ss), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            inset = thickness * ss / 2.0 + ss * 2
            arc_box = (inset, inset, dw * ss - inset, dh * ss - inset)

            if frac > 0.001:
                draw.arc(
                    arc_box, self.start_angle, self.start_angle + self.sweep * frac,
                    fill=color, width=max(1, int(round(thickness * ss))),
                )

            if pfrac >= 0.0:
                pang = math.radians(self.start_angle + self.sweep * pfrac)
                cx_s, cy_s = dw * ss / 2.0, dh * ss / 2.0
                r1 = min(cx_s, cy_s) - ss
                r0 = r1 - thickness * ss * 1.15
                draw.line(
                    [(cx_s + r0 * math.cos(pang), cy_s + r0 * math.sin(pang)),
                     (cx_s + r1 * math.cos(pang), cy_s + r1 * math.sin(pang))],
                    fill=self.color("peak_color", "text"),
                    width=max(2, int(round(2.5 * scale * ss))),
                )

            if self.shift_lights:
                n_leds = 7
                cx_s, cy_s = dw * ss / 2.0, dh * ss / 2.0
                lr = 3.6 * scale * ss
                spread = min(dw * ss * 0.42, n_leds * lr * 2.8)
                for idx in range(n_leds):
                    thresh = 0.62 + (idx / max(1, n_leds - 1)) * 0.34
                    lx = cx_s - spread / 2.0 + spread * (idx / max(1, n_leds - 1))
                    ly = thickness * ss * 0.55 + lr * 0.4
                    if idx < 3:
                        lcol = parse_color("#22c55e")
                    elif idx < 5:
                        lcol = parse_color("#facc15")
                    else:
                        lcol = parse_color("#ef4444")
                    c_draw = lcol if (frac >= thresh) else with_alpha(lcol, 0.20)
                    draw.ellipse((lx - lr, ly - lr, lx + lr, ly + lr), fill=c_draw)

            if self.needle:
                angle = math.radians(self.start_angle + self.sweep * frac)
                cx_s, cy_s = dw * ss / 2.0, dh * ss / 2.0
                ca, sa = math.cos(angle), math.sin(angle)
                outer_r = min(cx_s, cy_s) - thickness * ss * 0.3
                inner_r = (
                    min(cx_s, cy_s) - thickness * ss * 2.15
                    if self.show_value
                    else 0.0
                )
                tip = (cx_s + outer_r * ca, cy_s + outer_r * sa)
                base_pt = (cx_s + inner_r * ca, cy_s + inner_r * sa)
                draw.line([base_pt, tip], fill=color, width=max(2, int(round(3.2 * scale * ss))))
                if not self.show_value:
                    hub = 4.5 * scale * ss
                    draw.ellipse((cx_s - hub, cy_s - hub, cx_s + hub, cy_s + hub), fill=color)

            self._dyn_key = dyn_key
            self._dyn_img = layer.reduce(ss)

        composite_clipped(
            image, self._dyn_img,
            int(round(self.rect.x + box.x)), int(round(self.rect.y + box.y)),
        )

        if self.show_value:
            shadow, offset, blur = self.text_shadow()
            text = format_value(value, self.format)
            cx = self.rect.x + box.cx
            cy = self.rect.y + box.cy - (self.unit_size * scale * 0.25 if self.unit else 0.0)
            draw_text(
                image, (cx, cy), text, self.value_font,
                self.color("value_color", "text"), anchor="mm",
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
            if self.unit:
                draw_text(
                    image, (cx, cy + self.value_size * scale * 0.62),
                    self.unit, self.unit_font, self.color("unit_color", "text_dim"),
                    anchor="ma", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
                )


@register("led")
class LedWidget(Widget):
    """A status lamp driven by a boolean channel or threshold (pre-rendered on/off sprites)."""

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
        self.label_font = ctx.font(str(spec.get("font", self.theme.font_bold)), self.label_size)
        self._sprites: dict[bool, tuple[Image.Image, int, int]] = {}

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def _build_state_sprite(self, state: bool) -> tuple[Image.Image, int, int]:
        scale = self.ctx.scale
        on_color = self.color("on_color", "good")
        off_color = self.color("off_color", "text_dim")
        color = on_color if state else with_alpha(off_color, self.off_opacity)
        radius = self.size * scale
        text = ((self.on_text if state else self.off_text) or self.label).upper()
        tw, th = text_size(self.label_font, text) if text else (0, 0)
        shadow, offset, blur = self.text_shadow()

        glow_pad = int(math.ceil(radius * 2.2 + radius * 0.7 * 3 + 4)) if (state and self.glow) else int(math.ceil(radius + 6))
        text_pad = int(math.ceil(blur * 3 + abs(offset[0]) + abs(offset[1]) + 8)) if (shadow and shadow[3] > 0) else 8
        pad_l = glow_pad
        pad_t = max(glow_pad, int(math.ceil(th / 2 + text_pad)))
        width = pad_l + int(math.ceil(radius * 2.2 + tw)) + text_pad + glow_pad
        height = pad_t * 2 + 4

        sprite = Image.new("RGBA", (max(4, width), max(4, height)), (0, 0, 0, 0))
        cx = float(pad_l)
        cy = float(pad_t)

        if state and self.glow:
            gblur = radius * 0.7
            glow_layer = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow_layer).ellipse(
                (cx - radius * 2.0, cy - radius * 2.0, cx + radius * 2.0, cy + radius * 2.0),
                fill=with_alpha(on_color, 0.35),
            )
            sprite.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(gblur)))

        # Crisp 2x supersampled bulb with subtle specular ring
        bulb_sz = int(math.ceil(radius * 2 + 6))
        bulb = Image.new("RGBA", (bulb_sz * 2, bulb_sz * 2), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bulb)
        bc = bulb_sz
        rr = radius * 2
        bd.ellipse((bc - rr, bc - rr, bc + rr, bc + rr), fill=color)
        if state:
            bd.ellipse((bc - rr * 0.45, bc - rr * 0.45, bc + rr * 0.1, bc + rr * 0.1),
                       fill=(255, 255, 255, 165))
        else:
            bd.ellipse((bc - rr, bc - rr, bc + rr, bc + rr),
                       outline=with_alpha(off_color, 0.55), width=max(2, int(round(1.5 * scale * 2))))
        bulb = bulb.reduce(2)
        composite_clipped(sprite, bulb, int(round(cx - bulb_sz / 2)), int(round(cy - bulb_sz / 2)))

        if text:
            t_col = (
                self.color("color", "text")
                if state
                else with_alpha(self.color("color", "text"), 0.58)
            )
            draw_text(
                sprite, (cx + radius * 1.9, cy), text, self.label_font, t_col,
                anchor="lm", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )

        dest_x = int(round(self.rect.x + radius - cx))
        dest_y = int(round(self.rect.y + radius - cy))
        return sprite, dest_x, dest_y

    def prepare(self) -> None:
        self._sprites = {
            True: self._build_state_sprite(True),
            False: self._build_state_sprite(False),
        }

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        raw = self.ctx.value_at(str(self.channel), data_t, interpolate=False)
        state = bool(math.isfinite(raw) and raw >= self.threshold)
        if self.invert:
            state = not state
        sprite, dx, dy = self._sprites[state]
        composite_clipped(image, sprite, dx, dy)


@register("image", "logo", "icon")
class ImageWidget(Widget):
    """A logo, watermark or telemetry-driven rotating PNG icon."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        raw_path = spec.get("path") or spec.get("src")
        if not raw_path:
            raise ValueError("image widget needs a 'path'")
        p = Path(str(raw_path)).expanduser()
        if not p.is_absolute() and not p.is_file() and ctx.source_dir is not None:
            candidate = ctx.source_dir / p
            if candidate.is_file():
                p = candidate
        if not p.is_file():
            raise ValueError(f"image not found: {p}")
        self.path = p
        self.rotate_channel = self.resolve_optional_channel(spec.get("rotate_channel"))
        self.rotate_scale = float(spec.get("rotate_scale", 1.0))
        self.rotate_offset = float(spec.get("rotate_offset", 0.0))
        self._img: Image.Image | None = None
        self._dest: tuple[int, int] = (0, 0)

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def prepare(self) -> None:
        from ..theme import anchor_offset
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
        # Recompute anchored position using the real final dimensions!
        x = self.ctx.px_x(self.spec.get("x", 0))
        y = self.ctx.px_y(self.spec.get("y", 0))
        dx, dy = anchor_offset(str(self.spec.get("anchor", "top-left")), img.width, img.height)
        self._dest = (int(round(x + dx)), int(round(y + dy)))

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._img is None:
            return
        if self.rotate_channel:
            val = self.ctx.value_at(self.rotate_channel, data_t)
            angle = -(val * self.rotate_scale + self.rotate_offset) if math.isfinite(val) else 0.0
            rot = self._img.rotate(angle, resample=Image.BICUBIC, expand=True)
            cx = self._dest[0] + self._img.width / 2.0
            cy = self._dest[1] + self._img.height / 2.0
            composite_clipped(
                image, rot,
                int(round(cx - rot.width / 2.0)), int(round(cy - rot.height / 2.0)),
            )
            return
        composite_clipped(image, self._img, self._dest[0], self._dest[1])
