"""Text based widgets: labels, single readouts, stacked tables and clocks.

Static elements (labels, captions, panels, table row names) are rasterised
once during :meth:`prepare` so per-frame rendering only updates changing numbers.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from PIL import Image, ImageDraw

from ..theme import (
    RGBA, Rect, composite_clipped, draw_text, parse_color, text_size, with_alpha,
)
from .base import (
    RenderContext, Widget, format_value, parse_zones, peak_value, register,
    zone_color,
)

__all__ = ["LabelWidget", "ReadoutWidget", "TableWidget", "TimerWidget"]


def _fmt_clock(
    seconds: float,
    style: str = "t+",
    decimals: int = 2,
    start_epoch: float | None = None,
) -> str:
    """Format a timestamp as a mission clock or real wall-clock UTC time."""
    if not math.isfinite(seconds):
        return "--"
    if style in ("utc", "clock", "datetime", "time"):
        if start_epoch is not None:
            t_dt = dt.datetime.fromtimestamp(start_epoch + seconds, tz=dt.timezone.utc)
            if style == "datetime":
                return t_dt.strftime("%Y-%m-%d %H:%M:%S")
            if decimals > 0:
                frac = f"{t_dt.microsecond / 1e6:.{decimals}f}"[1:]
                return t_dt.strftime("%H:%M:%S") + frac
            return t_dt.strftime("%H:%M:%S")
        style = "hhmmss"

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


def _render_text_sprite(
    text: str,
    font,
    color: RGBA,
    shadow: RGBA | None,
    offset: tuple[int, int],
    blur: int,
    tracking: float = 0.0,
) -> tuple[Image.Image, int, int, int, int]:
    """Pre-render a text string (with tracking & blurred shadow) onto a tight sprite.

    Returns ``(sprite, pad_left, pad_top, text_width, text_height)``.
    """
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    if tracking > 0 and len(text) > 1:
        boxes = [dummy.textbbox((0, 0), ch, font=font, anchor="la") for ch in text]
        adv = [max(1, int(math.ceil(b[2] - b[0]))) for b in boxes]
        total_w = sum(adv) + int(math.ceil(tracking * (len(text) - 1)))
        min_x = min(b[0] for b in boxes)
        min_y = min(b[1] for b in boxes)
        max_y = max(b[3] for b in boxes)
        total_h = max(1, int(math.ceil(max_y - min_y)))
    else:
        b = dummy.textbbox((0, 0), text, font=font, anchor="la")
        min_x, min_y = b[0], b[1]
        total_w = max(1, int(math.ceil(b[2] - b[0])))
        total_h = max(1, int(math.ceil(b[3] - b[1])))
        adv = []

    pad = int(blur * 3 + abs(offset[0]) + abs(offset[1]) + 4) if (shadow and shadow[3] > 0) else 4
    w = total_w + pad * 2 + 2
    h = total_h + pad * 2 + 2
    ox = pad - int(math.floor(min_x))
    oy = pad - int(math.floor(min_y))

    sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if tracking > 0 and len(text) > 1:
        # Draw all glyphs onto ONE shadow layer & ONE fill layer so blur runs ONCE!
        if shadow is not None and shadow[3] > 0:
            sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            sd = ImageDraw.Draw(sh)
            cx = float(ox + offset[0])
            for ch, aw in zip(text, adv):
                sd.text((cx, oy + offset[1]), ch, font=font, fill=shadow, anchor="la")
                cx += aw + tracking
            if blur > 0:
                from PIL import ImageFilter
                sh = sh.filter(ImageFilter.GaussianBlur(blur))
            sprite.alpha_composite(sh)
        fg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fg)
        cx = float(ox)
        for ch, aw in zip(text, adv):
            fd.text((cx, oy), ch, font=font, fill=color, anchor="la")
            cx += aw + tracking
        sprite.alpha_composite(fg)
    else:
        draw_text(
            sprite, (ox, oy), text, font, color, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
    return sprite, ox, oy, total_w, total_h


@register("label")
class LabelWidget(Widget):
    """Static (or template-driven) text - a title, motor designation, or speed badge."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.text = str(spec.get("text", ""))
        self.size = float(spec.get("size", 34))
        self.align = str(spec.get("align", "left")).lower()
        self.font = ctx.font(str(spec.get("font", self.theme.font_bold)), self.size)
        self.tracking = float(spec.get("tracking", 0.0))
        self.uppercase = bool(spec.get("uppercase", False))
        self._dynamic = any(tok in self.text for tok in ("{speed", "{time", "{t}", "{t:", "{event"))
        self._sprite: Image.Image | None = None
        self._sprite_pos: tuple[int, int] = (0, 0)

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def _expand_static(self, text: str) -> str:
        # Resolve {meta.key} placeholders once at prepare time
        if "{meta." not in text:
            return text
        out = text
        for k, v in self.ctx.telemetry.meta.items():
            out = out.replace(f"{{meta.{k}}}", str(v))
        return out

    def _draw_x(self) -> float:
        if self.align in ("center", "centre"):
            return self.rect.x + self.rect.w / 2 if self.rect.w else self.rect.x
        if self.align == "right":
            return self.rect.x1 if self.rect.w else self.rect.x
        return self.rect.x

    def prepare(self) -> None:
        self._sprite = None
        if self._dynamic:
            return
        text = self._expand_static(self.text)
        if self.uppercase:
            text = text.upper()
        if not text:
            return
        shadow, offset, blur = self.text_shadow()
        color = self.color("color", "text")
        spacing = self.tracking * self.ctx.scale
        sprite, ox, oy, tw, _ = _render_text_sprite(
            text, self.font, color, shadow, offset, blur, tracking=spacing
        )
        x = self._draw_x()
        if self.align in ("center", "centre"):
            left = x - tw / 2.0
        elif self.align == "right":
            left = x - tw
        else:
            left = x
        self._sprite = sprite
        self._sprite_pos = (int(round(left - ox)), int(round(self.rect.y - oy)))

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        if self._sprite is not None:
            composite_clipped(image, self._sprite, self._sprite_pos[0], self._sprite_pos[1])
            return
        if not self._dynamic:
            return
        spd = self.ctx.timeline.speed_at(data_t)
        active_ev = ""
        for ev in self.ctx.events:
            if abs(float(ev.get("time", -1e9)) - data_t) <= 0.35:
                active_ev = str(ev.get("label", ""))
                break
        try:
            text = self._expand_static(self.text).format(
                time=data_t - self.ctx.t0, t=data_t - self.ctx.t0,
                speed=spd, event=active_ev,
            )
        except (ValueError, KeyError):
            text = self.text
        if self.uppercase:
            text = text.upper()
        if not text:
            return
        shadow, offset, blur = self.text_shadow()
        color = self.color("color", "text")
        anchor = {"left": "la", "center": "ma", "centre": "ma", "right": "ra"}.get(self.align, "la")
        draw_text(
            image, (self._draw_x(), self.rect.y), text, self.font, color,
            anchor=anchor, shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )


@register("readout")
class ReadoutWidget(Widget):
    """A big number (or text channel) with a caption, unit, zones and peak hold."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.channel = spec.get("channel")
        self.expression = spec.get("expression")
        if not self.channel and not self.expression:
            raise ValueError("readout needs a 'channel' or an 'expression'")
        if self.expression is not None:
            self.channel = ctx.ensure_expression(
                str(self.expression),
                unit=str(spec.get("unit", "")),
                label=str(spec.get("label", "")),
            )
        else:
            self.channel = self.resolve_channel(self.channel, "readout channel")
        source = ctx.telemetry.get(self.channel)
        self.is_text = bool(source and source.is_text)
        self.label = str(spec.get("label", source.label if source else ""))
        self.unit = str(spec.get("unit", source.unit if source else ""))
        self.format = str(spec.get("format", "{:.1f}"))
        self.value_size = float(spec.get("value_size", 62))
        self.label_size = float(spec.get("label_size", self.value_size * 0.30))
        self.unit_size = float(spec.get("unit_size", self.value_size * 0.42))
        self.align = str(spec.get("align", "left")).lower()
        self.interpolate = bool(spec.get("interpolate", True))
        self.peak_hold = bool(spec.get("peak_hold", False))
        self.peak_mode = str(spec.get("peak_mode", "max"))
        self.zones = parse_zones(spec.get("zones"))
        self.value_font = ctx.font(
            str(spec.get("value_font", self.theme.font_mono)), self.value_size)
        self.label_font = ctx.font(
            str(spec.get("label_font", self.theme.font)), self.label_size)
        self.unit_font = ctx.font(
            str(spec.get("unit_font", self.theme.font)), self.unit_size)
        self.padding = ctx.px_x(spec.get("padding", 0.0))
        self._label_sprite: Image.Image | None = None
        self._label_pos: tuple[int, int] = (0, 0)
        self._label_step = 0.0
        self._unit_sprite: Image.Image | None = None
        self._unit_ox = 0
        self._unit_oy = 0
        self._unit_w = 0
        self._unit_h = 0

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def _anchor_x(self) -> float:
        inner = self.rect.inset(self.padding) if self.padding else self.rect
        if self.align in ("center", "centre"):
            return inner.cx if inner.w else inner.x
        if self.align == "right":
            return inner.x1 if inner.w else inner.x
        return inner.x

    def prepare(self) -> None:
        shadow, offset, blur = self.text_shadow()
        inner = self.rect.inset(self.padding) if self.padding else self.rect
        x = self._anchor_x()
        y = inner.y
        self._label_sprite = None
        self._label_step = 0.0
        if self.label:
            lbl = self.label.upper()
            sp, ox, oy, tw, _ = _render_text_sprite(
                lbl, self.label_font, self.color("label_color", "text_dim"),
                shadow, offset, blur,
            )
            if self.align in ("center", "centre"):
                lx = x - tw / 2.0
            elif self.align == "right":
                lx = x - tw
            else:
                lx = x
            self._label_sprite = sp
            self._label_pos = (int(round(lx - ox)), int(round(y - oy)))
            self._label_step = text_size(self.label_font, "Xg")[1] * 1.55

        self._unit_sprite = None
        if self.unit:
            sp, ox, oy, tw, _ = _render_text_sprite(
                self.unit, self.unit_font, self.color("unit_color", "text_dim"),
                shadow, offset, blur,
            )
            self._unit_sprite = sp
            self._unit_ox = ox
            self._unit_oy = oy
            self._unit_w = text_size(self.unit_font, self.unit)[0]
            self._unit_h = text_size(self.unit_font, "X")[1]

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        if self._label_sprite is not None:
            composite_clipped(image, self._label_sprite, self._label_pos[0], self._label_pos[1])

        inner = self.rect.inset(self.padding) if self.padding else self.rect
        x = self._anchor_x()
        y = inner.y + self._label_step

        if self.is_text:
            text = self.ctx.text_at(str(self.channel), data_t) or "--"
            color = self.color("color", "text")
        else:
            if self.peak_hold:
                value = peak_value(self.ctx, str(self.channel), data_t, self.peak_mode)
            else:
                value = float(self.ctx.value_at(str(self.channel), data_t, self.interpolate))
            text = format_value(value, self.format)
            color = zone_color(value, self.zones, self.color("color", "text"))

        shadow, offset, blur = self.text_shadow()
        value_w, value_h = text_size(self.value_font, text)
        gap = self.unit_size * 0.35 * self.ctx.scale if self.unit else 0
        total_w = value_w + gap + self._unit_w

        if self.align in ("center", "centre"):
            value_x = x - total_w / 2.0
        elif self.align == "right":
            value_x = x - total_w
        else:
            value_x = x

        draw_text(
            image, (value_x, y), text, self.value_font, color, anchor="la",
            shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
        if self._unit_sprite is not None:
            unit_y = y + value_h - self._unit_h
            composite_clipped(
                image, self._unit_sprite,
                int(round(value_x + value_w + gap - self._unit_ox)),
                int(round(unit_y - self._unit_oy)),
            )


@register("table")
class TableWidget(Widget):
    """Several channels stacked as label/value rows (panel & labels cached)."""

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
            expr = row.get("expression")
            channel = row.get("channel")
            if expr is not None:
                channel = ctx.ensure_expression(
                    str(expr), unit=str(row.get("unit", "")), label=str(row.get("label", ""))
                )
            elif channel:
                channel = self.resolve_channel(channel, "table row")
            source = ctx.telemetry.get(channel) if channel else None
            self.rows.append({
                "channel": channel,
                "is_text": bool(source and source.is_text),
                "label": str(row.get("label", source.label if source else channel or "")),
                "unit": str(row.get("unit", source.unit if source else "")),
                "format": str(row.get("format", "{:.2f}")),
                "color": parse_color(row["color"]) if "color" in row else None,
                "zones": parse_zones(row.get("zones")),
                "peak_hold": bool(row.get("peak_hold", False)),
                "peak_mode": str(row.get("peak_mode", "max")),
            })
        self.size = float(spec.get("size", 30))
        self.row_gap = float(spec.get("row_gap", self.size * 0.55))
        self.padding = float(spec.get("padding", 18))
        self.label_font = ctx.font(str(spec.get("font", self.theme.font)), self.size)
        self.value_font = ctx.font(
            str(spec.get("value_font", self.theme.font_mono)), self.size)
        self._static_labels: Image.Image | None = None
        self._step = 0.0

    def default_width(self, ctx: RenderContext) -> float:
        return ctx.width * 0.22

    def default_height(self, ctx: RenderContext) -> float:
        rows = self.spec.get("rows") or self.spec.get("channels") or []
        size = float(self.spec.get("size", 30))
        gap = float(self.spec.get("row_gap", size * 0.55))
        padding = float(self.spec.get("padding", 18))
        return (len(rows) * (size + gap) + padding * 2) * ctx.scale

    def prepare(self) -> None:
        w, h = self.rect.size()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        panel = self.build_panel_layer(w, h)
        if panel is not None:
            layer.alpha_composite(panel)
        shadow, offset, blur = self.text_shadow()
        pad = self.padding * self.ctx.scale
        label_color = self.color("label_color", "text_dim")
        line_h = text_size(self.value_font, "Xg")[1]
        self._step = line_h + self.row_gap * self.ctx.scale
        y = pad
        for row in self.rows:
            draw_text(
                layer, (pad, y), row["label"], self.label_font, label_color,
                anchor="la", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
            y += self._step
        self._static_labels = layer

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        if self._static_labels is not None:
            composite_clipped(
                image, self._static_labels,
                int(round(self.rect.x)), int(round(self.rect.y)),
            )
        shadow, offset, blur = self.text_shadow()
        pad = self.padding * self.ctx.scale
        value_color = self.color("color", "text")
        y = self.rect.y + pad
        for row in self.rows:
            if row["is_text"]:
                text = self.ctx.text_at(str(row["channel"]), data_t) or "--"
                col = row["color"] or value_color
            else:
                if row["peak_hold"] and row["channel"]:
                    value = peak_value(self.ctx, str(row["channel"]), data_t, row["peak_mode"])
                else:
                    value = self.ctx.value_at(str(row["channel"]), data_t)
                text = format_value(value, row["format"])
                if row["unit"]:
                    text = f"{text} {row['unit']}"
                col = zone_color(value, row["zones"], row["color"] or value_color)
            draw_text(
                image, (self.rect.x1 - pad, y), text, self.value_font, col,
                anchor="ra", shadow=shadow, shadow_offset=offset, shadow_blur=blur,
            )
            y += self._step


@register("timer")
class TimerWidget(Widget):
    """A mission clock or UTC wall-clock driven by data time."""

    def __init__(self, spec: dict[str, Any], ctx: RenderContext):
        super().__init__(spec, ctx)
        self.style = str(spec.get("style", "t+")).lower()
        self.decimals = int(spec.get("decimals", 2))
        self.size = float(spec.get("size", 56))
        self.label = str(spec.get("label", ""))
        self.label_size = float(spec.get("label_size", self.size * 0.32))
        self.align = str(spec.get("align", "left")).lower()
        self.font = ctx.font(str(spec.get("font", self.theme.font_mono)), self.size)
        self.label_font = ctx.font(str(spec.get("label_font", self.theme.font)), self.label_size)
        self.padding = float(spec.get("padding", 0))
        self.zero = spec.get("zero")
        self.show_speed = bool(spec.get("show_speed", False))
        self._label_sprite: Image.Image | None = None
        self._label_pos: tuple[int, int] = (0, 0)
        self._label_step = 0.0

    def default_width(self, ctx: RenderContext) -> float:
        return 0.0

    def default_height(self, ctx: RenderContext) -> float:
        return 0.0

    def _anchor_x(self) -> float:
        pad = self.padding * self.ctx.scale
        if self.align in ("center", "centre"):
            return self.rect.cx if self.rect.w else self.rect.x
        if self.align == "right":
            return self.rect.x1 - pad
        return self.rect.x + pad

    def prepare(self) -> None:
        self._label_sprite = None
        self._label_step = 0.0
        if not self.label:
            return
        shadow, offset, blur = self.text_shadow()
        sp, ox, oy, tw, _ = _render_text_sprite(
            self.label.upper(), self.label_font,
            self.color("label_color", "text_dim"), shadow, offset, blur,
        )
        x = self._anchor_x()
        y = self.rect.y + self.padding * self.ctx.scale
        if self.align in ("center", "centre"):
            lx = x - tw / 2.0
        elif self.align == "right":
            lx = x - tw
        else:
            lx = x
        self._label_sprite = sp
        self._label_pos = (int(round(lx - ox)), int(round(y - oy)))
        self._label_step = text_size(self.label_font, "Xg")[1] * 1.55

    def render(self, image: Image.Image, data_t: float, frame: int) -> None:
        self.draw_panel(image)
        if self._label_sprite is not None:
            composite_clipped(image, self._label_sprite, self._label_pos[0], self._label_pos[1])

        zero = float(self.zero) if self.zero is not None else self.ctx.t0
        start_ep = self.ctx.telemetry.meta.get("start_epoch")
        start_ep_f = float(start_ep) if isinstance(start_ep, (int, float)) else None
        text = _fmt_clock(data_t - zero, self.style, self.decimals, start_epoch=start_ep_f)
        if self.show_speed:
            spd = self.ctx.timeline.speed_at(data_t)
            if abs(spd - 1.0) > 0.04:
                text = f"{text}  ({spd:.2g}x)"

        shadow, offset, blur = self.text_shadow()
        anchor = {"left": "la", "center": "ma", "centre": "ma", "right": "ra"}.get(self.align, "la")
        x = self._anchor_x()
        y = self.rect.y + self.padding * self.ctx.scale + self._label_step
        draw_text(
            image, (x, y), text, self.font, self.color("color", "text"),
            anchor=anchor, shadow=shadow, shadow_offset=offset, shadow_blur=blur,
        )
