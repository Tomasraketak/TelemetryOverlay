"""Spatial, GPS, aviation & motorsport widgets.

Includes:
- ``map`` (GPS track with speed/altitude heat-colouring, full & follow modes, scale bar)
- ``gforce`` (2D G-force friction circle with fading trail, concentric rings & peak marker)
- ``attitude`` (aviation artificial horizon with pitch ladder & bank arc)
- ``compass`` (aviation heading tape OR circular compass rose)
- ``tape`` (scrolling vertical/horizontal aviation/rocket ladder tape with chevron readout)
- ``lean`` (motorcycle / aviation lean & bank angle arc with L/R peak hold)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from ..formats.enrichment import haversine_meters
from ..theme import (
    RGBA, Rect, colormap_color, composite_clipped, draw_text, parse_color,
    rounded_rect, text_size, with_alpha,
)
from .base import (
    RenderContext, Widget, format_value, parse_zones, peak_value, register,
    zone_color,
)

__all__ = [
    "MapWidget", "GForceWidget", "AttitudeWidget",
    "CompassWidget", "TapeWidget", "LeanWidget",
]


def _nice_scale_meters(raw_m: float) -> tuple[float, str]:
    """Pick a clean human scale bar length in m or km."""
    for cand in (5, 10, 20, 25, 50, 100, 200, 250, 500,
                 1000, 2000, 5000, 10000, 20000, 50000, 100000):
        if cand >= raw_m * 0.7:
            label = f"{cand / 1000:g} km" if cand >= 1000 else f"{cand:g} m"
            return float(cand), label
    return float(raw_m), f"{raw_m / 1000:.0f} km"


# ---------------------------------------------------------------------------
# 1. GPS Track / Route Map
# ---------------------------------------------------------------------------

@register("map", "gps_track", "route")
class MapWidget(Widget):
    """Animated GPS track map with optional metric heat-colouring and follow zoom."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.lat_key = self.resolve_channel(
            spec.get("lat_channel", ["lat", "latitude", "gps_lat"]), "map lat"
        )
        self.lon_key = self.resolve_channel(
            spec.get("lon_channel", ["lon", "lng", "longitude", "gps_lon"]), "map lon"
        )
        self.color_channel = self.resolve_optional_channel(
            spec.get("color_channel", ["speed_kmh", "speed", "altitude"])
        ) if spec.get("color_channel", True) is not False else None
        self.heading_key = self.resolve_optional_channel(
            spec.get("heading_channel", ["bearing", "heading", "yaw"])
        )
        self.colormap = str(spec.get("colormap", "speed")).lower()
        self.mode = str(spec.get("mode", "full")).lower()  # full | follow
        self.follow_radius_m = float(spec.get("follow_radius", 250.0))
        self.line_width = float(spec.get("line_width", 5.0))
        self.outline_width = float(spec.get("outline_width", 2.0))
        self.dot_size = float(spec.get("dot_size", 8.0))
        self.padding = float(spec.get("padding", 20.0))
        self.ghost_opacity = float(spec.get("ghost_opacity", 0.30))
        self.show_trail = bool(spec.get("trail", True))
        self.show_start_finish = bool(spec.get("start_finish", True))
        self.show_scale_bar = bool(spec.get("scale_bar", True))
        self.show_north = bool(spec.get("north_arrow", True))
        self.show_legend = bool(spec.get("legend", bool(self.color_channel)))
        self.title = str(spec.get("title", ""))
        self.title_font = ctx.font(str(spec.get("title_font", self.theme.font_bold)),
                                   float(spec.get("title_size", 20)))
        self.small_font = ctx.font(str(spec.get("font", self.theme.font)),
                                   float(spec.get("label_size", 15)))

        self._static: Image.Image | None = None
        self._t: np.ndarray = np.zeros(0)
        self._mx: np.ndarray = np.zeros(0)  # local metres east
        self._my: np.ndarray = np.zeros(0)  # local metres north
        self._px: np.ndarray = np.zeros(0)  # widget-local pixel x (full mode)
        self._py: np.ndarray = np.zeros(0)  # widget-local pixel y (full mode)
        self._cvals: np.ndarray | None = None
        self._c_lo = 0.0
        self._c_hi = 1.0
        self._m_per_px = 1.0
        self._lat0 = 0.0
        self._lon0 = 0.0
        self._cos_lat = 1.0

    def default_width(self, ctx: RenderContext) -> float:
        return 320.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 320.0 * ctx.scale

    def _project_meters(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        deg_m = 111_320.0
        mx = (lon - self._lon0) * deg_m * self._cos_lat
        my = (lat - self._lat0) * deg_m
        return mx, my

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        tel = self.ctx.telemetry
        lat = tel[self.lat_key].values
        lon = tel[self.lon_key].values
        valid = np.isfinite(lat) & np.isfinite(lon)
        if valid.sum() < 2:
            self._static = self.build_panel_layer(w, h)
            return

        self._t = tel.time[valid]
        lat_v = lat[valid]
        lon_v = lon[valid]
        self._lat0 = float(np.mean(lat_v))
        self._lon0 = float(np.mean(lon_v))
        self._cos_lat = max(0.05, math.cos(math.radians(self._lat0)))
        self._mx, self._my = self._project_meters(lat_v, lon_v)

        if self.color_channel and self.color_channel in tel:
            cv = tel[self.color_channel].values[valid]
            fin = cv[np.isfinite(cv)]
            if fin.size:
                self._cvals = cv
                self._c_lo = float(np.percentile(fin, 2))
                self._c_hi = float(np.percentile(fin, 98))
                if self._c_hi <= self._c_lo:
                    self._c_hi = self._c_lo + 1.0

        pad = self.padding * scale
        top_pad = pad + (text_size(self.title_font, "Xg")[1] * 1.4 if self.title else 0.0)
        bot_pad = pad + (24 * scale if (self.show_scale_bar or self.show_legend) else 0.0)
        avail_w = max(16.0, w - 2 * pad)
        avail_h = max(16.0, h - top_pad - bot_pad)

        x_lo, x_hi = float(self._mx.min()), float(self._mx.max())
        y_lo, y_hi = float(self._my.min()), float(self._my.max())
        span_x = max(5.0, x_hi - x_lo)
        span_y = max(5.0, y_hi - y_lo)
        px_per_m = min(avail_w / span_x, avail_h / span_y)
        self._m_per_px = 1.0 / max(px_per_m, 1e-6)

        cx = pad + avail_w / 2.0
        cy = top_pad + avail_h / 2.0
        mid_x = (x_lo + x_hi) / 2.0
        mid_y = (y_lo + y_hi) / 2.0
        self._px = cx + (self._mx - mid_x) * px_per_m
        self._py = cy - (self._my - mid_y) * px_per_m  # north is up

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        if self.mode == "full":
            # Pre-render the full track (ghost or heat-coloured) at 2x supersampling
            ss = 2
            trk = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
            td = ImageDraw.Draw(trk)
            # Decimate to <= 1200 vertices for snappy drawing
            step = max(1, self._px.size // 1200)
            pts_x = self._px[::step] * ss
            pts_y = self._py[::step] * ss
            pts = list(zip(pts_x.tolist(), pts_y.tolist()))
            lw = max(2, int(round(self.line_width * scale * ss)))
            ow = lw + max(2, int(round(self.outline_width * scale * ss * 2)))

            # Dark subtle outline for readability over bright video
            if len(pts) >= 2:
                td.line(pts, fill=(0, 0, 0, 120), width=ow, joint="curve")

            if self._cvals is not None and not self.show_trail:
                cv_sub = self._cvals[::step]
                for i in range(len(pts) - 1):
                    v = float(cv_sub[i])
                    frac = (v - self._c_lo) / (self._c_hi - self._c_lo) if math.isfinite(v) else 0.5
                    col = colormap_color(frac, self.colormap, 0.92)
                    td.line([pts[i], pts[i + 1]], fill=col, width=lw)
            else:
                ghost_col = with_alpha(
                    self.color("track_color", "text"), self.ghost_opacity if self.show_trail else 0.85
                )
                if len(pts) >= 2:
                    td.line(pts, fill=ghost_col, width=lw, joint="curve")

            if self.show_start_finish and len(pts) >= 2:
                r = 4.5 * scale * ss
                sx, sy = pts[0]
                fx, fy = pts[-1]
                td.ellipse((sx - r, sy - r, sx + r, sy + r), fill=parse_color("#22c55e"),
                           outline=(255, 255, 255, 230), width=max(1, ss))
                td.ellipse((fx - r, fy - r, fx + r, fy + r), fill=parse_color("#ef4444"),
                           outline=(255, 255, 255, 230), width=max(1, ss))

            static.alpha_composite(trk.reduce(ss))

        # Title, North indicator, scale bar & colorbar legend
        if self.title:
            draw_text(
                static, (pad, pad * 0.5), self.title.upper(), self.title_font,
                self.color("title_color", "text"), anchor="la", shadow=None,
            )

        if self.show_north:
            nx = w - pad - 10 * scale
            ny = pad + 14 * scale
            nd = ImageDraw.Draw(static)
            ar = 7 * scale
            nd.polygon([(nx, ny - ar), (nx - ar * 0.55, ny + ar * 0.6), (nx, ny + ar * 0.2),
                        (nx + ar * 0.55, ny + ar * 0.6)], fill=self.color("accent", "accent"))
            draw_text(static, (nx, ny - ar - 7 * scale), "N", self.small_font,
                      self.color("label_color", "text_dim"), anchor="mm", shadow=None)

        if self.show_scale_bar and self.mode == "full":
            target_px = min(90.0 * scale, w * 0.28)
            bar_m, bar_lbl = _nice_scale_meters(target_px * self._m_per_px)
            bar_px = bar_m / self._m_per_px
            bx0 = pad
            by = h - pad * 0.65
            sd = ImageDraw.Draw(static)
            scol = self.color("label_color", "text_dim")
            sw = max(1, int(round(1.5 * scale)))
            sd.line([(bx0, by), (bx0 + bar_px, by)], fill=scol, width=sw)
            sd.line([(bx0, by - 4 * scale), (bx0, by + 1)], fill=scol, width=sw)
            sd.line([(bx0 + bar_px, by - 4 * scale), (bx0 + bar_px, by + 1)], fill=scol, width=sw)
            draw_text(static, (bx0 + bar_px / 2, by - 5 * scale), bar_lbl,
                      self.small_font, scol, anchor="md", shadow=None)

        if self.show_legend and self._cvals is not None and self.color_channel:
            lw_bar = min(118.0 * scale, w * 0.44)
            lh_bar = max(4.0, 6.0 * scale)
            lx1 = w - pad
            lx0 = lx1 - lw_bar
            ly = h - pad * 0.65 - lh_bar
            ld = ImageDraw.Draw(static)
            steps = max(12, int(lw_bar // 2))
            for i in range(steps):
                f0, f1 = i / steps, (i + 1) / steps
                col = colormap_color((f0 + f1) * 0.5, self.colormap, 0.95)
                ld.rectangle((lx0 + f0 * lw_bar, ly, lx0 + f1 * lw_bar + 1, ly + lh_bar), fill=col)
            unit = tel[self.color_channel].unit
            scol = self.color("label_color", "text_dim")
            draw_text(static, (lx0, ly - 3 * scale), f"{self._c_lo:.0f}",
                      self.small_font, scol, anchor="ld", shadow=None)
            draw_text(static, (lx1, ly - 3 * scale), f"{self._c_hi:.0f} {unit}".strip(),
                      self.small_font, scol, anchor="rd", shadow=None)

        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        if self._t.size < 2:
            return

        scale = self.ctx.scale
        w, h = self.rect.size()
        idx = int(np.clip(np.searchsorted(self._t, data_t), 0, self._t.size - 1))

        # Interpolate current position smoothly
        cur_mx = float(np.interp(data_t, self._t, self._mx))
        cur_my = float(np.interp(data_t, self._t, self._my))

        dyn = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(dyn)
        lw = max(2, int(round(self.line_width * scale)))

        if self.mode == "follow":
            # Zoomed local window centered on current vehicle position
            px_per_m = min(w, h) / max(20.0, self.follow_radius_m * 2.0)
            cx, cy = w / 2.0, h / 2.0
            sx = cx + (self._mx - cur_mx) * px_per_m
            sy = cy - (self._my - cur_my) * px_per_m
            # Draw full context in ghost, completed trail coloured
            step = max(1, sx.size // 800)
            pts_all = list(zip(sx[::step].tolist(), sy[::step].tolist()))
            if len(pts_all) >= 2:
                draw.line(pts_all, fill=with_alpha(self.color("track_color", "text"), self.ghost_opacity),
                          width=lw, joint="curve")
            if idx >= 1:
                trail_x = sx[:idx + 1]
                trail_y = sy[:idx + 1]
                tstep = max(1, trail_x.size // 500)
                tpts = list(zip(trail_x[::tstep].tolist(), trail_y[::tstep].tolist()))
                tpts.append((cx, cy))
                if len(tpts) >= 2:
                    draw.line(tpts, fill=self.color("color", "accent"), width=lw, joint="curve")
            dot_x, dot_y = cx, cy
        else:
            dot_x = float(np.interp(data_t, self._t, self._px))
            dot_y = float(np.interp(data_t, self._t, self._py))
            if self.show_trail and idx >= 1:
                step = max(1, idx // 400)
                tx = self._px[:idx + 1:step]
                ty = self._py[:idx + 1:step]
                if self._cvals is not None:
                    cv = self._cvals[:idx + 1:step]
                    for i in range(tx.size - 1):
                        v = float(cv[i])
                        frac = (v - self._c_lo) / (self._c_hi - self._c_lo) if math.isfinite(v) else 0.5
                        col = colormap_color(frac, self.colormap, 0.95)
                        draw.line([(float(tx[i]), float(ty[i])), (float(tx[i + 1]), float(ty[i + 1]))],
                                  fill=col, width=lw)
                    if tx.size:
                        v = float(cv[-1])
                        frac = (v - self._c_lo) / (self._c_hi - self._c_lo) if math.isfinite(v) else 0.5
                        draw.line([(float(tx[-1]), float(ty[-1])), (dot_x, dot_y)],
                                  fill=colormap_color(frac, self.colormap, 0.95), width=lw)
                else:
                    pts = list(zip(tx.tolist(), ty.tolist())) + [(dot_x, dot_y)]
                    if len(pts) >= 2:
                        draw.line(pts, fill=self.color("color", "accent"), width=lw, joint="curve")

        # Live position marker (directional arrow if heading available, else crisp dot)
        r = self.dot_size * scale
        dot_col = self.color("dot_color", "accent")
        draw.ellipse((dot_x - r * 1.5, dot_y - r * 1.5, dot_x + r * 1.5, dot_y + r * 1.5),
                     fill=with_alpha(dot_col, 0.32))
        if self.heading_key:
            brg = self.ctx.value_at(self.heading_key, data_t)
            if math.isfinite(brg):
                rad = math.radians(brg - 90.0)
                ca, sa = math.cos(rad), math.sin(rad)
                pa, pb = -sa, ca
                tip = (dot_x + r * 1.5 * ca, dot_y + r * 1.5 * sa)
                left = (dot_x - r * 0.9 * ca + r * 0.85 * pa, dot_y - r * 0.9 * sa + r * 0.85 * pb)
                back = (dot_x - r * 0.35 * ca, dot_y - r * 0.35 * sa)
                right = (dot_x - r * 0.9 * ca - r * 0.85 * pa, dot_y - r * 0.9 * sa - r * 0.85 * pb)
                draw.polygon([tip, left, back, right], fill=dot_col, outline=(255, 255, 255, 240))
            else:
                draw.ellipse((dot_x - r, dot_y - r, dot_x + r, dot_y + r),
                             fill=dot_col, outline=(255, 255, 255, 240), width=max(1, int(round(1.5 * scale))))
        else:
            draw.ellipse((dot_x - r, dot_y - r, dot_x + r, dot_y + r),
                         fill=dot_col, outline=(255, 255, 255, 240), width=max(1, int(round(1.5 * scale))))

        composite_clipped(image, dyn, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 2. 2D G-Force Friction Circle
# ---------------------------------------------------------------------------

@register("gforce", "g_ball", "accel")
class GForceWidget(Widget):
    """2D G-force friction circle with concentric rings, fading trail and peak G."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.x_key = self.resolve_channel(
            spec.get("x_channel", ["g_lat", "accel_y", "lat_g"]), "gforce x"
        )
        self.y_key = self.resolve_channel(
            spec.get("y_channel", ["g_lon", "accel_x", "lon_g"]), "gforce y"
        )
        # Auto-detect whether inputs are in m/s^2 vs G
        ux = ctx.telemetry[self.x_key].unit.lower()
        uy = ctx.telemetry[self.y_key].unit.lower()
        self.x_div = float(spec.get("x_divisor", 9.80665 if "m/s" in ux else 1.0))
        self.y_div = float(spec.get("y_divisor", 9.80665 if "m/s" in uy else 1.0))
        self.max_g = float(spec.get("max_g", 1.5))
        self.rings = int(spec.get("rings", 3))
        self.trail_seconds = float(spec.get("trail", 1.2))
        self.trail_steps = int(spec.get("trail_steps", 24))
        self.show_values = bool(spec.get("show_values", True))
        self.show_peak = bool(spec.get("peak_hold", True))
        self.padding = float(spec.get("padding", 14.0))
        self.label = str(spec.get("label", "G-FORCE"))
        self.label_font = ctx.font(str(spec.get("font", self.theme.font)), float(spec.get("label_size", 16)))
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), float(spec.get("value_size", 22)))
        self._static: Image.Image | None = None
        self._cx = 0.0
        self._cy = 0.0
        self._radius = 10.0

    def default_width(self, ctx: RenderContext) -> float:
        return 240.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 260.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        pad = self.padding * scale
        val_space = (text_size(self.value_font, "1.0G")[1] * 1.6 if self.show_values else 0.0)
        top_space = (text_size(self.label_font, "G")[1] * 1.3 if self.label else 0.0)
        avail_w = max(20.0, w - 2 * pad)
        avail_h = max(20.0, h - 2 * pad - val_space - top_space)
        self._radius = min(avail_w, avail_h) / 2.0
        self._cx = w / 2.0
        self._cy = pad + top_space + avail_h / 2.0

        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        ss = 2
        grid = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grid)
        cx, cy, r = self._cx * ss, self._cy * ss, self._radius * ss
        ring_col = parse_color(self.spec.get("grid_color", self.theme.grid))
        axis_col = parse_color(self.spec.get("axis_color", self.theme.axis))
        lw = max(1, int(round(1.2 * scale * ss)))

        # Subtle filled circle backdrop
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 55))
        for i in range(1, self.rings + 1):
            ri = r * (i / self.rings)
            col = axis_col if i == self.rings else ring_col
            gd.ellipse((cx - ri, cy - ri, cx + ri, cy + ri), outline=col, width=lw)
        gd.line([(cx - r, cy), (cx + r, cy)], fill=ring_col, width=lw)
        gd.line([(cx, cy - r), (cx, cy + r)], fill=ring_col, width=lw)
        static.alpha_composite(grid.reduce(ss))

        # Ring labels
        sfont = self.ctx.font(self.theme.font, 13)
        dim = self.color("label_color", "text_dim")
        for i in range(1, self.rings + 1):
            g_val = self.max_g * (i / self.rings)
            ry = self._cy - self._radius * (i / self.rings)
            draw_text(static, (self._cx + 4 * scale, ry + 2 * scale), f"{g_val:.1f}G",
                      sfont, dim, anchor="la", shadow=None)

        if self.label:
            draw_text(static, (w / 2.0, pad * 0.6), self.label.upper(),
                      self.label_font, dim, anchor="ma", shadow=None)
        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))

        scale = self.ctx.scale
        w, h = self.rect.size()
        gx = self.ctx.value_at(self.x_key, data_t) / self.x_div
        gy = self.ctx.value_at(self.y_key, data_t) / self.y_div
        if not (math.isfinite(gx) and math.isfinite(gy)):
            gx, gy = 0.0, 0.0
        gtot = math.hypot(gx, gy)

        def to_xy(vx: float, vy: float) -> tuple[float, float]:
            mag = math.hypot(vx, vy)
            if mag > self.max_g and mag > 0:
                vx = vx / mag * self.max_g
                vy = vy / mag * self.max_g
            px = self._cx + (vx / self.max_g) * self._radius
            py = self._cy - (vy / self.max_g) * self._radius
            return px, py

        dyn = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dyn)
        accent = self.color("color", "accent")

        # Fading comet trail
        if self.trail_seconds > 0 and self.trail_steps > 1:
            pts: list[tuple[float, float]] = []
            for i in range(self.trail_steps):
                frac = i / max(1, self.trail_steps - 1)
                ti = data_t - self.trail_seconds * (1.0 - frac)
                tx = self.ctx.value_at(self.x_key, ti) / self.x_div
                ty = self.ctx.value_at(self.y_key, ti) / self.y_div
                if math.isfinite(tx) and math.isfinite(ty):
                    pts.append(to_xy(tx, ty))
            for i in range(len(pts) - 1):
                a = ((i + 1) / len(pts)) ** 1.6 * 0.72
                dd.line([pts[i], pts[i + 1]], fill=with_alpha(accent, a),
                        width=max(1, int(round(2.5 * scale))))

        # Vector line + live G puck
        px, py = to_xy(gx, gy)
        dd.line([(self._cx, self._cy), (px, py)], fill=with_alpha(accent, 0.75),
                width=max(1, int(round(2.2 * scale))))
        pr = 7.0 * scale
        dd.ellipse((px - pr * 1.5, py - pr * 1.5, px + pr * 1.5, py + pr * 1.5),
                   fill=with_alpha(accent, 0.30))
        dd.ellipse((px - pr, py - pr, px + pr, py + pr),
                   fill=accent, outline=(255, 255, 255, 240), width=max(1, int(round(1.5 * scale))))

        composite_clipped(image, dyn, int(round(self.rect.x)), int(round(self.rect.y)))

        if self.show_values:
            shadow, offset, blur = self.text_shadow()
            by = self.rect.y1 - self.padding * scale * 0.7
            draw_text(
                image, (self.rect.cx, by), f"{gtot:.2f} G", self.value_font,
                self.color("value_color", "text"), anchor="md",
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )


# ---------------------------------------------------------------------------
# 3. Aviation Artificial Horizon / Attitude Indicator
# ---------------------------------------------------------------------------

@register("attitude", "horizon", "artificial_horizon")
class AttitudeWidget(Widget):
    """Aviation Primary Flight Display artificial horizon driven by pitch & roll."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.pitch_key = self.resolve_channel(
            spec.get("pitch_channel", ["pitch", "pitch_deg", "gimbal_pitch"]), "attitude pitch"
        )
        self.roll_key = self.resolve_channel(
            spec.get("roll_channel", ["roll", "roll_deg", "bank", "lean_angle"]), "attitude roll"
        )
        self.pitch_range = float(spec.get("pitch_range", 30.0))  # deg from center to top
        self.sky_color = parse_color(spec.get("sky_color", "#1d6fb8 @ 78%"))
        self.ground_color = parse_color(spec.get("ground_color", "#7c4a1e @ 78%"))
        self.show_values = bool(spec.get("show_values", True))
        self.small_font = ctx.font(str(spec.get("font", self.theme.font_mono)),
                                   float(spec.get("label_size", 15)))
        self._mask: Image.Image | None = None
        self._bezel: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return 250.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 250.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        size = min(w, h)
        ss = 2

        # Circular clip mask
        mask_hi = Image.new("L", (w * ss, h * ss), 0)
        pad = 6 * scale * ss
        cx, cy = w * ss / 2.0, h * ss / 2.0
        r = size * ss / 2.0 - pad
        ImageDraw.Draw(mask_hi).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        self._mask = mask_hi.reduce(ss)

        # Static bezel + bank ticks + center aircraft boresight
        bez_hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bez_hi)
        border_col = parse_color(self.spec.get("border_color", self.theme.axis))
        bw = max(2, int(round(2.5 * scale * ss)))
        bd.ellipse((cx - r, cy - r, cx + r, cy + r), outline=border_col, width=bw)

        # Top bank-angle ticks: 0, ±10, ±20, ±30, ±45, ±60 deg
        tick_col = (255, 255, 255, 215)
        for deg in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
            ang = math.radians(deg - 90.0)
            ca, sa = math.cos(ang), math.sin(ang)
            tlen = (12 if deg in (-60, -30, 0, 30, 60) else 7) * scale * ss
            r0 = r - tlen
            bd.line([(cx + r0 * ca, cy + r0 * sa), (cx + r * ca, cy + r * sa)],
                    fill=tick_col, width=max(2, int(round(1.8 * scale * ss))))

        # Fixed aircraft boresight wings in center
        acc = self.color("color", "accent")
        wing_w = r * 0.34
        wing_g = r * 0.10
        drop = 7 * scale * ss
        ww = max(3, int(round(3.2 * scale * ss)))
        bd.line([(cx - wing_w, cy), (cx - wing_g, cy), (cx - wing_g, cy + drop)], fill=acc, width=ww)
        bd.line([(cx + wing_w, cy), (cx + wing_g, cy), (cx + wing_g, cy + drop)], fill=acc, width=ww)
        dr = 3.5 * scale * ss
        bd.ellipse((cx - dr, cy - dr, cx + dr, cy + dr), fill=acc)

        self._bezel = bez_hi.reduce(ss)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._mask is None or self._bezel is None:
            return
        scale = self.ctx.scale
        w, h = self.rect.size()
        pitch = self.ctx.value_at(self.pitch_key, data_t)
        roll = self.ctx.value_at(self.roll_key, data_t)
        if not math.isfinite(pitch):
            pitch = 0.0
        if not math.isfinite(roll):
            roll = 0.0

        # Draw oversized horizon canvas then rotate by -roll and crop to (w, h)
        diag = int(math.ceil(math.hypot(w, h) * 1.4))
        cx, cy = diag / 2.0, diag / 2.0
        px_per_deg = (min(w, h) * 0.42) / max(5.0, self.pitch_range)
        horizon_y = cy + pitch * px_per_deg

        canvas = Image.new("RGBA", (diag, diag), self.sky_color)
        cd = ImageDraw.Draw(canvas)
        cd.rectangle((0, horizon_y, diag, diag), fill=self.ground_color)
        # Horizon line
        cd.line([(0, horizon_y), (diag, horizon_y)],
                fill=(255, 255, 255, 235), width=max(2, int(round(2.0 * scale))))

        # Pitch ladder lines every 5° (longer at 10°)
        lad_col = (255, 255, 255, 215)
        lw = max(1, int(round(1.6 * scale)))
        for deg in range(-40, 45, 5):
            if deg == 0:
                continue
            py = horizon_y - deg * px_per_deg
            if py < 0 or py > diag:
                continue
            half = (min(w, h) * (0.16 if deg % 10 == 0 else 0.08))
            cd.line([(cx - half, py), (cx + half, py)], fill=lad_col, width=lw)
            if deg % 10 == 0:
                cd.text((cx + half + 5 * scale, py), f"{abs(deg)}",
                        font=self.small_font, fill=lad_col, anchor="lm")

        rotated = canvas.rotate(roll, resample=Image.BICUBIC, expand=False)
        x0 = (diag - w) // 2
        y0 = (diag - h) // 2
        cropped = rotated.crop((x0, y0, x0 + w, y0 + h))

        # Apply circular mask cleanly
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(cropped, (0, 0), self._mask)

        # Bank pointer triangle inside the top rim
        rad = math.radians(-roll - 90.0)
        r = min(w, h) / 2.0 - 6 * scale
        px_tip = w / 2.0 + (r - 13 * scale) * math.cos(rad)
        py_tip = h / 2.0 + (r - 13 * scale) * math.sin(rad)
        px_base = w / 2.0 + (r - 22 * scale) * math.cos(rad)
        py_base = h / 2.0 + (r - 22 * scale) * math.sin(rad)
        perp_x, perp_y = -math.sin(rad) * 5 * scale, math.cos(rad) * 5 * scale
        ImageDraw.Draw(out).polygon(
            [(px_tip, py_tip), (px_base + perp_x, py_base + perp_y),
             (px_base - perp_x, py_base - perp_y)],
            fill=self.color("color", "accent"),
        )

        out.alpha_composite(self._bezel)
        composite_clipped(image, out, int(round(self.rect.x)), int(round(self.rect.y)))

        if self.show_values:
            shadow, offset, blur = self.text_shadow()
            draw_text(
                image, (self.rect.cx, self.rect.y1 + 10 * scale),
                f"P {pitch:+.0f}°   R {roll:+.0f}°", self.small_font,
                self.color("color", "text"), anchor="ma",
                shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )


# ---------------------------------------------------------------------------
# 4. Compass / Heading Tape & Compass Rose
# ---------------------------------------------------------------------------

_CARDINALS = {
    0: "N", 45: "NE", 90: "E", 135: "SE",
    180: "S", 225: "SW", 270: "W", 315: "NW",
}


@register("compass", "heading")
class CompassWidget(Widget):
    """Aviation/drone heading tape (horizontal) OR circular rotating compass rose."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = self.resolve_channel(
            spec.get("channel", ["bearing", "heading", "yaw", "course"]), "compass"
        )
        self.style = str(spec.get("style", "tape")).lower()  # tape | rose
        self.fov_deg = float(spec.get("fov", 120.0))  # visible degrees on tape
        self.font = ctx.font(str(spec.get("font", self.theme.font_mono)),
                             float(spec.get("size", 18)))
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                 float(spec.get("value_size", 22)))
        self._static: Image.Image | None = None

    def _is_rose(self) -> bool:
        return str(self.spec.get("style", "tape")).lower() == "rose"

    def default_width(self, ctx: RenderContext) -> float:
        return (190.0 if self._is_rose() else 420.0) * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return (190.0 if self._is_rose() else 72.0) * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        self._static = self.build_panel_layer(w, h)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        hdg = self.ctx.value_at(self.channel, data_t)
        if not math.isfinite(hdg):
            hdg = 0.0
        hdg = hdg % 360.0

        if self.style == "rose":
            self._render_rose(image, hdg)
        else:
            self._render_tape(image, hdg)

    def _render_tape(self, image: Image.Image, hdg: float) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = w / 2.0
        px_per_deg = (w - 24 * scale) / max(30.0, self.fov_deg)
        tick_col = self.color("color", "text")
        dim_col = self.color("label_color", "text_dim")
        acc = self.color("accent", "accent")
        base_y = h * 0.56

        # Draw tick marks every 5 deg within visible FOV
        box_w = 68 * scale
        box_h = 26 * scale
        start_deg = int(math.floor((hdg - self.fov_deg / 2.0) / 5.0)) * 5
        end_deg = int(math.ceil((hdg + self.fov_deg / 2.0) / 5.0)) * 5
        for raw_d in range(start_deg, end_deg + 1, 5):
            x = cx + (raw_d - hdg) * px_per_deg
            if x < 10 * scale or x > w - 10 * scale:
                continue
            d_mod = raw_d % 360
            is_card = d_mod in _CARDINALS
            is_major = d_mod % 15 == 0
            t_h = (12 if is_card else (9 if is_major else 5)) * scale
            draw.line([(x, base_y), (x, base_y + t_h)],
                      fill=tick_col if is_major else dim_col,
                      width=max(1, int(round((2.0 if is_card else 1.2) * scale))))
            if abs(x - cx) < box_w * 0.62:
                continue
            if is_card:
                lbl = _CARDINALS[d_mod]
                col = acc if d_mod == 0 else tick_col
                draw.text((x, base_y - 5 * scale), lbl, font=self.font, fill=col, anchor="md")
            elif d_mod % 30 == 0:
                draw.text((x, base_y - 5 * scale), f"{d_mod:03d}", font=self.font, fill=dim_col, anchor="md")

        # Center lubber triangle + crisp heading pill
        tw_p = 6.0 * scale
        draw.polygon(
            [(cx, base_y + 13 * scale), (cx - tw_p, base_y + 21 * scale),
             (cx + tw_p, base_y + 21 * scale)],
            fill=acc,
        )
        rounded_rect(
            draw, (cx - box_w / 2, 5 * scale, cx + box_w / 2, 5 * scale + box_h),
            5 * scale, fill=(10, 14, 22, 235), outline=acc, width=max(1, int(round(1.5 * scale))),
        )
        draw.text((cx, 5 * scale + box_h / 2), f"{int(round(hdg)) % 360:03d}°",
                  font=self.val_font, fill=tick_col, anchor="mm")
        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))

    def _render_rose(self, image: Image.Image, hdg: float) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - 12 * scale
        acc = self.color("accent", "accent")
        txt = self.color("color", "text")
        dim = self.color("label_color", "text_dim")

        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=dim, width=max(1, int(round(1.5 * scale))))
        for deg in range(0, 360, 15):
            ang = math.radians(deg - hdg - 90.0)
            ca, sa = math.cos(ang), math.sin(ang)
            is_card = deg in _CARDINALS
            tlen = (10 if is_card else 5) * scale
            draw.line([(cx + (r - tlen) * ca, cy + (r - tlen) * sa),
                       (cx + r * ca, cy + r * sa)],
                      fill=acc if deg == 0 else txt, width=max(1, int(round((2.0 if is_card else 1.2) * scale))))
            if is_card and deg % 90 == 0:
                tr = r - 22 * scale
                draw.text((cx + tr * ca, cy + tr * sa), _CARDINALS[deg],
                          font=self.font, fill=acc if deg == 0 else txt, anchor="mm")

        # Fixed top pointer + digital readout
        draw.polygon([(cx, cy - r - 1), (cx - 6 * scale, cy - r - 10 * scale),
                      (cx + 6 * scale, cy - r - 10 * scale)], fill=acc)
        draw.text((cx, cy), f"{int(round(hdg)) % 360:03d}°", font=self.val_font, fill=txt, anchor="mm")
        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 5. Aviation / Rocketry Scrolling Ladder Tape
# ---------------------------------------------------------------------------

@register("tape", "ladder")
class TapeWidget(Widget):
    """Scrolling vertical aviation/FPV graduated tape (altitude, speed, Mach, thrust)."""

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
                raise ValueError("tape widget needs a 'channel' or 'expression'")
            self.channel = self.resolve_channel(raw_ch, "tape")
        src = ctx.telemetry[self.channel]
        lo, hi = src.range()
        span = max(1.0, hi - lo)
        self.label = str(spec.get("label", src.label))
        self.unit = str(spec.get("unit", src.unit))
        self.window = float(spec.get("window", max(10.0, span * 0.35)))
        self.step = float(spec.get("step", self._auto_step(self.window)))
        self.side = str(spec.get("side", "left")).lower()  # left | right (which side ticks face)
        self.format = str(spec.get("format", "{:.0f}"))
        self.tick_font = ctx.font(str(spec.get("font", self.theme.font_mono)),
                                  float(spec.get("tick_size", 17)))
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                 float(spec.get("value_size", 24)))
        self.lbl_font = ctx.font(str(spec.get("label_font", self.theme.font)),
                                 float(spec.get("label_size", 15)))
        self._static: Image.Image | None = None

    @staticmethod
    def _auto_step(window: float) -> float:
        rough = window / 5.0
        if rough <= 0:
            return 10.0
        exp = math.floor(math.log10(rough))
        mag = 10.0 ** exp
        for c in (1.0, 2.0, 5.0, 10.0):
            if rough <= c * mag:
                return c * mag
        return 10.0 * mag

    def default_width(self, ctx: RenderContext) -> float:
        return 120.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 340.0 * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        self._static = self.build_panel_layer(w, h)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        val = self.ctx.value_at(self.channel, data_t)
        if not math.isfinite(val):
            val = 0.0

        scale = self.ctx.scale
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        pad_y = 26 * scale
        inner_h = max(20.0, h - 2 * pad_y)
        cy = h / 2.0
        px_per_unit = inner_h / max(1e-6, self.window)
        minor_step = self.step / 2.0

        axis_x = (w - 10 * scale) if self.side == "left" else (10 * scale)
        tick_dir = -1.0 if self.side == "left" else 1.0
        txt_col = self.color("color", "text")
        dim_col = self.color("label_color", "text_dim")
        acc = self.color("accent", "accent")

        # Vertical tape spine
        draw.line([(axis_x, pad_y), (axis_x, h - pad_y)],
                  fill=with_alpha(txt_col, 0.5), width=max(1, int(round(1.5 * scale))))

        v_lo = val - self.window * 0.55
        v_hi = val + self.window * 0.55
        k0 = int(math.floor(v_lo / minor_step))
        k1 = int(math.ceil(v_hi / minor_step))
        for k in range(k0, k1 + 1):
            tv = k * minor_step
            y = cy - (tv - val) * px_per_unit
            if y < pad_y or y > h - pad_y:
                continue
            is_major = (k % 2 == 0)
            tlen = (13 if is_major else 7) * scale
            draw.line([(axis_x, y), (axis_x + tick_dir * tlen, y)],
                      fill=txt_col if is_major else dim_col,
                      width=max(1, int(round((1.8 if is_major else 1.1) * scale))))
            if is_major and abs(y - cy) > 19 * scale:
                tx = axis_x + tick_dir * (tlen + 6 * scale)
                anc = "rm" if self.side == "left" else "lm"
                draw.text((tx, y), f"{tv:g}", font=self.tick_font, fill=dim_col, anchor=anc)

        # Center chevron digital readout box
        bh = 34 * scale
        bw = w - 12 * scale
        bx0 = 6 * scale
        rounded_rect(
            draw, (bx0, cy - bh / 2, bx0 + bw, cy + bh / 2), 5 * scale,
            fill=(8, 12, 20, 235), outline=acc, width=max(1, int(round(1.8 * scale))),
        )
        draw.text((bx0 + bw / 2, cy), format_value(val, self.format),
                  font=self.val_font, fill=txt_col, anchor="mm")

        # Top title & bottom unit
        if self.label:
            draw.text((w / 2, 8 * scale), self.label.upper(), font=self.lbl_font, fill=dim_col, anchor="ma")
        if self.unit:
            draw.text((w / 2, h - 8 * scale), self.unit, font=self.lbl_font, fill=dim_col, anchor="md")

        composite_clipped(image, layer, int(round(self.rect.x)), int(round(self.rect.y)))


# ---------------------------------------------------------------------------
# 6. Motorcycle / Aviation / Sailing Lean & Bank Angle Gauge
# ---------------------------------------------------------------------------

@register("lean", "bank")
class LeanWidget(Widget):
    """Symmetric lean/bank angle arc (-60°..+60°) with L/R peak hold markers."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = self.resolve_channel(
            spec.get("channel", ["lean_angle", "roll", "bank"]), "lean"
        )
        self.max_angle = float(spec.get("max_angle", 60.0))
        self.thickness = float(spec.get("thickness", 14.0))
        self.peak_hold = bool(spec.get("peak_hold", True))
        self.label = str(spec.get("label", "LEAN ANGLE"))
        self.val_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                 float(spec.get("value_size", 36)))
        self.lbl_font = ctx.font(str(spec.get("label_font", self.theme.font)),
                                 float(spec.get("label_size", 16)))
        self._static: Image.Image | None = None

    def default_width(self, ctx: RenderContext) -> float:
        return 260.0 * ctx.scale

    def default_height(self, ctx: RenderContext) -> float:
        return 165.0 * ctx.scale

    def prepare(self) -> None:
        scale = self.ctx.scale
        w, h = self.rect.size()
        static = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            static.alpha_composite(panel)

        ss = 2
        arc_l = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        ad = ImageDraw.Draw(arc_l)
        r = min(w * 0.43, h * 0.72) * ss
        cx = w * ss / 2.0
        cy = h * ss * 0.80
        thick = max(2, int(round(self.thickness * scale * ss)))
        box = (cx - r, cy - r, cx + r, cy + r)
        # Upper arc centered at 270 deg (straight up)
        ad.arc(box, 270.0 - self.max_angle, 270.0 + self.max_angle,
               fill=with_alpha(self.color("track_color", "text"), 0.22), width=thick)
        # Center zero tick
        ad.line([(cx, cy - r - 6 * scale * ss), (cx, cy - r + thick + 2 * ss)],
                fill=(255, 255, 255, 190), width=max(2, int(round(2 * scale * ss))))
        static.alpha_composite(arc_l.reduce(ss))

        if self.label:
            draw_text(static, (w / 2.0, h - 10 * scale), self.label.upper(),
                      self.lbl_font, self.color("label_color", "text_dim"),
                      anchor="md", shadow=None)
        self._static = static

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(image, self._static, int(round(self.rect.x)), int(round(self.rect.y)))
        scale = self.ctx.scale
        w, h = self.rect.size()
        val = self.ctx.value_at(self.channel, data_t)
        if not math.isfinite(val):
            val = 0.0
        clamped = float(np.clip(val, -self.max_angle, self.max_angle))

        ss = 2
        dyn = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dyn)
        r = min(w * 0.43, h * 0.72) * ss
        cx = w * ss / 2.0
        cy = h * ss * 0.80
        thick = max(2, int(round(self.thickness * scale * ss)))
        box = (cx - r, cy - r, cx + r, cy + r)
        acc = self.color("color", "accent")

        if abs(clamped) > 0.3:
            a0, a1 = (270.0 + clamped, 270.0) if clamped < 0 else (270.0, 270.0 + clamped)
            dd.arc(box, a0, a1, fill=acc, width=thick)

        # Rim pointer notch at current lean angle
        p_ang = math.radians(270.0 + clamped)
        ca, sa = math.cos(p_ang), math.sin(p_ang)
        dd.line([(cx + (r - thick * 1.15) * ca, cy + (r - thick * 1.15) * sa),
                 (cx + (r + 5 * ss) * ca, cy + (r + 5 * ss) * sa)],
                fill=acc, width=max(3, int(round(3.5 * scale * ss))))

        # Left & Right peak markers
        if self.peak_hold:
            pk_min = peak_value(self.ctx, self.channel, data_t, "min")
            pk_max = peak_value(self.ctx, self.channel, data_t, "max")
            for pk in (pk_min, pk_max):
                if math.isfinite(pk) and abs(pk) > 1.0:
                    ang = math.radians(270.0 + float(np.clip(pk, -self.max_angle, self.max_angle)))
                    ca, sa = math.cos(ang), math.sin(ang)
                    dd.line([(cx + (r - thick * 0.85) * ca, cy + (r - thick * 0.85) * sa),
                             (cx + (r + 4 * ss) * ca, cy + (r + 4 * ss) * sa)],
                            fill=(255, 255, 255, 235), width=max(2, int(round(2.2 * scale * ss))))

        composite_clipped(image, dyn.reduce(ss), int(round(self.rect.x)), int(round(self.rect.y)))

        shadow, offset, blur = self.text_shadow()
        side = "L" if val < -0.5 else ("R" if val > 0.5 else "")
        txt = f"{abs(val):.1f}° {side}".strip()
        draw_text(
            image, (self.rect.cx, self.rect.y + h * 0.58), txt, self.val_font,
            self.color("value_color", "text"), anchor="mm",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
