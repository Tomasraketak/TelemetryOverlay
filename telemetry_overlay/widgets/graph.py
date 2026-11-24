"""The plot widget: a live curve with a playhead.

Curves are pre-rasterised once into wide strips and merged into just two
composite layers (one ghost strip, one active fill+glow+curve strip) so each
frame costs only two fast crops and composites. Window-mode axes and direct
fallbacks render on tight plot-sized scratch layers instead of full-frame
allocations.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..theme import (
    RGBA, Rect, composite_clipped, draw_text, parse_color, rounded_rect,
    text_size, with_alpha,
)
from .base import RenderContext, Widget, format_value, register

__all__ = ["GraphWidget"]

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
    for _ in range(target * 8 + 16):
        if value > hi + step * 1e-6:
            break
        out.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return out


def _tick_format(step: float) -> str:
    if step <= 0 or not math.isfinite(step):
        return "{:.0f}"
    decimals = max(0, min(6, int(math.ceil(-math.log10(step) + 0.2))))
    return f"{{:.{decimals}f}}"


def _decimate(t: np.ndarray, y: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a dense series to at most ~2 points per output column (preserving spikes)."""
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


@register("graph", "plot", "chart")
class GraphWidget(Widget):
    """A time series plot that reveals itself as the clip plays."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        series = spec.get("series") or spec.get("channels")
        if not series:
            channel = spec.get("channel")
            expr = spec.get("expression")
            if not channel and not expr:
                raise ValueError("graph needs 'channel', 'channels', 'series' or 'expression'")
            series = [{"expression": expr}] if expr else [channel]
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
            if item.get("expression"):
                key = ctx.ensure_expression(
                    str(item["expression"]),
                    unit=str(item.get("unit", "")),
                    label=str(item.get("label", "")),
                )
            else:
                key = self.resolve_channel(item.get("channel"), "graph series")
            channel = ctx.telemetry[key]
            self.series.append({
                "key": key,
                "label": str(item.get("label", channel.label)),
                "color": parse_color(item["color"]) if "color" in item
                         else palette[i % len(palette)],
                "width": float(item.get("width", spec.get("line_width", 3.0))),
                "fill": item.get("fill", spec.get("fill", i == 0)),
                "fill_opacity": float(item.get("fill_opacity", spec.get("fill_opacity", 0.24))),
                "gradient": bool(item.get("gradient", spec.get("gradient", True))),
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
        self.legend = bool(spec.get("legend", len(self.series) > 1))

        self.tick_font = ctx.font(str(spec.get("font", self.theme.font)), self.tick_size)
        self.title_font = ctx.font(str(spec.get("title_font", self.theme.font_bold)), self.title_size)
        self.value_font = ctx.font(str(spec.get("value_font", self.theme.font_mono)), self.value_size)
        self.event_font = ctx.font(self.theme.font, self.tick_size * 0.85)

        self._plot: Rect = Rect(0, 0, 1, 1)
        self._static: Image.Image | None = None
        self._merged_ghost: Image.Image | None = None
        self._merged_active: Image.Image | None = None
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
        self._events_cache: dict[int, Image.Image | None] = {}

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
        if self.title or self.legend:
            top += text_size(self.title_font, "Xg")[1] * 1.7
        if self.axis_labels:
            sample = max(abs(self._y_lo), abs(self._y_hi))
            step = (self._y_hi - self._y_lo) / max(1, self.y_ticks)
            text = _tick_format(step).format(-sample if self._y_lo < 0 else sample)
            left += text_size(self.tick_font, text)[0] + 10 * scale
            bottom += text_size(self.tick_font, "0.0")[1] * 1.9
        if self.y_label:
            left += text_size(self.tick_font, "Xg")[1] * 1.6
        if self.x_label:
            bottom += text_size(self.tick_font, "Xg")[1] * 1.5
        return Rect(
            self.rect.x + left, self.rect.y + top,
            max(4.0, self.rect.w - left - right),
            max(4.0, self.rect.h - top - bottom),
        )

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
        return self._strip_pad + (t - self._t_lo) * self._pps

    def _py(self, value: float) -> float:
        span = self._y_hi - self._y_lo
        if span <= 0:
            return self._plot.h / 2.0
        return (1.0 - (value - self._y_lo) / span) * self._plot.h

    def _view_left(self, data_t: float) -> float:
        if self.mode == "full":
            return self._sx(self._t_lo)
        return self._sx(data_t) - self._plot.w * self.playhead_pos

    # -- preparation ------------------------------------------------------
    def prepare(self) -> None:
        timeline = self.ctx.timeline
        self._t_lo = float(self.time_start) if self.time_start is not None else timeline.data_start
        self._t_hi = float(self.time_end) if self.time_end is not None else timeline.data_end
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
        strip_h = max(1, int(math.ceil(self._plot.h)))

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
        self._event_line_h = text_size(self.event_font, "Xg")[1] * 1.25
        lanes: list[float] = []
        self._event_lanes = {}
        visible = [
            (i, e) for i, e in enumerate(self.ctx.events)
            if math.isfinite(float(e.get("time", math.nan)))
            and self._t_lo <= float(e["time"]) <= self._t_hi
        ]
        for index, event in sorted(visible, key=lambda pair: float(pair[1]["time"])):
            x = self._sx(float(event["time"]))
            width = text_size(self.event_font, str(event.get("label", "")))[0] + 14 * self.ctx.scale
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
        return telemetry.time[mask], telemetry[key].values[mask]

    def _polyline(self, t: np.ndarray, y: np.ndarray, ss: int) -> list[list[tuple[float, float]]]:
        if t.size == 0:
            return []
        xs = (self._strip_pad + (t - self._t_lo) * self._pps) * ss
        span = self._y_hi - self._y_lo or 1.0
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

    def _apply_vertical_gradient(self, fill_img: Image.Image, base_y: float) -> Image.Image:
        """Fade area fill gently toward the baseline for a sleek broadcast look."""
        h = fill_img.height
        if h <= 2:
            return fill_img
        arr = np.array(fill_img, dtype=np.uint8)
        alpha = arr[:, :, 3].astype(np.float32)
        ys = np.arange(h, dtype=np.float32)
        # Stronger near the curve top (y=0), softer near baseline (y=base_y)
        denom = max(1.0, float(base_y))
        fade = np.clip(1.0 - (ys / denom) * 0.78, 0.14, 1.0)
        arr[:, :, 3] = np.clip(alpha * fade[:, None], 0, 255).astype(np.uint8)
        return Image.fromarray(arr, mode="RGBA")

    def _build_strips(self, strip_w: int, strip_h: int) -> None:
        """Rasterise all series and merge into ONE ghost strip + ONE active strip."""
        budget = _MAX_STRIP_PIXELS / max(1, strip_w * strip_h)
        ss = int(max(1, min(_MAX_SUPERSAMPLE, math.floor(math.sqrt(budget)))))

        merged_active = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        merged_ghost = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0)) if self.ghost else None
        ghost_lut = [int(round(a * self.ghost_opacity)) for a in range(256)]
        glow_lut = [min(255, int(a * 1.5)) for a in range(256)]

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
            base_y_px = self._py(max(self._y_lo, min(0.0, self._y_hi)))
            if item["fill"]:
                fill_img = Image.new("RGBA", (strip_w * ss, strip_h * ss), (0, 0, 0, 0))
                fdraw = ImageDraw.Draw(fill_img)
                base_y = float(np.clip(base_y_px * ss, 0, strip_h * ss))
                color = with_alpha(item["color"], item["fill_opacity"])
                for run in runs:
                    if len(run) < 2:
                        continue
                    poly = run + [(run[-1][0], base_y), (run[0][0], base_y)]
                    fdraw.polygon(poly, fill=color)

            if ss > 1:
                curve = curve.reduce(ss)
                if fill_img is not None:
                    fill_img = fill_img.reduce(ss)

            if fill_img is not None:
                if item["gradient"]:
                    fill_img = self._apply_vertical_gradient(fill_img, base_y_px)
                merged_active.alpha_composite(fill_img)

            if self.glow:
                blur = max(2, int(round(item["width"] * self.ctx.scale)))
                glow = curve.filter(ImageFilter.GaussianBlur(blur))
                glow.putalpha(glow.getchannel("A").point(glow_lut))
                merged_active.alpha_composite(glow)

            merged_active.alpha_composite(curve)

            if merged_ghost is not None:
                ghost = curve.copy()
                ghost.putalpha(ghost.getchannel("A").point(ghost_lut))
                merged_ghost.alpha_composite(ghost)

        self._merged_active = merged_active
        self._merged_ghost = merged_ghost

    def _build_direct(self) -> None:
        self._direct = []
        for item in self.series:
            t, y = self._series_points(item["key"])
            t, y = _decimate(t, y, int(self._plot.w) * 4)
            self._direct.append({"t": t, "y": y})

    def _build_static(self) -> None:
        w, h = max(1, int(math.ceil(self.rect.w))), max(1, int(math.ceil(self.rect.h)))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        scale = self.ctx.scale

        panel = self.build_panel_layer(w, h)
        if panel is not None:
            layer.alpha_composite(panel)

        draw = ImageDraw.Draw(layer)
        px = self._plot.x - self.rect.x
        py = self._plot.y - self.rect.y
        grid_color = parse_color(self.spec.get("grid_color", self.theme.grid))
        axis_color = parse_color(self.spec.get("axis_color", self.theme.axis))
        tick_color = parse_color(self.spec.get("tick_color", self.theme.text_dim))
        line_w = max(1, int(round(scale)))

        # Draw grid & axes onto a separate overlay layer so alpha blends cleanly over panel
        grid_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(grid_layer)

        y_ticks = _ticks(self._y_lo, self._y_hi, self.y_ticks)
        y_step = (y_ticks[1] - y_ticks[0]) if len(y_ticks) > 1 else (self._y_hi - self._y_lo)
        y_fmt = _tick_format(y_step)
        for value in y_ticks:
            gy = py + self._py(value)
            if self.grid:
                gdraw.line([(px, gy), (px + self._plot.w, gy)], fill=grid_color, width=line_w)
            if self.axis_labels:
                gdraw.text((px - 8 * scale, gy), y_fmt.format(value),
                           font=self.tick_font, fill=tick_color, anchor="rm")

        if self.zero_line and self._y_lo < 0.0 < self._y_hi:
            zy = py + self._py(0.0)
            gdraw.line([(px, zy), (px + self._plot.w, zy)], fill=axis_color, width=line_w)

        gdraw.line([(px, py), (px, py + self._plot.h)], fill=axis_color, width=line_w)
        gdraw.line([(px, py + self._plot.h), (px + self._plot.w, py + self._plot.h)],
                   fill=axis_color, width=line_w)

        if self.mode == "full":
            self._draw_time_axis(gdraw, px, py, self._t_lo, self._t_hi,
                                 grid_color, tick_color, line_w)

        if self.title:
            gdraw.text(
                (px, self.padding * scale * 0.4), self.title,
                font=self.title_font,
                fill=parse_color(self.spec.get("title_color", self.theme.text)),
                anchor="la",
            )

        # Optional multi-series legend top-right of the header bar
        if self.legend and self.series:
            rx = px + self._plot.w
            ly = self.padding * scale * 0.4 + text_size(self.title_font, "Xg")[1] * 0.5
            swatch = max(6.0, 9.0 * scale)
            for item in reversed(self.series):
                lbl = item["label"]
                tw, _ = text_size(self.tick_font, lbl)
                rx -= tw
                gdraw.text((rx, ly), lbl, font=self.tick_font, fill=tick_color, anchor="lm")
                rx -= swatch + 6 * scale
                gdraw.ellipse((rx, ly - swatch / 2, rx + swatch, ly + swatch / 2), fill=item["color"])
                rx -= 14 * scale

        if self.x_label:
            gdraw.text((px + self._plot.w / 2, h - self.padding * scale * 0.3),
                       self.x_label, font=self.tick_font, fill=tick_color, anchor="md")

        layer.alpha_composite(grid_layer)

        if self.y_label:
            tw, th = text_size(self.tick_font, self.y_label)
            tmp = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((2, 2), self.y_label, font=self.tick_font, fill=tick_color)
            tmp = tmp.rotate(90, expand=True)
            composite_clipped(
                layer, tmp,
                int(self.padding * scale * 0.2),
                int(py + (self._plot.h - tmp.height) / 2),
            )

        self._static = layer

    def _draw_time_axis(
        self, draw: ImageDraw.ImageDraw, px: float, py: float,
        t_lo: float, t_hi: float, grid_color: RGBA,
        tick_color: RGBA, line_w: int,
    ) -> None:
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
                draw.line([(gx, py), (gx, py + self._plot.h)], fill=grid_color, width=line_w)
            if self.axis_labels:
                label = fmt.format(value - self.ctx.t0)
                draw.text((gx, py + self._plot.h + 6 * scale), label,
                          font=self.tick_font, fill=tick_color, anchor="ma")

    # -- per frame --------------------------------------------------------
    def _paste_strip(
        self, image: Image.Image, strip: Image.Image,
        src_left: float, clip_right: float,
    ) -> None:
        src_x0 = int(math.floor(src_left))
        src_x1 = int(math.ceil(min(src_left + self._plot.w, clip_right)))
        src_x0_c = max(0, src_x0)
        src_x1_c = min(strip.width, src_x1)
        if src_x1_c <= src_x0_c:
            return
        dest_x = int(round(self._plot.x + (src_x0_c - src_left)))
        dest_y = int(round(self._plot.y))
        region = strip.crop((src_x0_c, 0, src_x1_c, strip.height))
        composite_clipped(image, region, dest_x, dest_y)

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static is not None:
            composite_clipped(
                image, self._static, int(round(self.rect.x)), int(round(self.rect.y))
            )

        scale = self.ctx.scale
        view_left = self._view_left(data_t)
        head_x = min(self._sx(data_t), self._sx(self._t_hi))

        # Tight widget-sized scratch layer for window-mode scrolling ticks (never full-frame!)
        if self.mode == "window":
            t_view_lo = self._t_lo + (view_left - self._strip_pad) / self._pps
            t_view_hi = t_view_lo + self._plot.w / self._pps
            w, h = self.rect.size()
            axis_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            px = self._plot.x - self.rect.x
            py = self._plot.y - self.rect.y
            self._draw_time_axis(
                ImageDraw.Draw(axis_layer), px, py, t_view_lo, t_view_hi,
                parse_color(self.spec.get("grid_color", self.theme.grid)),
                parse_color(self.spec.get("tick_color", self.theme.text_dim)),
                max(1, int(round(scale))),
            )
            composite_clipped(image, axis_layer, int(round(self.rect.x)), int(round(self.rect.y)))

        if self.show_events:
            self._draw_events(image, view_left, head_x)

        if self._use_strips:
            far_right = self._sx(self._t_hi) + self._strip_pad
            if self._merged_ghost is not None:
                self._paste_strip(image, self._merged_ghost, view_left, far_right)
            if self._merged_active is not None:
                self._paste_strip(image, self._merged_active, view_left, head_x)
        else:
            self._render_direct(image, data_t, view_left, head_x)

        if self.playhead:
            self._draw_playhead(image, data_t, view_left, head_x)
        self._draw_dots(image, data_t, view_left, head_x)

    def _render_direct(
        self, image: Image.Image, data_t: float, view_left: float, head_x: float
    ) -> None:
        """Per-frame polyline path on a tight plot-sized scratch layer."""
        pw, ph = self._plot.size()
        layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        scale = self.ctx.scale
        span = self._y_hi - self._y_lo or 1.0
        for item, points in zip(self.series, self._direct):
            t, y = points["t"], points["y"]
            visible = t <= data_t
            if not visible.any():
                continue
            t, y = t[visible], y[visible]
            xs = self._sx(t) - view_left
            ys = (1.0 - (y - self._y_lo) / span) * self._plot.h
            run: list[tuple[float, float]] = []
            width = max(1, int(round(item["width"] * scale)))
            for xi, yi in zip(xs, ys):
                if not math.isfinite(yi) or xi < -width or xi > pw + width:
                    if len(run) > 1:
                        draw.line(run, fill=item["color"], width=width, joint="curve")
                    run = []
                    continue
                run.append((float(xi), float(yi)))
            if len(run) > 1:
                draw.line(run, fill=item["color"], width=width, joint="curve")
        composite_clipped(image, layer, int(round(self._plot.x)), int(round(self._plot.y)))

    def _draw_events(self, image: Image.Image, view_left: float, head_x: float) -> None:
        if not self.ctx.events:
            return
        if self.mode == "full":
            reached_mask = 0
            for index, event in enumerate(self.ctx.events):
                t = float(event.get("time", math.nan))
                if math.isfinite(t) and self._t_lo <= t <= self._t_hi and self._sx(t) <= head_x:
                    reached_mask |= (1 << index)
            if reached_mask in self._events_cache:
                cached = self._events_cache[reached_mask]
                if cached is not None:
                    composite_clipped(image, cached, int(round(self._plot.x)), int(round(self._plot.y)))
                return
            ev_layer = self._rasterize_events(view_left, head_x)
            self._events_cache[reached_mask] = ev_layer
            if ev_layer is not None:
                composite_clipped(image, ev_layer, int(round(self._plot.x)), int(round(self._plot.y)))
            return
        ev_layer = self._rasterize_events(view_left, head_x)
        if ev_layer is not None:
            composite_clipped(image, ev_layer, int(round(self._plot.x)), int(round(self._plot.y)))

    def _rasterize_events(self, view_left: float, head_x: float) -> Image.Image | None:
        scale = self.ctx.scale
        pw, ph = self._plot.size()
        ev_layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ev_layer)
        line_h = self._event_line_h
        any_drawn = False
        for index, event in enumerate(self.ctx.events):
            t = float(event.get("time", math.nan))
            if not math.isfinite(t) or not (self._t_lo <= t <= self._t_hi):
                continue
            lx = self._sx(t) - view_left
            if lx < -1 or lx > pw + 1:
                continue
            any_drawn = True
            color = parse_color(event.get("color", self.theme.accent))
            reached = self._sx(t) <= head_x
            alpha = 1.0 if reached else 0.35
            line_color = with_alpha(color, alpha * 0.85)
            width = max(1, int(round(scale)))
            dash, gap = 7 * scale, 6 * scale
            y = 0.0
            while y < ph:
                draw.line([(lx, y), (lx, min(y + dash, ph))], fill=line_color, width=width)
                y += dash + gap
            label = str(event.get("label", ""))
            if not label or not reached:
                continue
            lane = self._event_lanes.get(index, 0)
            ty = 4 * scale + lane * line_h
            tw = text_size(self.event_font, label)[0]
            if lx + tw + 8 * scale > pw:
                draw.text((lx - 5 * scale, ty), label, font=self.event_font,
                          fill=with_alpha(color, alpha), anchor="ra")
            else:
                draw.text((lx + 5 * scale, ty), label, font=self.event_font,
                          fill=with_alpha(color, alpha), anchor="la")
        return ev_layer if any_drawn else None

    def _draw_playhead(
        self, image: Image.Image, data_t: float, view_left: float, head_x: float
    ) -> None:
        x = head_x - view_left + self._plot.x
        if x < self._plot.x - 1 or x > self._plot.x1 + 1:
            return
        color = with_alpha(self.color("playhead_color", "accent"), 0.85)
        width = max(1, int(round(self.playhead_width * self.ctx.scale)))
        ph = max(1, int(math.ceil(self._plot.h)))
        strip = Image.new("RGBA", (width + 2, ph), (0, 0, 0, 0))
        ImageDraw.Draw(strip).line([(width // 2 + 1, 0), (width // 2 + 1, ph)], fill=color, width=width)
        composite_clipped(
            image, strip, int(round(x - width // 2 - 1)), int(round(self._plot.y))
        )

    def _draw_dots(
        self, image: Image.Image, data_t: float, view_left: float, head_x: float
    ) -> None:
        scale = self.ctx.scale
        x = head_x - view_left + self._plot.x
        if x < self._plot.x - 2 or x > self._plot.x1 + 2:
            return
        r = self.dot_size * scale
        sprites = getattr(self, "_dot_sprites", None)
        for idx_s, item in enumerate(self.series):
            if not item["dot"]:
                continue
            value = self.ctx.value_at(item["key"], data_t)
            if not math.isfinite(value):
                continue
            y = self._plot.y + self._py(value)
            if not (self._plot.y - 2 <= y <= self._plot.y1 + 2):
                continue
            if sprites and idx_s < len(sprites):
                dot_img = sprites[idx_s]
                composite_clipped(
                    image, dot_img,
                    int(round(x - dot_img.width / 2)), int(round(y - dot_img.height / 2)),
                )
            else:
                d_sz = max(4, int(math.ceil(r * 2 + 4)))
                dot_img = Image.new("RGBA", (d_sz * 2, d_sz * 2), (0, 0, 0, 0))
                dd = ImageDraw.Draw(dot_img)
                cx = d_sz
                rr = r * 2
                dd.ellipse((cx - rr, cx - rr, cx + rr, cx + rr), fill=item["color"])
                dd.ellipse((cx - rr * 0.45, cx - rr * 0.45, cx + rr * 0.45, cx + rr * 0.45),
                           fill=(255, 255, 255, 240))
                dot_img = dot_img.reduce(2)
                composite_clipped(image, dot_img, int(round(x - d_sz / 2)), int(round(y - d_sz / 2)))

            if self.show_value:
                text = format_value(value, self.value_format)
                if item["unit"]:
                    text = f"{text} {item['unit']}"
                shadow, offset, blur = self.text_shadow()
                tw, th = text_size(self.value_font, text)
                left = x + r * 2.2
                anchor = "lm"
                if left + tw > self._plot.x1:
                    left = x - r * 2.2
                    anchor = "rm"
                label_y = y
                if self.show_events and self._event_rows:
                    band = self._plot.y + 8 * scale + self._event_rows * self._event_line_h
                    if label_y - th * 0.6 < band:
                        label_y = max(y + r * 2.6 + th * 0.7, band + th * 0.8)
                        label_y = min(label_y, self._plot.y1 - th * 0.7)
                draw_text(
                    image, (left, label_y), text, self.value_font, item["color"],
                    anchor=anchor, shadow=shadow, shadow_offset=offset, shadow_blur=blur,
                )
