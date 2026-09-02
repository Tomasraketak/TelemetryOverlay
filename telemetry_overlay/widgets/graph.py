"""The plot widget: a live curve with a playhead.

Redrawing a polyline of thousands of points for every frame is the obvious
implementation and much too slow at 4K. Instead the curve is rasterised
*once* into a wide "strip" image whose horizontal scale is fixed pixels per
second. Every frame is then a crop of that strip - the visible time window,
clipped at the playhead. Anti-aliasing is paid for once, and a frame costs
little more than an alpha composite.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..theme import (
    RGBA, Rect, draw_text, mix, parse_color, rounded_rect, text_size, with_alpha,
)
from .base import RenderContext, Widget, format_value, register

__all__ = ["GraphWidget"]

# Caps that keep the pre-rendered strip inside a sane memory budget.
_MAX_STRIP_PIXELS = 48_000_000
_MAX_STRIP_WIDTH = 32_000
_MAX_SUPERSAMPLE = 3


def _nice_step(rough: float) -> float:
    """Round a step up to the nearest 1, 2, 2.5 or 5 times a power of ten."""
    if rough <= 0 or not math.isfinite(rough):
        return 1.0
    exponent = math.floor(math.log10(rough))
    magnitude = 10.0 ** exponent
    for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
        if rough <= candidate * magnitude * 1.0000001:
            return candidate * magnitude
    return 10.0 * magnitude


def _ticks(lo: float, hi: float, target: int) -> list[float]:
    """Evenly spaced, round-numbered ticks covering ``[lo, hi]``."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo or target < 1:
        return []
    step = _nice_step((hi - lo) / max(1, target))
    start = math.ceil(lo / step - 1e-9) * step
    out = []
    value = start
    # Guard against a pathological step producing an unbounded loop.
    for _ in range(target * 8 + 16):
        if value > hi + step * 1e-6:
            break
        out.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return out


def _tick_format(step: float) -> str:
    """Pick a decimal count that shows a tick step without noise."""
    if step <= 0 or not math.isfinite(step):
        return "{:.0f}"
    decimals = max(0, min(6, int(math.ceil(-math.log10(step) + 0.2))))
    return f"{{:.{decimals}f}}"


def _decimate(t: np.ndarray, y: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a dense series to at most ~2 points per output column.

    Uses per-column min/max so spikes survive - a plain stride would drop the
    very peak the plot exists to show.
    """
    n = t.size
    if width <= 0 or n <= width * 2:
        return t, y
    edges = np.searchsorted(t, np.linspace(t[0], t[-1], width + 1))
    out_t: list[np.ndarray] = []
    out_y: list[np.ndarray] = []
    for i in range(width):
        a, b = int(edges[i]), int(edges[i + 1])
        if b <= a:
            continue
        chunk = y[a:b]
        finite = np.isfinite(chunk)
        if not finite.any():
            out_t.append(t[a:a + 1])
            out_y.append(chunk[:1])
            continue
        idx_min = a + int(np.nanargmin(np.where(finite, chunk, np.nan)))
        idx_max = a + int(np.nanargmax(np.where(finite, chunk, np.nan)))
        lo, hi = sorted((idx_min, idx_max))
        picks = sorted({a, lo, hi, b - 1})
        out_t.append(t[picks])
        out_y.append(y[picks])
    if not out_t:
        return t, y
    return np.concatenate(out_t), np.concatenate(out_y)


@register("graph")
class GraphWidget(Widget):
    """A time series plot that reveals itself as the clip plays."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        # 'channel' (singular) is one series, and a list there is a set of
        # alternative names for it. 'channels'/'series' (plural) is a list of
        # separate series, each of which may itself be such a list.
        series = spec.get("series") or spec.get("channels")
        if not series:
            channel = spec.get("channel")
            if not channel:
                raise ValueError("graph needs 'channel', 'channels' or 'series'")
            series = [channel]
        elif isinstance(series, (str, dict)):
            series = [series]

        palette = self.theme.series_colors()
        self.series: list[dict[str, Any]] = []
        for i, item in enumerate(series):
            if isinstance(item, (str, list, tuple)):
                item = {"channel": item}
            if not isinstance(item, dict):
                raise ValueError(
                    f"graph series must be a mapping, a channel name, or a list "
                    f"of alternative names: {item!r}")
            key = self.resolve_channel(item.get("channel"), "graph series")
            channel = ctx.telemetry[key]
            self.series.append({
                "key": key,
                "label": str(item.get("label", channel.label)),
                "color": parse_color(item["color"]) if "color" in item
                         else palette[i % len(palette)],
                "width": float(item.get("width", spec.get("line_width", 3.0))),
                "fill": item.get("fill", spec.get("fill", i == 0)),
                "fill_opacity": float(item.get("fill_opacity",
                                               spec.get("fill_opacity", 0.22))),
                "dot": bool(item.get("dot", spec.get("dot", True))),
                "unit": str(item.get("unit", channel.unit)),
            })

        self.mode = str(spec.get("mode", "full")).lower()
        if self.mode not in ("full", "window"):
            raise ValueError("graph mode must be 'full' or 'window'")
        self.window = float(spec.get("window", 3.0))
        self.playhead_pos = float(spec.get("playhead_pos", 1.0))
        self.time_start = spec.get("time_start")
        self.time_end = spec.get("time_end")
        self.y_min = spec.get("y_min")
        self.y_max = spec.get("y_max")
        self.y_pad = float(spec.get("y_pad", 0.08))
        self.x_ticks = int(spec.get("x_ticks", 6))
        self.y_ticks = int(spec.get("y_ticks", 5))
        self.grid = bool(spec.get("grid", True))
        self.axis_labels = bool(spec.get("axis_labels", True))
        self.tick_size = float(spec.get("tick_size", 20))
        self.ghost = bool(spec.get("ghost", True))
        self.ghost_opacity = float(spec.get("ghost_opacity", 0.20))
        self.playhead = bool(spec.get("playhead", True))
        self.playhead_width = float(spec.get("playhead_width", 2.0))
        self.dot_size = float(spec.get("dot_size", 7.0))
        self.glow = bool(spec.get("glow", False))
        self.show_value = bool(spec.get("show_value", False))
        self.value_format = str(spec.get("value_format", "{:.1f}"))
        self.value_size = float(spec.get("value_size", 26))
        self.title = str(spec.get("title", ""))
        self.title_size = float(spec.get("title_size", 26))
        self.x_label = str(spec.get("x_label", ""))
        self.y_label = str(spec.get("y_label", ""))
        self.padding = float(spec.get("padding", 16))
        self.show_events = bool(spec.get("events", True))
        self.zero_line = bool(spec.get("zero_line", True))

        self.tick_font = ctx.font(str(spec.get("font", self.theme.font)),
                                  self.tick_size)
        self.title_font = ctx.font(str(spec.get("title_font", self.theme.font_bold)),
                                   self.title_size)
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)),
                                   self.value_size)

        self._plot: Rect = Rect(0, 0, 1, 1)
        self._static: Image.Image | None = None
        self._strips: list[dict[str, Any]] = []
        self._strip_pad = 0
        self._pps = 1.0
        self._t_lo = 0.0
        self._t_hi = 1.0
        self._y_lo = 0.0
        self._y_hi = 1.0
        self._direct: list[dict[str, Any]] = []
        self._use_strips = True
        self._event_lanes: dict[int, int] = {}
        self._event_rows = 0
        self._event_line_h = 0.0

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.42

    def default_height(self, ctx: RenderContext) -> float:
        return ctx.height * 0.28

    # -- geometry ---------------------------------------------------------
    def _compute_plot_rect(self) -> Rect:
        scale = self.ctx.scale
        pad = self.padding * scale
        left = pad
        right = pad
        top = pad
        bottom = pad
        if self.title:
            top += text_size(self.title_font, "Xg")[1] * 1.7
        if self.axis_labels:
            # Room for the widest y tick label, measured against the real range.
            sample = max(abs(self._y_lo), abs(self._y_hi))
            step = (self._y_hi - self._y_lo) / max(1, self.y_ticks)
            text = _tick_format(step).format(-sample if self._y_lo < 0 else sample)
            left += text_size(self.tick_font, text)[0] + 10 * scale
            bottom += text_size(self.tick_font, "0.0")[1] * 1.9
        if self.y_label:
            left += text_size(self.tick_font, "Xg")[1] * 1.6
        if self.x_label:
            bottom += text_size(self.tick_font, "Xg")[1] * 1.5
        return Rect(self.rect.x + left, self.rect.y + top,
                    max(4.0, self.rect.w - left - right),
                    max(4.0, self.rect.h - top - bottom))

    def _y_range(self) -> tuple[float, float]:
        if self.y_min is not None and self.y_max is not None:
            return float(self.y_min), float(self.y_max)
        lo, hi = math.inf, -math.inf
        for item in self.series:
            channel = self.ctx.telemetry[item["key"]]
            mask = ((self.ctx.telemetry.time >= self._t_lo) &
                    (self.ctx.telemetry.time <= self._t_hi))
            values = channel.values[mask] if mask.any() else channel.values
            values = values[np.isfinite(values)]
            if values.size:
                lo = min(lo, float(values.min()))
                hi = max(hi, float(values.max()))
        if not math.isfinite(lo) or not math.isfinite(hi):
            lo, hi = 0.0, 1.0
        if hi <= lo:
            hi = lo + 1.0
        span = hi - lo
        lo -= span * self.y_pad
        hi += span * self.y_pad
        if self.y_min is not None:
            lo = float(self.y_min)
        if self.y_max is not None:
            hi = float(self.y_max)
        return lo, hi

    def _sx(self, t: float) -> float:
        """Data time to strip x (strip pixels, includes the left pad)."""
        return self._strip_pad + (t - self._t_lo) * self._pps

    def _py(self, value: float) -> float:
        """Data value to plot-local y (0 at the top of the plot area)."""
        span = self._y_hi - self._y_lo
        if span <= 0:
            return self._plot.h / 2.0
        return (1.0 - (value - self._y_lo) / span) * self._plot.h

    def _view_left(self, data_t: float) -> float:
        """Strip x of the plot area's left edge at a given moment."""
        if self.mode == "full":
            return self._sx(self._t_lo)
        return self._sx(data_t) - self._plot.w * self.playhead_pos

    # -- preparation ------------------------------------------------------
    def prepare(self) -> None:
        timeline = self.ctx.timeline
        self._t_lo = float(self.time_start) if self.time_start is not None \
            else timeline.data_start
        self._t_hi = float(self.time_end) if self.time_end is not None \
            else timeline.data_end
        if self._t_hi <= self._t_lo:
            self._t_hi = self._t_lo + 1.0
        self._y_lo, self._y_hi = self._y_range()
        self._plot = self._compute_plot_rect()

        span = self._t_hi - self._t_lo
        if self.mode == "full":
            self._pps = self._plot.w / span
        else:
            self._pps = self._plot.w / max(1e-6, self.window)

        max_line = max((s["width"] for s in self.series), default=3.0) * self.ctx.scale
        self._strip_pad = int(math.ceil(max_line * 2 + self.dot_size * self.ctx.scale))
        strip_w = int(math.ceil(span * self._pps)) + self._strip_pad * 2
        strip_h = int(math.ceil(self._plot.h))

        self._use_strips = (
            strip_w <= _MAX_STRIP_WIDTH
            and strip_w * strip_h <= _MAX_STRIP_PIXELS
        )
        self._assign_event_lanes()
        self._build_static()
        if self._use_strips:
            self._build_strips(strip_w, strip_h)
        else:
            self._build_direct()

    def _assign_event_lanes(self) -> None:
        """Stack event labels into rows so neighbouring ones do not collide.

        Markers often cluster - ignition and a pyro edge can be milliseconds
        apart - and overlapping text is worse than no text at all.
        """
        font = self.ctx.font(self.theme.font, self.tick_size * 0.85)
        self._event_line_h = text_size(font, "Xg")[1] * 1.25
        lanes: list[float] = []          # right edge, in strip pixels, per lane
        self._event_lanes = {}
        visible = [(i, e) for i, e in enumerate(self.ctx.events)
                   if math.isfinite(float(e.get("time", math.nan)))
                   and self._t_lo <= float(e["time"]) <= self._t_hi]
        for index, event in sorted(visible, key=lambda pair: float(pair[1]["time"])):
            x = self._sx(float(event["time"]))
            width = text_size(font, str(event.get("label", "")))[0] + 12 * self.ctx.scale
            for lane, right in enumerate(lanes):
                if x >= right:
                    lanes[lane] = x + width
                    self._event_lanes[index] = lane
                    break
            else:
                lanes.append(x + width)
                self._event_lanes[index] = len(lanes) - 1
        self._event_rows = len(lanes)

    def _series_points(self, key: str) -> tuple[np.ndarray, np.ndarray]:
        telemetry = self.ctx.telemetry
        mask = (telemetry.time >= self._t_lo) & (telemetry.time <= self._t_hi)
        t = telemetry.time[mask]
        y = telemetry[key].values[mask]
        if t.size == 0:
            return t, y
        return t, y

    def _polyline(self, t: np.ndarray, y: np.ndarray, ss: int,
                  ) -> list[list[tuple[float, float]]]:
        """Screen-space runs of the curve, split at gaps in the data."""
        if t.size == 0:
            return []
        xs = (self._strip_pad + (t - self._t_lo) * self._pps) * ss
        span = self._y_hi - self._y_lo
        if span <= 0:
            span = 1.0
        ys = (1.0 - (y - self._y_lo) / span) * self._plot.h * ss
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for xi, yi in zip(xs, ys):
            if not math.isfinite(yi):
                if len(current) > 1:
                    runs.append(current)
                current = []
                continue
            current.append((float(xi), float(yi)))
        if len(current) > 1:
            runs.append(current)
        elif len(current) == 1:
            runs.append(current * 2)
        return runs

    def _build_strips(self, strip_w: int, strip_h: int) -> None:
        """Rasterise every series into a wide image, once."""
        budget = _MAX_STRIP_PIXELS / max(1, strip_w * strip_h)
        ss = int(max(1, min(_MAX_SUPERSAMPLE, math.floor(math.sqrt(budget)))))
        self._strips = []
        for item in self.series:
            t, y = self._series_points(item["key"])
            t, y = _decimate(t, y, strip_w)
            runs = self._polyline(t, y, ss)
            line_w = max(1, int(round(item["width"] * self.ctx.scale * ss)))

            curve = Image.new("RGBA", (strip_w * ss, strip_h * ss), (0, 0, 0, 0))
            draw = ImageDraw.Draw(curve)
            for run in runs:
                draw.line(run, fill=item["color"], width=line_w, joint="curve")

            fill_img = None
            if item["fill"]:
                fill_img = Image.new("RGBA", (strip_w * ss, strip_h * ss), (0, 0, 0, 0))
                fdraw = ImageDraw.Draw(fill_img)
                # Fill down to the zero line when it is visible, else to the floor.
                base_y = self._py(max(self._y_lo, min(0.0, self._y_hi))) * ss
                base_y = float(np.clip(base_y, 0, strip_h * ss))
                color = with_alpha(item["color"], item["fill_opacity"])
                for run in runs:
                    if len(run) < 2:
                        continue
                    poly = run + [(run[-1][0], base_y), (run[0][0], base_y)]
                    fdraw.polygon(poly, fill=color)

            if ss > 1:
                # reduce() is an exact box average for an integer factor -
                # the right filter for supersampling, and far cheaper.
                curve = curve.reduce(ss)
                if fill_img is not None:
                    fill_img = fill_img.reduce(ss)

            glow = None
            if self.glow:
                blur = max(2, int(round(item["width"] * self.ctx.scale)))
                glow = curve.filter(ImageFilter.GaussianBlur(blur))
                bands = glow.split()
                glow.putalpha(bands[3].point(lambda a: min(255, int(a * 1.5))))

            ghost = None
            if self.ghost:
                ghost = curve.copy()
                bands = ghost.split()
                ghost.putalpha(
                    bands[3].point(lambda a: int(a * self.ghost_opacity)))

            self._strips.append({
                "curve": curve, "fill": fill_img, "ghost": ghost, "glow": glow,
            })

    def _build_direct(self) -> None:
        """Fallback for logs too long to pre-rasterise: keep decimated points."""
        self._direct = []
        for item in self.series:
            t, y = self._series_points(item["key"])
            t, y = _decimate(t, y, int(self._plot.w) * 4)
            self._direct.append({"t": t, "y": y})

    def _build_static(self) -> None:
        """Panel, grid and labels that never change between frames."""
        w, h = max(1, int(math.ceil(self.rect.w))), max(1, int(math.ceil(self.rect.h)))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        offset = Rect(0, 0, self.rect.w, self.rect.h)
        draw = ImageDraw.Draw(layer)
        scale = self.ctx.scale

        panel = self.spec.get("panel", True)
        if panel is not False:
            fill = parse_color(self.spec.get("panel_color", self.theme.panel))
            border = parse_color(self.spec.get("panel_border", self.theme.panel_border))
            radius = self.ctx.px_x(self.spec.get("corner_radius"),
                                   self.theme.corner_radius * scale)
            rounded_rect(draw, (0, 0, w - 1, h - 1), radius, fill=fill,
                         outline=border if border[3] else None,
                         width=max(1, int(round(scale))))

        # Plot rect in layer-local coordinates.
        px = self._plot.x - self.rect.x
        py = self._plot.y - self.rect.y
        grid_color = parse_color(self.spec.get("grid_color", self.theme.grid))
        axis_color = parse_color(self.spec.get("axis_color", self.theme.axis))
        tick_color = parse_color(self.spec.get("tick_color", self.theme.text_dim))
        line_w = max(1, int(round(scale)))

        y_ticks = _ticks(self._y_lo, self._y_hi, self.y_ticks)
        y_step = (y_ticks[1] - y_ticks[0]) if len(y_ticks) > 1 else \
            (self._y_hi - self._y_lo)
        y_fmt = _tick_format(y_step)
        for value in y_ticks:
            gy = py + self._py(value)
            if self.grid:
                draw.line([(px, gy), (px + self._plot.w, gy)], fill=grid_color,
                          width=line_w)
            if self.axis_labels:
                draw.text((px - 8 * scale, gy), y_fmt.format(value),
                          font=self.tick_font, fill=tick_color, anchor="rm")

        if self.zero_line and self._y_lo < 0.0 < self._y_hi:
            zy = py + self._py(0.0)
            draw.line([(px, zy), (px + self._plot.w, zy)], fill=axis_color,
                      width=line_w)

        # Axis frame.
        draw.line([(px, py), (px, py + self._plot.h)], fill=axis_color, width=line_w)
        draw.line([(px, py + self._plot.h), (px + self._plot.w, py + self._plot.h)],
                  fill=axis_color, width=line_w)

        # In full mode the time axis is fixed, so its ticks can be baked in.
        if self.mode == "full":
            self._draw_time_axis(draw, px, py, self._t_lo, self._t_hi,
                                 grid_color, tick_color, line_w)

        if self.title:
            draw.text((px, self.padding * scale * 0.4), self.title,
                      font=self.title_font,
                      fill=parse_color(self.spec.get("title_color", self.theme.text)),
                      anchor="la")
        if self.x_label:
            draw.text((px + self._plot.w / 2, h - self.padding * scale * 0.3),
                      self.x_label, font=self.tick_font, fill=tick_color, anchor="md")
        if self.y_label:
            # Pillow cannot rotate text in place, so the label is drawn on its
            # own small layer and pasted turned.
            tw, th = text_size(self.tick_font, self.y_label)
            tmp = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((2, 2), self.y_label, font=self.tick_font,
                                     fill=tick_color)
            tmp = tmp.rotate(90, expand=True)
            layer.alpha_composite(tmp, (int(self.padding * scale * 0.2),
                                        int(py + (self._plot.h - tmp.height) / 2)))

        self._static = layer

    def _draw_time_axis(self, draw: ImageDraw.ImageDraw, px: float, py: float,
                        t_lo: float, t_hi: float, grid_color: RGBA,
                        tick_color: RGBA, line_w: int) -> None:
        ticks = _ticks(t_lo, t_hi, self.x_ticks)
        step = (ticks[1] - ticks[0]) if len(ticks) > 1 else (t_hi - t_lo)
        fmt = _tick_format(step)
        scale = self.ctx.scale
        for value in ticks:
            frac = (value - t_lo) / (t_hi - t_lo) if t_hi > t_lo else 0.0
            gx = px + frac * self._plot.w
            if gx < px - 0.5 or gx > px + self._plot.w + 0.5:
                continue
            if self.grid:
                draw.line([(gx, py), (gx, py + self._plot.h)], fill=grid_color,
                          width=line_w)
            if self.axis_labels:
                label = fmt.format(value - self.ctx.t0)
                draw.text((gx, py + self._plot.h + 6 * scale), label,
                          font=self.tick_font, fill=tick_color, anchor="ma")

    # -- per frame --------------------------------------------------------
    def _paste_strip(self, image: Image.Image, strip: Image.Image,
                     src_left: float, clip_right: float) -> None:
        """Composite the visible slice of a strip into the plot area."""
        src_x0 = int(math.floor(src_left))
        src_x1 = int(math.ceil(min(src_left + self._plot.w, clip_right)))
        src_x0_c = max(0, src_x0)
        src_x1_c = min(strip.width, src_x1)
        if src_x1_c <= src_x0_c:
            return
        dest_x = int(round(self._plot.x + (src_x0_c - src_left)))
        dest_y = int(round(self._plot.y))
        region = strip.crop((src_x0_c, 0, src_x1_c, strip.height))
        image.alpha_composite(region, (dest_x, dest_y))

    def _clip_to_plot(self, image: Image.Image, layer: Image.Image) -> None:
        image.alpha_composite(layer)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            image.alpha_composite(self._static,
                                  (int(round(self.rect.x)), int(round(self.rect.y))))

        scale = self.ctx.scale
        view_left = self._view_left(data_t)
        head_x = self._sx(data_t)
        # The playhead never runs past the end of the logged data.
        head_x = min(head_x, self._sx(self._t_hi))

        # A scrolling window needs its time ticks redrawn every frame.
        if self.mode == "window":
            t_view_lo = self._t_lo + (view_left - self._strip_pad) / self._pps
            t_view_hi = t_view_lo + self._plot.w / self._pps
            axis_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            self._draw_time_axis(
                ImageDraw.Draw(axis_layer), self._plot.x, self._plot.y,
                t_view_lo, t_view_hi,
                parse_color(self.spec.get("grid_color", self.theme.grid)),
                parse_color(self.spec.get("tick_color", self.theme.text_dim)),
                max(1, int(round(scale))),
            )
            image.alpha_composite(axis_layer)

        if self.show_events:
            self._draw_events(image, view_left, head_x)

        if self._use_strips:
            far_right = self._sx(self._t_hi) + self._strip_pad
            for item, strip in zip(self.series, self._strips):
                if strip["ghost"] is not None:
                    self._paste_strip(image, strip["ghost"], view_left, far_right)
                if strip["fill"] is not None:
                    self._paste_strip(image, strip["fill"], view_left, head_x)
                if strip["glow"] is not None:
                    self._paste_strip(image, strip["glow"], view_left, head_x)
                self._paste_strip(image, strip["curve"], view_left, head_x)
        else:
            self._render_direct(image, data_t, view_left, head_x)

        if self.playhead:
            self._draw_playhead(image, data_t, view_left, head_x)
        self._draw_dots(image, data_t, view_left, head_x)

    def _render_direct(self, image: Image.Image, data_t: float,
                       view_left: float, head_x: float) -> None:
        """Per-frame polyline path, used only for very long logs."""
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        scale = self.ctx.scale
        for item, points in zip(self.series, self._direct):
            t, y = points["t"], points["y"]
            visible = t <= data_t
            if not visible.any():
                continue
            t, y = t[visible], y[visible]
            xs = self._sx(t) - view_left + self._plot.x
            span = self._y_hi - self._y_lo or 1.0
            ys = (1.0 - (y - self._y_lo) / span) * self._plot.h + self._plot.y
            run: list[tuple[float, float]] = []
            width = max(1, int(round(item["width"] * scale)))
            for xi, yi in zip(xs, ys):
                if not math.isfinite(yi) or xi < self._plot.x - width:
                    if len(run) > 1:
                        draw.line(run, fill=item["color"], width=width, joint="curve")
                    run = []
                    continue
                run.append((float(xi), float(yi)))
            if len(run) > 1:
                draw.line(run, fill=item["color"], width=width, joint="curve")
        # Keep the curve inside the plot box.
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rectangle(self._plot.as_tuple(), fill=255)
        layer.putalpha(Image.composite(layer.split()[3],
                                       Image.new("L", image.size, 0), mask))
        image.alpha_composite(layer)

    def _draw_events(self, image: Image.Image, view_left: float,
                     head_x: float) -> None:
        scale = self.ctx.scale
        draw = ImageDraw.Draw(image)
        font = self.ctx.font(self.theme.font, self.tick_size * 0.85)
        line_h = self._event_line_h
        for index, event in enumerate(self.ctx.events):
            t = float(event.get("time", math.nan))
            if not math.isfinite(t) or not (self._t_lo <= t <= self._t_hi):
                continue
            x = self._sx(t) - view_left + self._plot.x
            if x < self._plot.x - 1 or x > self._plot.x1 + 1:
                continue
            color = parse_color(event.get("color", self.theme.accent))
            reached = self._sx(t) <= head_x
            alpha = 1.0 if reached else 0.35
            line_color = with_alpha(color, alpha * 0.85)
            width = max(1, int(round(scale)))
            # Dashed vertical marker.
            dash, gap = 7 * scale, 6 * scale
            y = self._plot.y
            while y < self._plot.y1:
                draw.line([(x, y), (x, min(y + dash, self._plot.y1))],
                          fill=line_color, width=width)
                y += dash + gap
            label = str(event.get("label", ""))
            if not label or not reached:
                continue
            lane = self._event_lanes.get(index, 0)
            ty = self._plot.y + 4 * scale + lane * line_h
            # A label near the right edge is flipped to the inside.
            if x + text_size(font, label)[0] + 8 * scale > self._plot.x1:
                draw.text((x - 5 * scale, ty), label, font=font,
                          fill=with_alpha(color, alpha), anchor="ra")
            else:
                draw.text((x + 5 * scale, ty), label, font=font,
                          fill=with_alpha(color, alpha), anchor="la")

    def _draw_playhead(self, image: Image.Image, data_t: float,
                       view_left: float, head_x: float) -> None:
        x = head_x - view_left + self._plot.x
        if x < self._plot.x - 1 or x > self._plot.x1 + 1:
            return
        draw = ImageDraw.Draw(image)
        color = self.color("playhead_color", "accent")
        width = max(1, int(round(self.playhead_width * self.ctx.scale)))
        draw.line([(x, self._plot.y), (x, self._plot.y1)],
                  fill=with_alpha(color, 0.8), width=width)

    def _draw_dots(self, image: Image.Image, data_t: float, view_left: float,
                   head_x: float) -> None:
        scale = self.ctx.scale
        draw = ImageDraw.Draw(image)
        x = head_x - view_left + self._plot.x
        for item in self.series:
            if not item["dot"]:
                continue
            value = self.ctx.value_at(item["key"], data_t)
            if not math.isfinite(value):
                continue
            y = self._plot.y + self._py(value)
            if not (self._plot.y - 2 <= y <= self._plot.y1 + 2):
                continue
            if x < self._plot.x - 2 or x > self._plot.x1 + 2:
                continue
            r = self.dot_size * scale
            draw.ellipse((x - r, y - r, x + r, y + r), fill=item["color"])
            draw.ellipse((x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45),
                         fill=(255, 255, 255, 235))
            if self.show_value:
                text = format_value(value, self.value_format)
                if item["unit"]:
                    text = f"{text} {item['unit']}"
                shadow, offset, blur = self.text_shadow()
                # Flip the label to the other side when it would fall off.
                tw, th = text_size(self.value_font, text)
                left = x + r * 2.2
                anchor = "lm"
                if left + tw > self._plot.x1:
                    left = x - r * 2.2
                    anchor = "rm"
                # Event labels occupy a band at the top of the plot; the
                # value drops below the whole band rather than overprint it.
                label_y = y
                if self.show_events and self._event_rows:
                    band = (self._plot.y + 4 * scale
                            + self._event_rows * self._event_line_h)
                    if label_y - th / 2 < band:
                        label_y = max(y + r * 2.4 + th * 0.6, band + th * 0.7)
                        label_y = min(label_y, self._plot.y1 - th * 0.6)
                draw_text(image, (left, label_y), text, self.value_font,
                          item["color"], anchor=anchor, shadow=shadow,
                          shadow_offset=offset, shadow_blur=blur)
