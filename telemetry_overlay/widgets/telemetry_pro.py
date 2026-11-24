"""Pro motorsport, cycling, FPV, rocketry & broadcast widgets.

Includes:
- ``elevation`` (terrain elevation profile with gradient fill, slope % & elevation gain)
- ``sparkline`` (compact broadcast KPI card with value, trend & inline area mini-plot)
- ``pedals`` (motorsport throttle/brake/clutch bars + rotating steering wheel + gear)
- ``sticks`` (FPV/drone RC dual thumbstick gimbals with live motion trails)
- ``lap_timer`` (racing/cycling lap counter, live lap clock, best/last lap & delta)
- ``progress`` (mission/clip progress bar with event pins & slow-motion speed badge)
- ``badge`` (color-mapped status pill for text/enum channels or numeric states)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from ..theme import (
    RGBA, Rect, composite_clipped, draw_text, parse_color, rounded_rect,
    text_size, with_alpha,
)
from .base import (
    RenderContext, Widget, format_value, parse_zones, register, zone_color,
)

__all__ = [
    "ElevationWidget", "SparklineWidget", "PedalsWidget", "SticksWidget",
    "LapTimerWidget", "ProgressWidget", "BadgeWidget",
]


def _fmt_lap(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "--:--.--"
    mins, rest = divmod(seconds, 60.0)
    return f"{int(mins):02d}:{rest:05.2f}"


# ---------------------------------------------------------------------------
# 1. Elevation Profile
# ---------------------------------------------------------------------------

@register("elevation", "profile")
class ElevationWidget(Widget):
    """Terrain elevation profile with gradient fill, slope % and elevation gain."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.alt_key = self.resolve_channel(
            spec.get("channel", ["altitude", "alt", "ele", "elevation", "rel_altitude"]),
            "elevation",
        )
        self.grade_key = self.resolve_optional_channel(spec.get("grade_channel", ["grade", "slope"]))
        self.gain_key = self.resolve_optional_channel(spec.get("gain_channel", ["elevation_gain"]))
        self.dist_key = self.resolve_optional_channel(spec.get("distance_channel", ["distance"]))
        self.padding = float(spec.get("padding", 14.0))
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                 float(spec.get("value_size", 24)))
        self.lbl_font = ctx.font(str(spec.get("font", self.theme.font)),
                                 float(spec.get("label_size", 15)))
        self._static: Image.Image | None = None
        self._active_strip: Image.Image | None = None
        self._plot: Rect = Rect(0, 0, 1, 1)
        self._x_lo = 0.0
        self._x_hi = 1.0
        self._y_lo = 0.0
        self._y_hi = 1.0
        self._use_dist = False

    def default_width(self, ctx: RenderContext) -> float:
        return 420.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 145.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        hdr_h = text_size(self.val_font, "1234 m")[1] * 1.45
        self._plot = Rect(pad, pad + hdr_h, max(16.0, w - 2 * pad), max(16.0, h - 2 * pad - hdr_h))

        tel = self.ctx.telemetry
        alt = tel[self.alt_key].values
        fin = alt[np.isfinite(alt)]
        if fin.size < 2:
            self._static = self.build_panel_layer(w, h)
            return

        self._use_dist = bool(
            self.dist_key and self.dist_key in tel and tel[self.dist_key].finite.size >= 2
            and (tel[self.dist_key].range()[1] - tel[self.dist_key].range()[0]) > 1.0
        )
        xs_raw = tel[self.dist_key].values if self._use_dist and self.dist_key else tel.time
        self._x_lo, self._x_hi = float(np.nanmin(xs_raw)), float(np.nanmax(xs_raw))
        if self._x_hi <= self._x_lo:
            self._x_hi = self._x_lo + 1.0
        lo, hi = float(fin.min()), float(fin.max())
        span = max(5.0, hi - lo)
        self._y_lo = lo - span * 0.12
        self._y_hi = hi + span * 0.15

        pw, ph = self._plot.size()
        ss = 2
        step = max(1, xs_raw.size // (pw * 2))
        sx = ((xs_raw[::step] - self._x_lo) / (self._x_hi - self._x_lo)) * pw * ss
        sy = (1.0 - (alt[::step] - self._y_lo) / (self._y_hi - self._y_lo)) * ph * ss
        pts = [(float(x), float(y)) for x, y in zip(sx, sy) if math.isfinite(x) and math.isfinite(y)]

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        acc = self.color("color", "accent")
        ghost = Image.new("RGBA", (pw * ss, ph * ss), (0, 0, 0, 0))
        active = Image.new("RGBA", (pw * ss, ph * ss), (0, 0, 0, 0))
        if len(pts) >= 2:
            poly = pts + [(pts[-1][0], ph * ss), (pts[0][0], ph * ss)]
            gd = ImageDraw.Draw(ghost)
            gd.polygon(poly, fill=with_alpha(self.color("track_color", "text"), 0.12))
            gd.line(pts, fill=with_alpha(self.color("track_color", "text"), 0.32),
                    width=max(2, int(round(2.0 * scale * ss))), joint="curve")

            ad = ImageDraw.Draw(active)
            ad.polygon(poly, fill=with_alpha(acc, 0.34))
            ad.line(pts, fill=acc, width=max(2, int(round(3.0 * scale * ss))), joint="curve")

        composite_clipped(static, ghost.reduce(ss), int(round(self._plot.x)), int(round(self._plot.y)))
        self._static = static
        self._active_strip = active.reduce(ss)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        if self._active_strip is None:
            return
        scale = self.ctx.scale
        alt = self.ctx.value_at(self.alt_key, data_t)
        x_val = self.ctx.value_at(self.dist_key, data_t) if (self._use_dist and self.dist_key) else data_t
        frac_x = float(np.clip((x_val - self._x_lo) / (self._x_hi - self._x_lo), 0.0, 1.0))
        clip_w = max(1, int(round(frac_x * self._active_strip.width)))
        if clip_w > 0:
            crop = self._active_strip.crop((0, 0, clip_w, self._active_strip.height))
            composite_clipped(
                image, crop,
                int(round(self.rect.x + self._plot.x)), int(round(self.rect.y + self._plot.y)),
            )

        # Live dot + drop line
        px = self.rect.x + self._plot.x + frac_x * self._plot.w
        py = self.rect.y + self._plot.y + (
            1.0 - (alt - self._y_lo) / (self._y_hi - self._y_lo)
        ) * self._plot.h if math.isfinite(alt) else self.rect.y + self._plot.cy
        acc = self.color("color", "accent")
        r = 5.5 * scale
        dot_layer = Image.new("RGBA", (int(r * 4) + 6, int(self._plot.h) + int(r * 4) + 6), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dot_layer)
        dcx = dot_layer.width / 2.0
        dcy = py - (self.rect.y + self._plot.y) + r * 2
        dd.line([(dcx, dcy), (dcx, self._plot.h + r * 2)], fill=with_alpha(acc, 0.7), width=max(1, int(round(1.5 * scale))))
        dd.ellipse((dcx - r, dcy - r, dcx + r, dcy + r), fill=acc, outline=(255, 255, 255, 240), width=max(1, int(round(scale))))
        composite_clipped(
            image, dot_layer,
            int(round(px - dcx)), int(round(self.rect.y + self._plot.y - r * 2)),
        )

        # Header readouts: Altitude | Slope % | Gain +m
        shadow, offset, blur = self.text_shadow()
        pad = self.padding * scale
        unit = self.ctx.telemetry[self.alt_key].unit or "m"
        draw_text(
            image, (self.rect.x + pad, self.rect.y + pad * 0.75),
            f"{format_value(alt, '{:.0f}')} {unit}", self.val_font,
            self.color("value_color", "text"), anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        right_parts: list[str] = []
        if self.grade_key:
            gr = self.ctx.value_at(self.grade_key, data_t)
            if math.isfinite(gr):
                right_parts.append(f"{gr:+.1f}%")
        if self.gain_key:
            gn = self.ctx.value_at(self.gain_key, data_t)
            if math.isfinite(gn):
                right_parts.append(f"+{gn:.0f}m")
        if right_parts:
            draw_text(
                image, (self.rect.x1 - pad, self.rect.y + pad * 0.9),
                "   ".join(right_parts), self.lbl_font,
                self.color("label_color", "text_dim"), anchor="ra",
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )


# ---------------------------------------------------------------------------
# 2. Compact Sparkline KPI Card
# ---------------------------------------------------------------------------

@register("sparkline", "mini_graph")
class SparklineWidget(Widget):
    """Compact broadcast KPI card: label, live value, delta arrow and inline sparkline."""

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
                raise ValueError("sparkline needs a 'channel' or 'expression'")
            self.channel = self.resolve_channel(raw_ch, "sparkline")
        src = ctx.telemetry[self.channel]
        self.label = str(spec.get("label", src.label))
        self.unit = str(spec.get("unit", src.unit))
        self.format = str(spec.get("format", "{:.1f}"))
        self.zones = parse_zones(spec.get("zones"))
        self.padding = float(spec.get("padding", 12.0))
        self.lbl_font = ctx.font(str(spec.get("font", self.theme.font)), float(spec.get("label_size", 15)))
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), float(spec.get("value_size", 28)))
        self._static: Image.Image | None = None
        self._strip: Image.Image | None = None
        self._spark_box: Rect = Rect(0, 0, 1, 1)
        self._t_lo = 0.0
        self._t_hi = 1.0

    def default_width(self, ctx: RenderContext) -> float:
        return 250.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 105.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        if self.label:
            draw_text(static, (pad, pad * 0.75), self.label.upper(), self.lbl_font,
                      self.color("label_color", "text_dim"), anchor="la", shadow=None)

        top_used = pad + text_size(self.lbl_font, "X")[1] * 1.2 + text_size(self.val_font, "0")[1] * 1.1
        self._spark_box = Rect(pad, top_used, max(16.0, w - 2 * pad), max(12.0, h - top_used - pad * 0.7))

        tel = self.ctx.telemetry
        self._t_lo = self.ctx.timeline.data_start
        self._t_hi = max(self._t_lo + 1e-6, self.ctx.timeline.data_end)
        mask = (tel.time >= self._t_lo) & (tel.time <= self._t_hi)
        t = tel.time[mask]
        y = tel[self.channel].values[mask]
        fin = y[np.isfinite(y)]
        sw, sh = self._spark_box.size()
        if fin.size >= 2:
            lo, hi = float(fin.min()), float(fin.max())
            if hi <= lo:
                hi = lo + 1.0
            ss = 2
            step = max(1, t.size // (sw * 2))
            sx = ((t[::step] - self._t_lo) / (self._t_hi - self._t_lo)) * sw * ss
            sy = (1.0 - (y[::step] - lo) / (hi - lo)) * (sh - 4) * ss + 2 * ss
            pts = [(float(a), float(b)) for a, b in zip(sx, sy) if math.isfinite(a) and math.isfinite(b)]
            acc = self.color("color", "accent")

            ghost = Image.new("RGBA", (sw * ss, sh * ss), (0, 0, 0, 0))
            active = Image.new("RGBA", (sw * ss, sh * ss), (0, 0, 0, 0))
            if len(pts) >= 2:
                ImageDraw.Draw(ghost).line(
                    pts, fill=with_alpha(acc, 0.22), width=max(2, int(round(2.0 * scale * ss))), joint="curve"
                )
                poly = pts + [(pts[-1][0], sh * ss), (pts[0][0], sh * ss)]
                ad = ImageDraw.Draw(active)
                ad.polygon(poly, fill=with_alpha(acc, 0.26))
                ad.line(pts, fill=acc, width=max(2, int(round(2.4 * scale * ss))), joint="curve")
            composite_clipped(static, ghost.reduce(ss), int(round(self._spark_box.x)), int(round(self._spark_box.y)))
            self._strip = active.reduce(ss)
        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        val = self.ctx.value_at(self.channel, data_t)
        col = zone_color(val, self.zones, self.color("value_color", "text"))
        txt = format_value(val, self.format)
        if self.unit:
            txt = f"{txt} {self.unit}"
        pad = self.padding * scale
        vy = self.rect.y + pad * 0.75 + text_size(self.lbl_font, "X")[1] * 1.25
        shadow, offset, blur = self.text_shadow()
        draw_text(image, (self.rect.x + pad, vy), txt, self.val_font, col,
                  anchor="la", shadow=shadow, shadow_offset=offset, shadow_blur=blur)

        if self._strip is not None:
            frac = float(np.clip((data_t - self._t_lo) / (self._t_hi - self._t_lo), 0.0, 1.0))
            cw = max(1, int(round(frac * self._strip.width)))
            crop = self._strip.crop((0, 0, cw, self._strip.height))
            composite_clipped(
                image, crop,
                int(round(self.rect.x + self._spark_box.x)),
                int(round(self.rect.y + self._spark_box.y)),
            )


# ---------------------------------------------------------------------------
# 3. Motorsport Driver Inputs (Throttle / Brake / Clutch / Steering / Gear)
# ---------------------------------------------------------------------------

@register("pedals", "inputs")
class PedalsWidget(Widget):
    """Motorsport pedal bars (throttle/brake/clutch), steering wheel & gear badge."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.throttle_key = self.resolve_optional_channel(
            spec.get("throttle_channel", ["throttle", "accel_pedal", "tps", "gas"])
        )
        self.brake_key = self.resolve_optional_channel(
            spec.get("brake_channel", ["brake", "brake_pressure", "brk"])
        )
        self.clutch_key = self.resolve_optional_channel(
            spec.get("clutch_channel", ["clutch"])
        )
        self.steering_key = self.resolve_optional_channel(
            spec.get("steering_channel", ["steering", "steer", "steering_angle"])
        )
        self.gear_key = self.resolve_optional_channel(
            spec.get("gear_channel", ["gear"])
        )
        if not any((self.throttle_key, self.brake_key, self.clutch_key, self.steering_key, self.gear_key)):
            from .base import ChannelNotFound
            raise ChannelNotFound(
                "pedals widget needs at least one of throttle/brake/clutch/steering/gear channels"
            )
        self.padding = float(spec.get("padding", 14.0))
        self.lbl_font = ctx.font(str(spec.get("font", self.theme.font)), 13)
        self.gear_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), 42)
        self._static: Image.Image | None = None
        self._max_vals: dict[str, float] = {}
        for k in (self.throttle_key, self.brake_key, self.clutch_key):
            if k:
                _, hi = ctx.telemetry[k].range()
                self._max_vals[k] = 1.0 if hi <= 1.05 else (100.0 if hi <= 105.0 else hi)

    def default_width(self, ctx: RenderContext) -> float:
        return 260.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 145.0 * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        self._static = self.build_panel_layer(w, h)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        cx = pad
        bar_h = h - 2 * pad - 18 * scale
        bar_w = 24 * scale
        gap = 10 * scale
        dim = self.color("label_color", "text_dim")
        track_col = with_alpha(self.color("track_color", "text"), 0.20)

        bars = [
            (self.clutch_key, "CLU", parse_color("#38bdf8")),
            (self.brake_key, "BRK", parse_color("#ef4444")),
            (self.throttle_key, "THR", parse_color("#22c55e")),
        ]
        for key, lbl, col in bars:
            if not key:
                continue
            v = self.ctx.value_at(key, data_t)
            frac = float(np.clip(v / max(1e-6, self._max_vals[key]), 0.0, 1.0)) if math.isfinite(v) else 0.0
            rounded_rect(draw, (cx, pad, cx + bar_w, pad + bar_h), 4 * scale, fill=track_col)
            fh = bar_h * frac
            if fh > 1:
                rounded_rect(draw, (cx, pad + bar_h - fh, cx + bar_w, pad + bar_h), 4 * scale, fill=col)
            draw.text((cx + bar_w / 2, h - pad * 0.6), lbl, font=self.lbl_font, fill=dim, anchor="md")
            cx += bar_w + gap

        # Steering wheel icon
        if self.steering_key:
            ang_deg = self.ctx.value_at(self.steering_key, data_t)
            if not math.isfinite(ang_deg):
                ang_deg = 0.0
            rem_w = w - cx - pad - (58 * scale if self.gear_key else 0)
            wr = min(rem_w * 0.45, (h - 2 * pad) * 0.42)
            wcx = cx + max(wr + 4 * scale, rem_w / 2.0)
            wcy = h / 2.0
            if wr > 10:
                sw = max(2, int(round(5 * scale)))
                draw.ellipse((wcx - wr, wcy - wr, wcx + wr, wcy + wr),
                             outline=(226, 232, 240, 220), width=sw)
                # Top center marker + 3 spokes rotated by steering angle
                rad0 = math.radians(ang_deg - 90.0)
                mx = wcx + wr * math.cos(rad0)
                my = wcy + wr * math.sin(rad0)
                mr = 4.5 * scale
                draw.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=parse_color("#ef4444"))
                for spk_deg in (ang_deg, ang_deg + 180.0, ang_deg + 90.0):
                    r_a = math.radians(spk_deg)
                    draw.line([(wcx, wcy), (wcx + (wr - sw) * math.cos(r_a), wcy + (wr - sw) * math.sin(r_a))],
                              fill=(203, 213, 225, 200), width=max(2, int(round(3.5 * scale))))
                draw.ellipse((wcx - 7 * scale, wcy - 7 * scale, wcx + 7 * scale, wcy + 7 * scale),
                             fill=(30, 41, 59, 240), outline=(226, 232, 240, 220))
            cx = wcx + wr + 10 * scale

        # Gear box
        if self.gear_key:
            g_ch = self.ctx.telemetry[self.gear_key]
            if g_ch.is_text:
                g_txt = self.ctx.text_at(self.gear_key, data_t) or "-"
            else:
                gv = self.ctx.value_at(self.gear_key, data_t, interpolate=False)
                if not math.isfinite(gv):
                    g_txt = "-"
                elif int(round(gv)) == 0:
                    g_txt = "N"
                elif int(round(gv)) < 0:
                    g_txt = "R"
                else:
                    g_txt = str(int(round(gv)))
            gx1 = w - pad
            gx0 = max(cx, gx1 - 54 * scale)
            rounded_rect(
                draw, (gx0, pad + 8 * scale, gx1, h - pad - 8 * scale), 6 * scale,
                fill=(15, 23, 42, 210), outline=self.color("accent", "accent"),
                width=max(1, int(round(1.8 * scale))),
            )
            draw.text(((gx0 + gx1) / 2, h / 2), g_txt, font=self.gear_font,
                      fill=self.color("value_color", "text"), anchor="mm")

        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 4. FPV / Drone RC Dual Thumbsticks
# ---------------------------------------------------------------------------

@register("sticks", "rc_sticks", "gimbal")
class SticksWidget(Widget):
    """FPV / drone dual RC thumbsticks (Mode 2: throttle/yaw left, pitch/roll right)."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.lx = self.resolve_optional_channel(spec.get("left_x", ["rc_yaw", "yaw_stick", "ch4"]))
        self.ly = self.resolve_optional_channel(spec.get("left_y", ["rc_throttle", "throttle_stick", "ch3", "throttle"]))
        self.rx = self.resolve_optional_channel(spec.get("right_x", ["rc_roll", "roll_stick", "ch1", "roll"]))
        self.ry = self.resolve_optional_channel(spec.get("right_y", ["rc_pitch", "pitch_stick", "ch2", "pitch"]))
        if not any((self.lx, self.ly, self.rx, self.ry)):
            from .base import ChannelNotFound
            raise ChannelNotFound("sticks widget needs RC channels (left_x, left_y, right_x, right_y)")
        self.v_min = float(spec.get("min", -1.0))
        self.v_max = float(spec.get("max", 1.0))
        self.padding = float(spec.get("padding", 14.0))
        self.trail_seconds = float(spec.get("trail", 0.35))
        self._static: Image.Image | None = None
        self._boxes: list[Rect] = []

    def default_width(self, ctx: RenderContext) -> float:
        return 300.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 150.0 * ctx.scale

    def _norm(self, key: str | None, data_t: float) -> float:
        if not key:
            return 0.0
        v = self.ctx.value_at(key, data_t)
        if not math.isfinite(v):
            return 0.0
        # Auto-handle standard RC PWM 1000..2000 if detected
        if v > 500.0:
            return float(np.clip((v - 1500.0) / 500.0, -1.0, 1.0))
        mid = (self.v_min + self.v_max) / 2.0
        half = max(1e-6, (self.v_max - self.v_min) / 2.0)
        return float(np.clip((v - mid) / half, -1.0, 1.0))

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        gap = 18 * scale
        sz = min((w - 2 * pad - gap) / 2.0, h - 2 * pad)
        y0 = (h - sz) / 2.0
        x0 = (w - (sz * 2 + gap)) / 2.0
        self._boxes = [Rect(x0, y0, sz, sz), Rect(x0 + sz + gap, y0, sz, sz)]

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)
        draw = ImageDraw.Draw(static)
        grid = parse_color(self.spec.get("grid_color", self.theme.grid))
        axis = parse_color(self.spec.get("axis_color", self.theme.axis))
        for b in self._boxes:
            rounded_rect(draw, b.as_tuple(), 10 * scale, fill=(0, 0, 0, 70), outline=axis,
                         width=max(1, int(round(1.4 * scale))))
            draw.line([(b.x + 6 * scale, b.cy), (b.x1 - 6 * scale, b.cy)], fill=grid, width=1)
            draw.line([(b.cx, b.y + 6 * scale), (b.cx, b.y1 - 6 * scale)], fill=grid, width=1)
        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        acc = self.color("color", "accent")
        r = 8.0 * scale

        for box, (kx, ky) in zip(self._boxes, ((self.lx, self.ly), (self.rx, self.ry))):
            inset_r = box.w / 2.0 - r - 4 * scale
            if self.trail_seconds > 0:
                pts = []
                for i in range(10):
                    ti = data_t - self.trail_seconds * (1.0 - i / 9.0)
                    px = box.cx + self._norm(kx, ti) * inset_r
                    py = box.cy - self._norm(ky, ti) * inset_r
                    pts.append((px, py))
                for i in range(len(pts) - 1):
                    draw.line([pts[i], pts[i + 1]], fill=with_alpha(acc, (i + 1) / len(pts) * 0.65),
                              width=max(1, int(round(2.5 * scale))))
            px = box.cx + self._norm(kx, data_t) * inset_r
            py = box.cy - self._norm(ky, data_t) * inset_r
            draw.line([(box.cx, box.cy), (px, py)], fill=with_alpha(acc, 0.55), width=max(1, int(round(2 * scale))))
            draw.ellipse((px - r, py - r, px + r, py + r), fill=acc,
                         outline=(255, 255, 255, 240), width=max(1, int(round(1.5 * scale))))

        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 5. Motorsport / Cycling Lap Timer
# ---------------------------------------------------------------------------

@register("lap_timer", "laps")
class LapTimerWidget(Widget):
    """Lap counter, live running lap time, last/best lap and live split delta."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.lap_key = self.resolve_optional_channel(spec.get("lap_channel", ["lap"]))
        self.lap_time_key = self.resolve_optional_channel(spec.get("lap_time_channel", ["lap_time"]))
        self.last_lap_key = self.resolve_optional_channel(spec.get("last_lap_channel", ["last_lap"]))
        self.best_lap_key = self.resolve_optional_channel(spec.get("best_lap_channel", ["best_lap"]))
        self.delta_key = self.resolve_optional_channel(spec.get("delta_channel", ["lap_delta"]))
        self.padding = float(spec.get("padding", 16.0))
        self.hdr_font = ctx.font(str(spec.get("font", self.theme.font_bold)), float(spec.get("label_size", 18)))
        self.time_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), float(spec.get("value_size", 36)))
        self.sub_font = ctx.font(str(spec.get("font", self.theme.font_mono)), float(spec.get("sub_size", 18)))
        self._static: Image.Image | None = None
        self._total_laps = 0
        if self.lap_key and self.lap_key in ctx.telemetry:
            fin = ctx.telemetry[self.lap_key].finite
            if fin.size:
                self._total_laps = int(round(float(fin.max())))

    def default_width(self, ctx: RenderContext) -> float:
        return 270.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 145.0 * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        self._static = self.build_panel_layer(w, h)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        pad = self.padding * scale
        shadow, offset, blur = self.text_shadow()

        lap_n = int(round(self.ctx.value_at(self.lap_key, data_t, interpolate=False))) if self.lap_key else 1
        lap_t = self.ctx.value_at(self.lap_time_key, data_t) if self.lap_time_key else max(0.0, data_t - self.ctx.t0)
        best_t = self.ctx.value_at(self.best_lap_key, data_t, interpolate=False) if self.best_lap_key else math.nan
        last_t = self.ctx.value_at(self.last_lap_key, data_t, interpolate=False) if self.last_lap_key else math.nan
        delta = self.ctx.value_at(self.delta_key, data_t, interpolate=False) if self.delta_key else math.nan

        lap_str = f"LAP {lap_n}/{self._total_laps}" if self._total_laps > 1 else f"LAP {lap_n}"
        y = self.rect.y + pad
        draw_text(image, (self.rect.x + pad, y), lap_str, self.hdr_font,
                  self.color("accent", "accent"), anchor="la",
                  shadow=shadow, shadow_offset=offset, shadow_blur=blur)

        if math.isfinite(delta):
            dcol = parse_color("#22c55e") if delta <= 0.0 else parse_color("#ef4444")
            draw_text(image, (self.rect.x1 - pad, y), f"{delta:+.2f}s", self.hdr_font, dcol,
                      anchor="ra", shadow=shadow, shadow_offset=offset, shadow_blur=blur)

        y += text_size(self.hdr_font, "LAP")[1] * 1.45
        draw_text(image, (self.rect.x + pad, y), _fmt_lap(lap_t), self.time_font,
                  self.color("value_color", "text"), anchor="la",
                  shadow=shadow, shadow_offset=offset, shadow_blur=blur)

        y += text_size(self.time_font, "00")[1] * 1.38
        dim = self.color("label_color", "text_dim")
        sub_step = text_size(self.sub_font, "00")[1] * 1.35
        if math.isfinite(best_t):
            draw_text(image, (self.rect.x + pad, y), "BEST", self.sub_font,
                      parse_color("#c084fc"), anchor="la", shadow=None)
            draw_text(image, (self.rect.x1 - pad, y), _fmt_lap(best_t), self.sub_font,
                      parse_color("#c084fc"), anchor="ra", shadow=None)
            y += sub_step
        if math.isfinite(last_t):
            draw_text(image, (self.rect.x + pad, y), "LAST", self.sub_font,
                      dim, anchor="la", shadow=None)
            draw_text(image, (self.rect.x1 - pad, y), _fmt_lap(last_t), self.sub_font,
                      dim, anchor="ra", shadow=None)


# ---------------------------------------------------------------------------
# 6. Mission / Clip Progress Bar with Event Pins & Slow-Mo Speed Indicator
# ---------------------------------------------------------------------------

@register("progress", "timeline_bar")
class ProgressWidget(Widget):
    """Full-width mission progress bar with event pins and active slow-mo badge."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.bar_height = float(spec.get("thickness", 8.0))
        self.show_events = bool(spec.get("events", True))
        self.show_speed_badge = bool(spec.get("speed_badge", True))
        self.font = ctx.font(str(spec.get("font", self.theme.font_mono)),
                             float(spec.get("size", 15)))
        self._static: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.92

    def default_height(self, ctx: RenderContext) -> float:
        return 36.0 * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        scale = self.ctx.scale
        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if self.spec.get("panel", False):
            panel = self.build_panel_layer(w, h)
            if panel is not None:
                static.alpha_composite(panel)
        draw = ImageDraw.Draw(static)
        bh = self.bar_height * scale
        by = h - bh - 4 * scale
        rounded_rect(
            draw, (0, by, w - 1, by + bh), bh / 2,
            fill=with_alpha(self.color("track_color", "text"), 0.24),
        )
        if self.show_events:
            t0, t1 = self.ctx.timeline.data_start, self.ctx.timeline.data_end
            span = max(1e-6, t1 - t0)
            for ev in self.ctx.events:
                et = float(ev.get("time", math.nan))
                if not math.isfinite(et) or not (t0 <= et <= t1):
                    continue
                ex = (et - t0) / span * (w - 1)
                col = parse_color(ev.get("color", self.theme.accent))
                draw.line([(ex, by - 4 * scale), (ex, by + bh + 2 * scale)],
                          fill=with_alpha(col, 0.85), width=max(1, int(round(2 * scale))))
        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        w, h = self.rect.size()
        t0, t1 = self.ctx.timeline.data_start, self.ctx.timeline.data_end
        frac = float(np.clip((data_t - t0) / max(1e-6, t1 - t0), 0.0, 1.0))

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        bh = self.bar_height * scale
        by = h - bh - 4 * scale
        acc = self.color("color", "accent")
        fw = max(bh, frac * (w - 1))
        if frac > 0.001:
            rounded_rect(draw, (0, by, fw, by + bh), bh / 2, fill=acc)

        if self.show_speed_badge:
            spd = self.ctx.timeline.speed_at(data_t)
            if spd < 0.01:
                badge = "HOLD"
                bcol = parse_color("#facc15")
            elif abs(spd - 1.0) > 0.04:
                badge = f"{spd:.2g}x SLOW-MO" if spd < 1.0 else f"{spd:.2g}x FAST"
                bcol = acc
            else:
                badge = ""
                bcol = acc
            if badge:
                tw, th = text_size(self.font, badge)
                px = min(w - tw - 16 * scale, max(0.0, fw - (tw + 14 * scale) / 2))
                py = max(0.0, by - th - 10 * scale)
                rounded_rect(
                    draw, (px, py, px + tw + 14 * scale, py + th + 6 * scale),
                    4 * scale, fill=with_alpha(bcol, 0.90),
                )
                draw.text((px + 7 * scale, py + 3 * scale), badge, font=self.font,
                          fill=(10, 14, 22, 255), anchor="la")

        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 7. Dynamic Text / Enum Status Badge
# ---------------------------------------------------------------------------

@register("badge", "status", "mode")
class BadgeWidget(Widget):
    """Color-mapped pill badge for text/enum channels (gear, flight phase, mode, GPS fix)."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        raw_ch = spec.get("channel")
        if not raw_ch:
            raise ValueError("badge widget needs a 'channel'")
        self.channel = self.resolve_channel(raw_ch, "badge")
        src = ctx.telemetry[self.channel]
        self.is_text = src.is_text
        self.label = str(spec.get("label", ""))
        self.format = str(spec.get("format", "{:.0f}"))
        self.colors: dict[str, RGBA] = {
            str(k).strip().lower(): parse_color(v)
            for k, v in (spec.get("colors") or {}).items()
        }
        self.zones = parse_zones(spec.get("zones"))
        self.size = float(spec.get("size", 22.0))
        self.font = ctx.font(str(spec.get("font", self.theme.font_bold)), self.size)
        self.lbl_font = ctx.font(str(spec.get("label_font", self.theme.font)), self.size * 0.65)
        self.padding_x = float(spec.get("padding_x", 16.0))
        self.padding_y = float(spec.get("padding_y", 8.0))

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        scale = self.ctx.scale
        if self.is_text:
            val_txt = self.ctx.text_at(self.channel, data_t) or "--"
            bg = self.colors.get(val_txt.strip().lower(), self.color("color", "accent"))
        else:
            v = self.ctx.value_at(self.channel, data_t, interpolate=False)
            val_txt = format_value(v, self.format)
            bg = self.colors.get(val_txt.strip().lower()) or zone_color(v, self.zones, self.color("color", "accent"))

        disp = f"{self.label.upper()}: {val_txt.upper()}" if self.label else val_txt.upper()
        tw, th = text_size(self.font, disp)
        px = self.padding_x * scale
        py = self.padding_y * scale
        pw = int(math.ceil(tw + px * 2))
        ph = int(math.ceil(th + py * 2))
        pill = Image.new("RGBA", (max(4, pw), max(4, ph)), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        rounded_rect(pd, (0, 0, pw - 1, ph - 1), ph / 2.0,
                     fill=with_alpha(bg, 0.88),
                     outline=(255, 255, 255, 90), width=max(1, int(round(scale))))
        pd.text((pw / 2.0, ph / 2.0), disp, font=self.font,
                fill=self.color("text_color", "text"), anchor="mm")
        composite_clipped(image, pill, int(round(self.rect.x)), int(round(self.rect.y)))
