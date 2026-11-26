"""Dedicated rocketry widgets: all-in-one Motor Burn card & true 3D Rocket Attitude.

Includes:
- ``motor_burn`` (aliases: ``rocket_motor``, ``thrust_card``, ``static_fire``):
  All-in-one static-fire / rocket motor burn panel showing live thrust (N & kgf),
  peak thrust, live cumulative impulse with automatic NAR/TRA motor class,
  average burn thrust, burn phase badge, ignition-zeroed burn clock, live
  thrust-time curve with ignition/peak/burnout markers, and an animated nozzle
  exhaust plume scaled by live thrust.
- ``rocket_3d`` (aliases: ``rocket_attitude``, ``attitude_3d``, ``orientation_3d``):
  True 3D perspective-projected rocket orientation visualizer driven by Euler
  angles (pitch, roll, yaw) or quaternion channels, with shaded nosecone,
  fuselage, roll-band, swept fins, nozzle, dynamic 3D exhaust flame, 3D ground
  horizon ring, vertical zenith plumb line, off-vertical tilt arc, and live
  tilt/pitch/roll/yaw readouts.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from ..events import detect_events
from ..expressions import evaluate
from ..theme import (
    RGBA, Rect, composite_clipped, draw_text, mix, parse_color, rounded_rect,
    text_size, with_alpha,
)
from .base import RenderContext, Widget, format_value, peak_value, register

__all__ = ["MotorBurnWidget", "Rocket3DWidget", "nar_motor_class"]


# Standard NAR / TRA total impulse motor classes (upper limit in N·s)
_NAR_CLASSES: tuple[tuple[str, float, float], ...] = (
    ("1/4A", 0.0, 0.625),
    ("1/2A", 0.625, 1.25),
    ("A", 1.25, 2.5),
    ("B", 2.5, 5.0),
    ("C", 5.0, 10.0),
    ("D", 10.0, 20.0),
    ("E", 20.0, 40.0),
    ("F", 40.0, 80.0),
    ("G", 80.0, 160.0),
    ("H", 160.0, 320.0),
    ("I", 320.0, 640.0),
    ("J", 640.0, 1280.0),
    ("K", 1280.0, 2560.0),
    ("L", 2560.0, 5120.0),
    ("M", 5120.0, 10240.0),
    ("N", 10240.0, 20480.0),
    ("O", 20480.0, 40960.0),
)


def nar_motor_class(impulse_ns: float, avg_thrust_n: float = 0.0) -> tuple[str, int]:
    """Return ``(designation_or_class, percent_into_class)`` for a total impulse."""
    if not math.isfinite(impulse_ns) or impulse_ns <= 0.05:
        return "--", 0
    for letter, lo, hi in _NAR_CLASSES:
        if impulse_ns <= hi:
            pct = int(round(max(1.0, min(100.0, (impulse_ns - lo) / max(1e-6, hi - lo) * 100.0))))
            desig = f"{letter}{int(round(avg_thrust_n))}" if avg_thrust_n >= 1.0 else f"Class {letter}"
            return desig, pct
    return f"O+{int(round(avg_thrust_n))}" if avg_thrust_n >= 1.0 else "Class O+", 100


# ---------------------------------------------------------------------------
# 1. All-in-one Rocket Motor Burn Card
# ---------------------------------------------------------------------------

@register("motor_burn", "rocket_motor", "thrust_card", "static_fire")
class MotorBurnWidget(Widget):
    """All-in-one rocket motor telemetry card: live thrust, curve, burn time, impulse & class."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = self.resolve_channel(
            spec.get("channel", ["thrust", "thrust_above_rest", "force", "tah"]),
            "motor_burn",
        )
        src = ctx.telemetry[self.channel]
        self.unit = str(spec.get("unit", src.unit or "N"))
        default_title = (
            str(ctx.telemetry.meta.get("Motor designation", "")).strip()
            or "ROCKET MOTOR"
        )
        self.title = str(spec.get("title", default_title))
        self.threshold_fraction = float(spec.get("threshold_fraction", 0.05))
        self.show_plume = bool(spec.get("show_plume", True))
        self.show_class = bool(spec.get("show_class", True))
        self.show_kgf = bool(spec.get("show_kgf", self.unit.strip().lower() == "n"))
        self.zero_on_ignition = bool(spec.get("zero_on_ignition", True))
        self.zoom_burn = bool(spec.get("zoom_burn", False))
        self.padding = float(spec.get("padding", 18.0))
        self.color_accent = self.color("color", "accent")

        self.title_font = ctx.font(str(spec.get("title_font", self.theme.font_bold)), 22)
        self.clock_font = ctx.font(str(spec.get("clock_font", self.theme.font_mono)), 24)
        self.badge_font = ctx.font(str(spec.get("badge_font", self.theme.font_bold)), 15)
        self.kpi_lbl_font = ctx.font(str(spec.get("label_font", self.theme.font)), 14)
        self.kpi_big_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), 40)
        self.kpi_val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), 30)
        self.kpi_sub_font = ctx.font(str(spec.get("font", self.theme.font)), 14)
        self.tick_font = ctx.font(str(spec.get("font", self.theme.font)), 14)

        self._static: Image.Image | None = None
        self._active_curve: Image.Image | None = None
        self._plot: Rect = Rect(0, 0, 1, 1)
        self._plume_box: Rect = Rect(0, 0, 1, 1)
        self._t_lo = 0.0
        self._t_hi = 1.0
        self._y_hi = 1.0
        self._t_ign: float | None = None
        self._t_peak: float | None = None
        self._t_burnout: float | None = None
        self._peak_val = 1.0
        self._impulse_arr: np.ndarray = np.zeros(0)
        self._time_arr: np.ndarray = np.zeros(0)
        self._burn_thresh = 0.0

    def default_width(self, ctx: RenderContext) -> float:
        return 760.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 350.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        tel = self.ctx.telemetry

        self._t_lo = self.ctx.timeline.data_start
        self._t_hi = max(self._t_lo + 1e-6, self.ctx.timeline.data_end)
        self._time_arr = tel.time
        vals = np.asarray(tel[self.channel].values, dtype=float)
        fin = vals[np.isfinite(vals)]
        self._peak_val = float(fin.max()) if fin.size else 1.0
        if self._peak_val <= 0:
            self._peak_val = 1.0
        self._y_hi = self._peak_val * 1.12
        self._burn_thresh = self._peak_val * self.threshold_fraction

        # Cumulative positive impulse integral
        self._impulse_arr = evaluate(f"integral(clip({self.channel}, 0, 1e12))", tel)

        # Auto-detect ignition / peak / burnout for phase & burn clock
        evs = detect_events(tel, self.channel, fraction=self.threshold_fraction)
        ev_map = {e["key"]: float(e["time"]) for e in evs}
        self._t_ign = ev_map.get("ignition")
        self._t_peak = ev_map.get("peak")
        self._t_burnout = ev_map.get("burnout")

        # Optional burn-window zoom (great when motor burn is a short slice of a long flight!)
        if self.zoom_burn and self._t_ign is not None and self._t_burnout is not None:
            burn_dur = max(0.5, self._t_burnout - self._t_ign)
            self._t_lo = max(self._t_lo, self._t_ign - burn_dur * 0.25)
            self._t_hi = min(self._t_hi, self._t_burnout + burn_dur * 0.45)
            if self._t_hi <= self._t_lo:
                self._t_hi = self._t_lo + 1.0

        # Geometry: header -> KPI row -> bottom plot (+ optional nozzle plume on right)
        hdr_h = 36 * scale
        kpi_h = 84 * scale
        plot_top = pad + hdr_h + kpi_h + 14 * scale
        plot_h = max(32.0, h - plot_top - pad - 18 * scale)
        plume_w = (96 * scale) if self.show_plume else 0.0
        left_axis = 46 * scale
        plot_w = max(40.0, w - pad * 2 - left_axis - plume_w - (10 * scale if self.show_plume else 0.0))
        self._plot = Rect(pad + left_axis, plot_top, plot_w, plot_h)
        self._plume_box = Rect(w - pad - plume_w, plot_top - 4 * scale, plume_w, plot_h + 18 * scale)

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        draw = ImageDraw.Draw(static)
        draw_text(
            static, (pad, pad + 2 * scale), self.title.upper(),
            self.title_font, self.color("title_color", "text"), anchor="la", shadow=None,
        )
        div_col = parse_color(self.spec.get("grid_color", self.theme.grid))
        draw.line([(pad, pad + hdr_h), (w - pad, pad + hdr_h)], fill=div_col, width=max(1, int(round(scale))))

        kpi_top = pad + hdr_h + 8 * scale
        col_w = (w - 2 * pad) / 4.0
        dim = self.color("label_color", "text_dim")
        for idx, cap in enumerate(("THRUST", "PEAK THRUST", "TOTAL IMPULSE", "BURN / AVG")):
            cx = pad + idx * col_w
            if idx > 0:
                draw.line(
                    [(cx - 6 * scale, kpi_top + 4 * scale), (cx - 6 * scale, kpi_top + kpi_h - 6 * scale)],
                    fill=div_col, width=max(1, int(round(scale))),
                )
            draw_text(static, (cx + 4 * scale, kpi_top), cap, self.kpi_lbl_font, dim, anchor="la", shadow=None)

        px, py, pw, ph = self._plot.x, self._plot.y, self._plot.w, self._plot.h
        axis_col = parse_color(self.spec.get("axis_color", self.theme.axis))
        lw = max(1, int(round(scale)))
        for i in range(4):
            frac = i / 3.0
            gy = py + (1.0 - frac) * ph
            draw.line([(px, gy), (px + pw, gy)], fill=div_col, width=lw)
            yval = self._y_hi * frac
            draw_text(static, (px - 6 * scale, gy), f"{yval:.0f}", self.tick_font, dim, anchor="rm", shadow=None)
        draw.line([(px, py), (px, py + ph)], fill=axis_col, width=lw)
        draw.line([(px, py + ph), (px + pw, py + ph)], fill=axis_col, width=lw)

        t_zero = self._t_ign if (self.zero_on_ignition and self._t_ign is not None) else self.ctx.t0
        for i in range(5):
            frac = i / 4.0
            tx = px + frac * pw
            t_val = self._t_lo + frac * (self._t_hi - self._t_lo) - t_zero
            draw.line([(tx, py), (tx, py + ph)], fill=with_alpha(div_col, 0.7), width=lw)
            draw_text(static, (tx, py + ph + 4 * scale), f"{t_val:+.1f}s", self.tick_font, dim, anchor="ma", shadow=None)

        mask = (tel.time >= self._t_lo) & (tel.time <= self._t_hi)
        ts = tel.time[mask]
        ys = vals[mask]
        ss = 2
        ipw, iph = self._plot.size()
        step = max(1, ts.size // (ipw * 2))
        sx = ((ts[::step] - self._t_lo) / (self._t_hi - self._t_lo)) * ipw * ss
        sy = (1.0 - np.clip(ys[::step], 0.0, self._y_hi) / self._y_hi) * iph * ss
        pts = [(float(a), float(b)) for a, b in zip(sx, sy) if math.isfinite(a) and math.isfinite(b)]

        ghost = Image.new("RGBA", (ipw * ss, iph * ss), (0, 0, 0, 0))
        active = Image.new("RGBA", (ipw * ss, iph * ss), (0, 0, 0, 0))
        if len(pts) >= 2:
            poly = pts + [(pts[-1][0], iph * ss), (pts[0][0], iph * ss)]
            gd = ImageDraw.Draw(ghost)
            gd.line(pts, fill=with_alpha(self.color_accent, 0.24),
                    width=max(2, int(round(2.0 * scale * ss))), joint="curve")

            ad = ImageDraw.Draw(active)
            ad.polygon(poly, fill=with_alpha(self.color_accent, 0.42))
            ad.line(pts, fill=self.color_accent,
                    width=max(2, int(round(3.2 * scale * ss))), joint="curve")

        ghost_lo = ghost.reduce(ss)
        active_lo = active.reduce(ss)
        arr = np.array(active_lo, dtype=np.uint8)
        fade = np.clip(1.0 - (np.arange(active_lo.height, dtype=np.float32) / max(1.0, active_lo.height)) * 0.72, 0.16, 1.0)
        is_fill = arr[:, :, 3] < 200
        arr[:, :, 3] = np.where(is_fill, np.clip(arr[:, :, 3] * fade[:, None], 0, 255), arr[:, :, 3]).astype(np.uint8)
        self._active_curve = Image.fromarray(arr, mode="RGBA")

        composite_clipped(static, ghost_lo, int(round(px)), int(round(py)))

        # Event marker pins with lane stacking so labels never collide
        last_right = -1e9
        lane = 0
        line_h = text_size(self.tick_font, "X")[1] * 1.25
        for ev_t, ev_lbl, ev_col in (
            (self._t_ign, "IGN", "#facc15"),
            (self._t_peak, "PEAK", "#ff453a"),
            (self._t_burnout, "BO", "#38bdf8"),
        ):
            if ev_t is not None and self._t_lo <= ev_t <= self._t_hi:
                ex = px + ((ev_t - self._t_lo) / (self._t_hi - self._t_lo)) * pw
                c = with_alpha(parse_color(ev_col), 0.75)
                y_cur = py
                while y_cur < py + ph:
                    draw.line([(ex, y_cur), (ex, min(y_cur + 5 * scale, py + ph))], fill=c, width=lw)
                    y_cur += 9 * scale
                tw_ev = text_size(self.tick_font, ev_lbl)[0]
                if ex < last_right:
                    lane = (lane + 1) % 3
                else:
                    lane = 0
                last_right = ex + tw_ev + 8 * scale
                draw_text(static, (ex + 3 * scale, py + 2 * scale + lane * line_h), ev_lbl,
                          self.tick_font, c, anchor="la", shadow=None)

        self._static = static

    def _draw_nozzle_plume(self, layer: Image.Image, thrust_frac: float, frame: int) -> None:
        """Draw a compact metallic rocket nozzle with live thrust-scaled exhaust plume."""
        scale = self.ctx.scale
        bx = self._plume_box
        cx = bx.cx
        top_y = bx.y + 6 * scale
        draw = ImageDraw.Draw(layer)

        # Rocket aft casing & De Laval nozzle bell
        casing_w = 28 * scale
        casing_h = 22 * scale
        throat_w = 13 * scale
        exit_w = 24 * scale
        nozzle_h = 22 * scale
        y_throat = top_y + casing_h
        y_exit = y_throat + nozzle_h

        # Casing
        rounded_rect(
            draw, (cx - casing_w / 2, top_y, cx + casing_w / 2, y_throat + 2 * scale),
            3 * scale, fill=(148, 163, 184, 225), outline=(226, 232, 240, 220),
            width=max(1, int(round(scale))),
        )
        # Converging-diverging nozzle bell
        draw.polygon(
            [(cx - casing_w * 0.42, y_throat), (cx + casing_w * 0.42, y_throat),
             (cx + throat_w / 2, y_throat + nozzle_h * 0.3),
             (cx + exit_w / 2, y_exit), (cx - exit_w / 2, y_exit),
             (cx - throat_w / 2, y_throat + nozzle_h * 0.3)],
            fill=(71, 85, 105, 240), outline=(203, 213, 225, 220),
        )

        # Dynamic flame plume with subtle high-frequency flicker & Mach diamonds
        frac = float(np.clip(thrust_frac, 0.0, 1.2))
        if frac > 0.02:
            avail_h = max(16.0, bx.y1 - y_exit - 8 * scale)
            flicker = 1.0 + 0.04 * math.sin(frame * 1.9)
            plume_h = avail_h * (0.18 + 0.82 * min(1.0, frac)) * flicker
            plume_w = exit_w * (0.45 + 0.48 * min(1.0, frac))

            # Outer orange-red plume
            draw.polygon(
                [(cx - plume_w * 0.55, y_exit), (cx + plume_w * 0.55, y_exit),
                 (cx + plume_w * 0.32, y_exit + plume_h * 0.55),
                 (cx, y_exit + plume_h),
                 (cx - plume_w * 0.32, y_exit + plume_h * 0.55)],
                fill=(255, 107, 10, int(140 + 95 * min(1.0, frac))),
            )
            # Mid amber-yellow plume
            draw.polygon(
                [(cx - plume_w * 0.36, y_exit), (cx + plume_w * 0.36, y_exit),
                 (cx, y_exit + plume_h * 0.74)],
                fill=(255, 204, 41, int(175 + 75 * min(1.0, frac))),
            )
            # Hot white core + Mach shock diamonds
            draw.polygon(
                [(cx - plume_w * 0.20, y_exit), (cx + plume_w * 0.20, y_exit),
                 (cx, y_exit + plume_h * 0.42)],
                fill=(255, 252, 230, 245),
            )
            if frac > 0.25:
                for k in (0.34, 0.56, 0.74):
                    dy = y_exit + plume_h * k
                    dw = plume_w * (0.20 * (1.0 - k * 0.6))
                    dh = 4.5 * scale
                    draw.polygon(
                        [(cx, dy - dh), (cx + dw, dy), (cx, dy + dh), (cx - dw, dy)],
                        fill=(255, 245, 200, 210),
                    )

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))

        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        hdr_h = 36 * scale
        shadow, offset, blur = self.text_shadow()

        thrust = self.ctx.value_at(self.channel, data_t)
        if not math.isfinite(thrust):
            thrust = 0.0
        peak_so_far = peak_value(self.ctx, self.channel, data_t, "max")
        if not math.isfinite(peak_so_far):
            peak_so_far = 0.0
        impulse = float(np.interp(data_t, self._time_arr, self._impulse_arr)) if self._time_arr.size else 0.0

        # Determine live burn phase & burn clock
        t_zero = self._t_ign if (self.zero_on_ignition and self._t_ign is not None) else self.ctx.t0
        rel_t = data_t - t_zero
        if self._t_ign is not None and data_t < self._t_ign:
            phase, phase_col = "STANDBY", parse_color("#64748b")
            active_burn = 0.0
        elif self._t_burnout is not None and data_t > self._t_burnout:
            phase, phase_col = "BURNOUT", parse_color("#38bdf8")
            active_burn = self._t_burnout - (self._t_ign or self._t_lo)
        elif thrust >= self._burn_thresh:
            if self._t_ign is not None and (data_t - self._t_ign) < 0.12:
                phase, phase_col = "IGNITION", parse_color("#facc15")
            else:
                phase, phase_col = "BURNING", parse_color("#ff453a")
            active_burn = max(0.0, data_t - (self._t_ign if self._t_ign is not None else self._t_lo))
        else:
            phase, phase_col = "IDLE", parse_color("#64748b")
            active_burn = 0.0

        avg_thrust = (impulse / active_burn) if active_burn > 0.02 else 0.0

        # Active portion of thrust curve
        frac_x = float(np.clip((data_t - self._t_lo) / (self._t_hi - self._t_lo), 0.0, 1.0))
        if self._active_curve is not None:
            cw = max(1, int(round(frac_x * self._active_curve.width)))
            crop = self._active_curve.crop((0, 0, cw, self._active_curve.height))
            composite_clipped(
                image, crop,
                int(round(self.rect.x + self._plot.x)),
                int(round(self.rect.y + self._plot.y)),
            )

        # Dynamic layer: phase badge + playhead + tip dot + nozzle plume
        dyn = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dyn)

        # Phase pill next to title
        tw_title = text_size(self.title_font, self.title.upper())[0]
        bw_t, bh_t = text_size(self.badge_font, phase)
        px_b = pad + tw_title + 14 * scale
        py_b = pad + 2 * scale
        rounded_rect(
            dd, (px_b, py_b, px_b + bw_t + 16 * scale, py_b + bh_t + 8 * scale),
            (bh_t + 8 * scale) / 2, fill=with_alpha(phase_col, 0.90),
        )
        dd.text((px_b + 8 * scale, py_b + 4 * scale), phase, font=self.badge_font,
                fill=(255, 255, 255, 255), anchor="la")

        # Playhead & glowing curve dot
        hx = self._plot.x + frac_x * self._plot.w
        hy = self._plot.y + (1.0 - float(np.clip(thrust, 0.0, self._y_hi)) / self._y_hi) * self._plot.h
        dd.line([(hx, self._plot.y), (hx, self._plot.y1)],
                fill=with_alpha(self.color_accent, 0.82), width=max(1, int(round(1.8 * scale))))
        r = 5.5 * scale
        dd.ellipse((hx - r * 1.6, hy - r * 1.6, hx + r * 1.6, hy + r * 1.6),
                   fill=with_alpha(self.color_accent, 0.35))
        dd.ellipse((hx - r, hy - r, hx + r, hy + r),
                   fill=self.color_accent, outline=(255, 255, 255, 240), width=max(1, int(round(scale))))

        if self.show_plume:
            self._draw_nozzle_plume(dyn, thrust / self._peak_val, frame)

        composite_clipped(image, dyn, int(round(self.rect.x)), int(round(self.rect.y)))

        # Header mission clock (top-right)
        sign = "-" if rel_t < 0 else "+"
        draw_text(
            image, (self.rect.x1 - pad, self.rect.y + pad + 1 * scale),
            f"T{sign}{abs(rel_t):.2f} s", self.clock_font,
            self.color("value_color", "text"), anchor="ra",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )

        # KPI values across the 4 columns
        kpi_val_y = self.rect.y + pad + hdr_h + 26 * scale
        kpi_sub_y = kpi_val_y + 40 * scale
        col_w = (w - 2 * pad) / 4.0
        txt_col = self.color("value_color", "text")
        dim_col = self.color("label_color", "text_dim")

        # 1) Live Thrust
        t_col = self.color_accent if thrust >= self._burn_thresh else txt_col
        draw_text(
            image, (self.rect.x + pad + 4 * scale, kpi_val_y - 4 * scale),
            f"{thrust:.1f} {self.unit}", self.kpi_big_font, t_col, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        if self.show_kgf:
            draw_text(
                image, (self.rect.x + pad + 4 * scale, kpi_sub_y + 2 * scale),
                f"{thrust / 9.80665:.2f} kgf", self.kpi_sub_font, dim_col, anchor="la", shadow=None,
            )

        # 2) Peak Thrust
        c1 = self.rect.x + pad + col_w + 4 * scale
        draw_text(
            image, (c1, kpi_val_y + 2 * scale),
            f"{peak_so_far:.1f} {self.unit}", self.kpi_val_font, txt_col, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        draw_text(
            image, (c1, kpi_sub_y + 2 * scale),
            f"{peak_so_far / 9.80665:.2f} kgf" if self.show_kgf else f"{(thrust / self._peak_val * 100):.0f}% of peak",
            self.kpi_sub_font, dim_col, anchor="la", shadow=None,
        )

        # 3) Total Impulse + NAR/TRA Motor Class
        c2 = self.rect.x + pad + col_w * 2 + 4 * scale
        draw_text(
            image, (c2, kpi_val_y + 2 * scale),
            f"{impulse:.1f} N·s", self.kpi_val_font, txt_col, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        if self.show_class:
            cls_name, cls_pct = nar_motor_class(impulse, avg_thrust)
            cls_txt = f"Class {cls_name} ({cls_pct}%)" if cls_pct > 0 else "Standby"
            draw_text(
                image, (c2, kpi_sub_y + 2 * scale),
                cls_txt, self.kpi_sub_font, self.color_accent if cls_pct > 0 else dim_col,
                anchor="la", shadow=None,
            )

        # 4) Burn time & average thrust
        c3 = self.rect.x + pad + col_w * 3 + 4 * scale
        draw_text(
            image, (c3, kpi_val_y + 2 * scale),
            f"{active_burn:.2f} s", self.kpi_val_font, txt_col, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        draw_text(
            image, (c3, kpi_sub_y + 2 * scale),
            f"Avg {avg_thrust:.1f} {self.unit}", self.kpi_sub_font, dim_col, anchor="la", shadow=None,
        )


# ---------------------------------------------------------------------------
# 2. True 3D Rocket Attitude / Tilt Visualizer
# ---------------------------------------------------------------------------

def _euler_to_rot(pitch_deg: float, roll_deg: float, yaw_deg: float) -> np.ndarray:
    """Build 3x3 rotation matrix from tilt/attitude angles (degrees) for a Z-up rocket.

    At ``pitch=0, roll=0, yaw=0``, the rocket points straight up along ``+Z``.
    - ``pitch`` tilts the rocket around the East/X axis (forward/aft tilt)
    - ``roll`` tilts the rocket around the North/Y axis (sideways tilt)
    - ``yaw`` spins the rocket around its longitudinal Z axis
    """
    p = math.radians(pitch_deg)
    r = math.radians(roll_deg)
    y = math.radians(yaw_deg)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    cy, sy = math.cos(y), math.sin(y)

    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=float)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]], dtype=float)
    ry = np.array([[cr, 0.0, sr], [0.0, 1.0, 0.0], [-sr, 0.0, cr]], dtype=float)
    return ry @ rx @ rz


def _quat_to_rot(w: float, x: float, y: float, z: float) -> np.ndarray:
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return np.eye(3, dtype=float)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=float)


def _build_rocket_mesh(
    body_color: RGBA, nose_color: RGBA, fin_color: RGBA, stripe_color: RGBA, fin_count: int = 4
) -> tuple[np.ndarray, list[tuple[list[int], RGBA]]]:
    """Generate a shaded 3D rocket mesh (nosecone + fuselage + roll-band + fins + nozzle)."""
    verts: list[list[float]] = []
    faces: list[tuple[list[int], RGBA]] = []
    segs = 14
    r_body = 0.155
    z_tail = -0.82
    z_shoulder = 0.42
    z_tip = 0.98
    z_noz = -0.96

    def add_ring(z: float, rad: float) -> list[int]:
        idxs = []
        for i in range(segs):
            ang = (2.0 * math.pi * i) / segs
            idxs.append(len(verts))
            verts.append([rad * math.cos(ang), rad * math.sin(ang), z])
        return idxs

    # Fuselage rings (split into upper + lower + roll stripe band)
    r_tail = add_ring(z_tail, r_body)
    r_band0 = add_ring(-0.18, r_body)
    r_band1 = add_ring(0.08, r_body)
    r_shld = add_ring(z_shoulder, r_body)

    # Ogive nosecone (3 rings + apex)
    r_n1 = add_ring(0.62, r_body * 0.78)
    r_n2 = add_ring(0.81, r_body * 0.42)
    tip_idx = len(verts)
    verts.append([0.0, 0.0, z_tip])

    # Nozzle bell
    r_noz_top = add_ring(z_tail, r_body * 0.52)
    r_noz_bot = add_ring(z_noz, r_body * 0.78)
    noz_col: RGBA = (78, 88, 104, 255)

    def stitch(ring_a: list[int], ring_b: list[int], col: RGBA, stripe_idx: set[int] | None = None) -> None:
        for i in range(segs):
            j = (i + 1) % segs
            c = stripe_color if (stripe_idx and i in stripe_idx) else col
            faces.append(([ring_a[i], ring_a[j], ring_b[j], ring_b[i]], c))

    # Add an unmistakable vertical roll-reference stripe on 2 segments of body
    stitch(r_tail, r_band0, body_color, stripe_idx={0, 1})
    stitch(r_band0, r_band1, (36, 44, 58, 255), stripe_idx={0, 1})
    stitch(r_band1, r_shld, body_color, stripe_idx={0, 1})
    stitch(r_shld, r_n1, nose_color)
    stitch(r_n1, r_n2, nose_color)
    for i in range(segs):
        j = (i + 1) % segs
        faces.append(([r_n2[i], r_n2[j], tip_idx], nose_color))
    stitch(r_noz_bot, r_noz_top, noz_col)

    # Swept trapezoidal fins (fin #0 painted nose_color so roll angle is obvious!)
    n_fins = max(3, min(6, int(fin_count)))
    for f in range(n_fins):
        ang = (2.0 * math.pi * f) / n_fins
        ca, sa = math.cos(ang), math.sin(ang)
        c_fin = nose_color if f == 0 else fin_color
        # Slight thickness offset along tangent for double-sided visibility
        tx, ty = -sa * 0.012, ca * 0.012
        r_in = r_body * 0.96
        r_out = r_body * 2.55
        pts = [
            [r_in * ca, r_in * sa, z_tail + 0.36],
            [r_out * ca, r_out * sa, z_tail + 0.06],
            [r_out * ca, r_out * sa, z_tail - 0.04],
            [r_in * ca, r_in * sa, z_tail],
        ]
        # Front face & back face so lighting works from both sides
        base_f = len(verts)
        for p in pts:
            verts.append([p[0] + tx, p[1] + ty, p[2]])
        faces.append(([base_f, base_f + 1, base_f + 2, base_f + 3], c_fin))
        base_b = len(verts)
        for p in pts:
            verts.append([p[0] - tx, p[1] - ty, p[2]])
        faces.append(([base_b + 3, base_b + 2, base_b + 1, base_b], c_fin))

    return np.asarray(verts, dtype=float), faces


@register("rocket_3d", "rocket_attitude", "attitude_3d", "orientation_3d")
class Rocket3DWidget(Widget):
    """True 3D projected rocket tilt & orientation indicator with horizon grid and live flame."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        # Support either quaternion channels or Euler pitch/roll/(yaw)
        q_spec = spec.get("quat_channels")
        self.quat_keys: tuple[str, str, str, str] | None = None
        if isinstance(q_spec, (list, tuple)) and len(q_spec) == 4:
            self.quat_keys = tuple(self.resolve_channel(c, "rocket_3d quat") for c in q_spec)  # type: ignore[assignment]
        else:
            auto_q = [
                self.resolve_optional_channel(c)
                for c in (["quat_w", "qw", "q0"], ["quat_x", "qx", "q1"],
                          ["quat_y", "qy", "q2"], ["quat_z", "qz", "q3"])
            ]
            if all(auto_q):
                self.quat_keys = tuple(auto_q)  # type: ignore[assignment]

        self.pitch_key = self.resolve_optional_channel(
            spec.get("pitch_channel", ["pitch", "pitch_deg", "tilt_x", "attitude_pitch"])
        )
        self.roll_key = self.resolve_optional_channel(
            spec.get("roll_channel", ["roll", "roll_deg", "tilt_y", "attitude_roll", "lean_angle"])
        )
        self.yaw_key = self.resolve_optional_channel(
            spec.get("yaw_channel", ["yaw", "yaw_deg", "heading", "bearing"])
        )
        if not self.quat_keys and not self.pitch_key and not self.roll_key:
            from .base import ChannelNotFound
            raise ChannelNotFound(
                "rocket_3d needs pitch/roll channels (or quat_channels [qw,qx,qy,qz]); "
                f"available: {', '.join(sorted(ctx.telemetry.channels))}"
            )
        self.thrust_key = self.resolve_optional_channel(
            spec.get("thrust_channel", ["thrust", "thrust_above_rest", "force"])
        )
        self.title = str(spec.get("title", "3D ATTITUDE"))
        self.camera_elev = float(spec.get("camera_elev", 20.0))
        self.camera_azim = float(spec.get("camera_azim", -32.0))
        self.show_axes = bool(spec.get("show_axes", True))
        self.show_values = bool(spec.get("show_values", True))
        self.fin_count = int(spec.get("fin_count", 4))
        self.padding = float(spec.get("padding", 12.0))

        self.body_color = parse_color(spec.get("body_color", "#e2e8f0"))
        self.nose_color = parse_color(spec.get("nose_color", self.theme.accent))
        self.fin_color = parse_color(spec.get("fin_color", "#94a3b8"))
        self.stripe_color = parse_color(spec.get("stripe_color", "#ff453a"))

        self.title_font = ctx.font(str(spec.get("title_font", self.theme.font_bold)), 14)
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), 16)
        self.tilt_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), 21)
        self.axis_font = ctx.font(str(spec.get("font", self.theme.font_mono)), 13)

        self._static: Image.Image | None = None
        self._verts: np.ndarray = np.zeros((0, 3))
        self._faces: list[tuple[list[int], RGBA]] = []
        self._cam_rot: np.ndarray = np.eye(3)
        self._light_dir: np.ndarray = np.array([-0.45, -0.55, 0.70], dtype=float)
        self._light_dir /= np.linalg.norm(self._light_dir)
        self._thrust_max = 1.0
        self._cx = 0.0
        self._cy = 0.0
        self._focal = 100.0

    def default_width(self, ctx: RenderContext) -> float:
        return 300.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 340.0 * ctx.scale

    def _project(self, pts_world: np.ndarray, ss: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Project (N, 3) world Z-up points to 2D screen (sx, sy, cam_depth)."""
        # Camera looks toward origin from distance D along rotated -Y
        cam = pts_world @ self._cam_rot.T
        cam_dist = 3.8
        depth = cam_dist - cam[:, 1]
        safe_d = np.maximum(0.4, depth)
        scale_f = (self._focal * ss) / safe_d
        sx = self._cx * ss + cam[:, 0] * scale_f
        sy = self._cy * ss - cam[:, 2] * scale_f
        return sx, sy, depth

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        top_h = 28 * scale if self.title else 4 * scale
        bot_h = 46 * scale if self.show_values else 8 * scale
        avail_w = max(40.0, w - 2 * pad)
        avail_h = max(40.0, h - pad - top_h - bot_h)
        self._cx = w / 2.0
        self._cy = pad + top_h + avail_h * 0.45
        self._focal = min(avail_w, avail_h) * 1.24

        # Camera view matrix (azimuth around Z, elevation around X)
        az = math.radians(self.camera_azim)
        el = math.radians(self.camera_elev)
        rz = np.array([[math.cos(az), -math.sin(az), 0.0],
                       [math.sin(az), math.cos(az), 0.0],
                       [0.0, 0.0, 1.0]], dtype=float)
        rx = np.array([[1.0, 0.0, 0.0],
                       [0.0, math.cos(el), math.sin(el)],
                       [0.0, -math.sin(el), math.cos(el)]], dtype=float)
        self._cam_rot = rx @ rz

        self._verts, self._faces = _build_rocket_mesh(
            self.body_color, self.nose_color, self.fin_color, self.stripe_color, self.fin_count
        )
        if self.thrust_key and self.thrust_key in self.ctx.telemetry:
            fin = self.ctx.telemetry[self.thrust_key].finite
            if fin.size and float(fin.max()) > 0:
                self._thrust_max = float(fin.max())

        # Pre-render static backdrop: panel + title + 3D horizon ellipse & zenith axis
        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        if self.title:
            draw_text(static, (pad, pad * 0.7), self.title.upper(), self.title_font,
                      self.color("label_color", "text_dim"), anchor="la", shadow=None)

        if self.show_axes:
            ss = 2
            grid = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
            gd = ImageDraw.Draw(grid)
            ring_col = parse_color(self.spec.get("grid_color", self.theme.grid))
            axis_col = parse_color(self.spec.get("axis_color", self.theme.axis))

            # Ground horizon ring at z = 0 (and inner 0.55 ring)
            thetas = np.linspace(0.0, 2.0 * math.pi, 65)
            for rad, col in ((0.95, axis_col), (0.52, ring_col)):
                ring_pts = np.column_stack([rad * np.cos(thetas), rad * np.sin(thetas), np.zeros_like(thetas)])
                rx_s, ry_s, _ = self._project(ring_pts, ss)
                gd.line(list(zip(rx_s.tolist(), ry_s.tolist())),
                        fill=col, width=max(1, int(round(1.2 * scale * ss))))

            # Cardinal horizon crosshairs (N-S, E-W)
            for p0, p1 in (([-0.95, 0, 0], [0.95, 0, 0]), ([0, -0.95, 0], [0, 0.95, 0])):
                ax_pts = np.array([p0, p1], dtype=float)
                ax_x, ax_y, _ = self._project(ax_pts, ss)
                gd.line([(ax_x[0], ax_y[0]), (ax_x[1], ax_y[1])],
                        fill=ring_col, width=max(1, int(round(1.0 * scale * ss))))

            # Vertical Zenith reference axis (dashed line from z=0 to z=1.18)
            z_pts = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.18]], dtype=float)
            zx, zy, _ = self._project(z_pts, ss)
            gd.line([(zx[0], zy[0]), (zx[1], zy[1])],
                    fill=with_alpha(axis_col, 0.7), width=max(1, int(round(1.4 * scale * ss))))
            static.alpha_composite(grid.reduce(ss))

            # Compass & UP labels on 1x static layer
            dim = self.color("label_color", "text_dim")
            lbl_pts = np.array([
                [0.0, 1.08, 0.0],   # N
                [1.08, 0.0, 0.0],   # E
                [0.0, 0.0, 1.24],   # UP
            ], dtype=float)
            lx, ly, _ = self._project(lbl_pts, 1)
            for idx, txt in enumerate(("N", "E", "UP")):
                draw_text(static, (float(lx[idx]), float(ly[idx])), txt,
                          self.axis_font, dim, anchor="mm", shadow=None)

        self._static = static

    def _orientation_at(self, data_t: float) -> tuple[np.ndarray, float, float, float, float]:
        """Return ``(rot_matrix_3x3, tilt_deg, pitch_deg, roll_deg, yaw_deg)``."""
        if self.quat_keys is not None:
            qw, qx, qy, qz = (self.ctx.value_at(k, data_t) for k in self.quat_keys)
            if all(math.isfinite(v) for v in (qw, qx, qy, qz)):
                rot = _quat_to_rot(qw, qx, qy, qz)
                nose_z = float(np.clip(rot[2, 2], -1.0, 1.0))
                tilt = math.degrees(math.acos(nose_z))
                pitch = math.degrees(math.asin(float(np.clip(-rot[2, 0], -1.0, 1.0))))
                roll = math.degrees(math.atan2(rot[2, 1], rot[2, 2]))
                yaw = (math.degrees(math.atan2(rot[1, 0], rot[0, 0])) + 360.0) % 360.0
                return rot, tilt, pitch, roll, yaw

        pitch = self.ctx.value_at(self.pitch_key, data_t) if self.pitch_key else 0.0
        roll = self.ctx.value_at(self.roll_key, data_t) if self.roll_key else 0.0
        yaw = self.ctx.value_at(self.yaw_key, data_t) if self.yaw_key else 0.0
        if not math.isfinite(pitch):
            pitch = 0.0
        if not math.isfinite(roll):
            roll = 0.0
        if not math.isfinite(yaw):
            yaw = 0.0
        rot = _euler_to_rot(pitch, roll, yaw)
        nose_z = float(np.clip(rot[2, 2], -1.0, 1.0))
        tilt = math.degrees(math.acos(nose_z))
        return rot, tilt, pitch, roll, yaw % 360.0

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))

        scale = self.ctx.scale
        w, h = self.rect.size()
        rot, tilt, pitch, roll, yaw = self._orientation_at(data_t)

        # Optional 3D thrust plume vertices appended dynamically when burning
        thrust_frac = 0.0
        if self.thrust_key:
            tv = self.ctx.value_at(self.thrust_key, data_t)
            if math.isfinite(tv) and tv > self._thrust_max * 0.03:
                thrust_frac = float(np.clip(tv / self._thrust_max, 0.0, 1.0))

        verts_local = self._verts
        faces = self._faces
        if thrust_frac > 0.03:
            # Append a lightweight 6-sided glowing 3D flame cone below nozzle
            flicker = 1.0 + 0.06 * math.sin(frame * 2.1)
            f_len = (0.28 + 0.65 * thrust_frac) * flicker
            f_rad = 0.095 * (0.65 + 0.45 * thrust_frac)
            base_idx = verts_local.shape[0]
            f_verts = [[f_rad * math.cos(2 * math.pi * i / 6), f_rad * math.sin(2 * math.pi * i / 6), -0.96] for i in range(6)]
            f_verts.append([0.0, 0.0, -0.96 - f_len])
            verts_local = np.vstack([verts_local, np.asarray(f_verts, dtype=float)])
            faces = list(self._faces)
            f_col: RGBA = (255, 165, 30, 230)
            for i in range(6):
                j = (i + 1) % 6
                faces.append(([base_idx + i, base_idx + j, base_idx + 6], f_col))

        world_pts = verts_local @ rot.T
        ss = 2
        sx, sy, depth = self._project(world_pts, ss)

        layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        # Tilt angle arc from Zenith (0,0,1) to rocket nose vector (rot @ [0,0,1])
        nose_dir = rot[:, 2]
        if self.show_axes and tilt > 1.0:
            arc_r = 1.06
            up_v = np.array([0.0, 0.0, 1.0])
            ts_arc = np.linspace(0.0, 1.0, 16)
            arc_pts = []
            for ta in ts_arc:
                v = (1.0 - ta) * up_v + ta * nose_dir
                vn = np.linalg.norm(v)
                if vn > 1e-6:
                    arc_pts.append((v / vn) * arc_r)
            if len(arc_pts) >= 2:
                ax, ay, _ = self._project(np.asarray(arc_pts), ss)
                acc = self.color("color", "accent")
                draw.line(list(zip(ax.tolist(), ay.tolist())),
                          fill=with_alpha(acc, 0.88), width=max(2, int(round(2.4 * scale * ss))))

        # Painter's algorithm: sort polygons back-to-front by average camera depth
        draw_list: list[tuple[float, list[tuple[float, float]], RGBA]] = []
        for idxs, base_col in faces:
            d_mean = float(depth[idxs].mean())
            p0, p1, p2 = world_pts[idxs[0]], world_pts[idxs[1]], world_pts[idxs[2]]
            normal = np.cross(p1 - p0, p2 - p0)
            n_len = float(np.linalg.norm(normal))
            if n_len > 1e-9:
                normal /= n_len
                # Diffuse Lambertian lighting (absolute dot so thin fins are lit nicely)
                ndotl = float(np.dot(normal, self._light_dir))
                lit = 0.42 + 0.58 * max(0.0, ndotl if len(idxs) != 4 or abs(ndotl) < 0.99 else abs(ndotl))
                if ndotl < 0:
                    lit = max(lit, 0.36 + 0.24 * abs(ndotl))
            else:
                lit = 0.75
            # Emissive flame stays full brightness
            if base_col[0] == 255 and base_col[1] == 165 and base_col[2] == 30:
                shaded = base_col
            else:
                shaded = (
                    min(255, int(round(base_col[0] * lit))),
                    min(255, int(round(base_col[1] * lit))),
                    min(255, int(round(base_col[2] * lit))),
                    base_col[3],
                )
            poly = [(float(sx[i]), float(sy[i])) for i in idxs]
            draw_list.append((d_mean, poly, shaded))

        # Larger camera depth = further away -> draw first
        draw_list.sort(key=lambda item: item[0], reverse=True)
        for _, poly, col in draw_list:
            draw.polygon(poly, fill=col)

        # Subtle nose-axis ray extending beyond tip
        if self.show_axes:
            ray = np.array([nose_dir * 0.98, nose_dir * 1.18], dtype=float)
            rx_s, ry_s, _ = self._project(ray, ss)
            draw.line([(rx_s[0], ry_s[0]), (rx_s[1], ry_s[1])],
                      fill=self.color("color", "accent"), width=max(2, int(round(1.8 * scale * ss))))

        composite_clipped(image, layer.reduce(ss), int(round(self.rect.x)), int(round(self.rect.y)))

        if self.show_values:
            pad = self.padding * scale
            shadow, offset, blur = self.text_shadow()
            tilt_col = (
                parse_color("#ef4444") if tilt > 30.0
                else (parse_color("#facc15") if tilt > 15.0 else self.color("value_color", "text"))
            )
            draw_text(
                image, (self.rect.x1 - pad, self.rect.y + pad * 0.55),
                f"TILT {tilt:.1f}°", self.tilt_font, tilt_col, anchor="ra",
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
            draw_text(
                image, (self.rect.cx, self.rect.y1 - pad * 0.8),
                f"P {pitch:+.1f}°   R {roll:+.1f}°   Y {yaw:.0f}°",
                self.val_font, self.color("label_color", "text_dim"),
                anchor="md", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
