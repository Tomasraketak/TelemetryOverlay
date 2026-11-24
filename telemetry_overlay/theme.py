"""Colours, colormaps, geometry helpers and alpha-safe drawing primitives."""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter

__all__ = [
    "RGBA", "parse_color", "with_alpha", "mix", "colormap_color", "COLORMAPS",
    "Theme", "THEMES", "PALETTES", "resolve_length", "Rect", "anchor_offset",
    "rounded_rect", "make_panel_image", "draw_text", "text_size",
    "soft_shadow", "composite_clipped", "scale_alpha",
]

RGBA = tuple[int, int, int, int]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3,8})$")

_NAMED: dict[str, str] = {
    "white": "#ffffff", "black": "#000000", "red": "#ff3b30",
    "green": "#34c759", "blue": "#0a84ff", "yellow": "#ffd60a",
    "orange": "#ff9f0a", "cyan": "#32d6e0", "magenta": "#ff2d92",
    "grey": "#8e8e93", "gray": "#8e8e93", "transparent": "#00000000",
    "amber": "#ffb800", "lime": "#b6f400", "violet": "#bf5af2",
    "teal": "#40c8b0", "pink": "#ff6482", "slate": "#64748b",
    "purple": "#af52de", "gold": "#ffd700", "crimson": "#dc2626",
    "emerald": "#10b981", "sky": "#38bdf8", "chroma_green": "#00ff00",
    "chroma_blue": "#0000ff",
}


def parse_color(value: Any, default: RGBA = (255, 255, 255, 255)) -> RGBA:
    """Accept ``"#rgb"``, ``"#rrggbb"``, ``"#rrggbbaa"``, a name, or a sequence."""
    if value is None:
        return default
    if isinstance(value, (tuple, list)):
        parts = [int(round(float(v))) for v in value]
        if len(parts) == 3:
            parts.append(255)
        if len(parts) != 4:
            raise ValueError(f"colour needs 3 or 4 components, got {value!r}")
        return tuple(max(0, min(255, p)) for p in parts)  # type: ignore[return-value]
    if not isinstance(value, str):
        raise ValueError(f"cannot read colour from {value!r}")

    text = value.strip().lower()
    alpha_scale = 1.0
    if "@" in text:
        text, _, alpha_part = text.partition("@")
        text = text.strip()
        alpha_part = alpha_part.strip().rstrip("%")
        try:
            alpha_scale = float(alpha_part) / (100.0 if "%" in value else 1.0)
        except ValueError:
            alpha_scale = 1.0

    text = _NAMED.get(text, text)
    match = _HEX_RE.match(text)
    if not match:
        raise ValueError(f"unrecognised colour {value!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits) + "ff"
    elif len(digits) == 4:
        digits = "".join(c * 2 for c in digits)
    elif len(digits) == 6:
        digits += "ff"
    elif len(digits) != 8:
        raise ValueError(f"unrecognised colour {value!r}")
    r, g, b, a = (int(digits[i:i + 2], 16) for i in (0, 2, 4, 6))
    a = int(round(max(0.0, min(1.0, alpha_scale)) * a))
    return (r, g, b, a)


def with_alpha(color: RGBA, alpha: float) -> RGBA:
    """Scale a colour's alpha by a 0..1 factor."""
    r, g, b, a = color
    return (r, g, b, int(round(max(0.0, min(1.0, alpha)) * a)))


def mix(a: RGBA, b: RGBA, t: float) -> RGBA:
    """Linear blend between two colours."""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))  # type: ignore[return-value]


# Multi-stop colormaps for GPS track colouring, heatmaps, gradient bars & gauges
COLORMAPS: dict[str, list[RGBA]] = {
    "speed": [
        (37, 99, 235, 255),    # blue (slow)
        (16, 185, 129, 255),   # emerald
        (250, 204, 21, 255),   # yellow
        (249, 115, 22, 255),   # orange
        (239, 68, 68, 255),    # red (fast)
    ],
    "thermal": [
        (20, 12, 68, 255),
        (120, 28, 129, 255),
        (216, 65, 92, 255),
        (251, 159, 58, 255),
        (252, 255, 164, 255),
    ],
    "elevation": [
        (34, 197, 94, 255),    # valley green
        (234, 179, 8, 255),    # ridge gold
        (234, 88, 12, 255),    # highland rust
        (241, 245, 249, 255),  # summit snow
    ],
    "ocean": [
        (14, 116, 144, 255),
        (6, 182, 212, 255),
        (56, 189, 248, 255),
        (186, 230, 253, 255),
    ],
    "rdylgn": [
        (239, 68, 68, 255),
        (250, 204, 21, 255),
        (34, 197, 94, 255),
    ],
    "cyber": [
        (56, 189, 248, 255),
        (168, 85, 247, 255),
        (244, 63, 94, 255),
    ],
}


def colormap_color(t: float, name: str = "speed", alpha: float = 1.0) -> RGBA:
    """Sample a continuous colormap at ``t`` in ``[0, 1]``."""
    stops = COLORMAPS.get(name.lower(), COLORMAPS["speed"])
    t = max(0.0, min(1.0, float(t)))
    if len(stops) == 1:
        return with_alpha(stops[0], alpha)
    pos = t * (len(stops) - 1)
    idx = min(int(math.floor(pos)), len(stops) - 2)
    frac = pos - idx
    return with_alpha(mix(stops[idx], stops[idx + 1], frac), alpha)


# Series palettes, tuned to stay readable over both bright flame and dark sky.
PALETTES: dict[str, list[str]] = {
    "default": ["#4ea8ff", "#ffb800", "#34c759", "#ff6482", "#bf5af2", "#40c8b0"],
    "rocket": ["#ff9f0a", "#4ea8ff", "#34c759", "#ff453a", "#bf5af2", "#ffd60a"],
    "mono": ["#ffffff", "#c8c8c8", "#9a9a9a", "#6e6e6e"],
    "hot": ["#ffd60a", "#ff9f0a", "#ff6b00", "#ff3b30", "#c81e12"],
    "motorsport": ["#ef4444", "#22c55e", "#facc15", "#38bdf8", "#a855f7", "#f97316"],
    "aviation": ["#22ff66", "#fbbf24", "#38bdf8", "#f43f5e", "#e2e8f0"],
    "cycling": ["#fc4c02", "#38bdf8", "#ef4444", "#a855f7", "#22c55e"],
    "cyber": ["#32d6e0", "#ff2d92", "#b6f400", "#bf5af2", "#ffb800"],
    "ocean": ["#38bdf8", "#40c8b0", "#fbbf24", "#ffffff"],
}


@dataclass
class Theme:
    """Look and feel shared by every widget unless overridden."""

    name: str = "night"
    text: str = "#ffffff"
    text_dim: str = "#ffffff @ 55%"
    accent: str = "#ff9f0a"
    good: str = "#34c759"
    warn: str = "#ffd60a"
    bad: str = "#ff453a"
    panel: str = "#000000 @ 42%"
    panel_border: str = "#ffffff @ 16%"
    grid: str = "#ffffff @ 14%"
    axis: str = "#ffffff @ 38%"
    shadow: str = "#000000 @ 70%"
    font: str = "sans"
    font_bold: str = "sans-bold"
    font_mono: str = "mono-bold"
    palette: str = "default"
    corner_radius: int = 10
    shadow_blur: int = 6
    shadow_offset: tuple[int, int] = (0, 3)

    def color(self, key: str) -> RGBA:
        return parse_color(getattr(self, key))

    def series_colors(self) -> list[RGBA]:
        names = PALETTES.get(self.palette, PALETTES["default"])
        return [parse_color(c) for c in names]

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Theme":
        data = dict(data or {})
        base = data.pop("base", data.pop("preset", None))
        if base is not None and str(base) not in THEMES:
            raise ValueError(
                f"unknown theme base {base!r}; available: {', '.join(sorted(THEMES))}"
            )
        theme = THEMES.get(str(base), THEMES["night"]) if base else THEMES["night"]
        merged = {**theme.__dict__, **data}
        merged.pop("name", None)
        known = {k: v for k, v in merged.items() if k in cls.__dataclass_fields__}
        unknown = set(merged) - set(known)
        if unknown:
            raise ValueError(f"unknown theme keys: {', '.join(sorted(unknown))}")
        return cls(name=str(base or theme.name), **known)


THEMES: dict[str, Theme] = {
    "night": Theme(),
    "day": Theme(
        name="day", text="#101418", text_dim="#101418 @ 60%", accent="#c2410c",
        panel="#ffffff @ 70%", panel_border="#101418 @ 14%", grid="#101418 @ 12%",
        axis="#101418 @ 40%", shadow="#ffffff @ 55%", palette="default",
    ),
    "hud": Theme(
        name="hud", text="#d8faff", text_dim="#8ad4e0", accent="#32d6e0",
        panel="#001018 @ 38%", panel_border="#32d6e0 @ 35%", grid="#32d6e0 @ 16%",
        axis="#32d6e0 @ 45%", font="mono", font_bold="mono-bold",
        palette="default", corner_radius=2,
    ),
    "flame": Theme(
        name="flame", text="#ffffff", text_dim="#ffe6c0", accent="#ffb800",
        panel="#000000 @ 0%", panel_border="#000000 @ 0%", grid="#ffffff @ 18%",
        axis="#ffffff @ 45%", shadow="#000000 @ 88%", shadow_blur=10,
        palette="hot", corner_radius=0,
    ),
    "clean": Theme(
        name="clean", text="#ffffff", text_dim="#ffffff @ 62%", accent="#4ea8ff",
        panel="#0b1020 @ 60%", panel_border="#ffffff @ 12%", corner_radius=14,
        palette="default",
    ),
    "motorsport": Theme(
        name="motorsport", text="#f8fafc", text_dim="#94a3b8", accent="#ef4444",
        good="#22c55e", warn="#facc15", bad="#ef4444",
        panel="#090d16 @ 72%", panel_border="#ef4444 @ 32%",
        grid="#ffffff @ 12%", axis="#ffffff @ 36%",
        shadow="#000000 @ 80%", font="condensed-bold", font_bold="condensed-bold",
        font_mono="mono-bold", palette="motorsport", corner_radius=8, shadow_blur=5,
    ),
    "aviation": Theme(
        name="aviation", text="#66ff99", text_dim="#66ff99 @ 68%", accent="#22ff66",
        good="#22ff66", warn="#fbbf24", bad="#ff3b30",
        panel="#02140a @ 45%", panel_border="#22ff66 @ 42%",
        grid="#22ff66 @ 18%", axis="#22ff66 @ 55%",
        shadow="#000000 @ 85%", font="mono", font_bold="mono-bold",
        font_mono="mono-bold", palette="aviation", corner_radius=3, shadow_blur=4,
    ),
    "cycling": Theme(
        name="cycling", text="#ffffff", text_dim="#cbd5e1", accent="#fc4c02",
        good="#22c55e", warn="#facc15", bad="#ef4444",
        panel="#0f172a @ 62%", panel_border="#ffffff @ 14%",
        grid="#ffffff @ 14%", axis="#ffffff @ 38%",
        shadow="#000000 @ 75%", palette="cycling", corner_radius=12, shadow_blur=5,
    ),
    "neon": Theme(
        name="neon", text="#f0fdff", text_dim="#a5f3fc", accent="#32d6e0",
        good="#b6f400", warn="#ffb800", bad="#ff2d92",
        panel="#090418 @ 64%", panel_border="#32d6e0 @ 45%",
        grid="#bf5af2 @ 20%", axis="#32d6e0 @ 50%",
        shadow="#000000 @ 85%", palette="cyber", corner_radius=10, shadow_blur=8,
    ),
    "minimal_light": Theme(
        name="minimal_light", text="#0f172a", text_dim="#475569", accent="#0284c7",
        panel="#f8fafc @ 78%", panel_border="#0f172a @ 12%",
        grid="#0f172a @ 10%", axis="#0f172a @ 35%",
        shadow="#ffffff @ 40%", shadow_blur=3, palette="ocean", corner_radius=12,
    ),
}


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def resolve_length(value: Any, extent: int, default: float = 0.0) -> float:
    """Resolve a length that may be pixels, a percentage or a fraction."""
    if value is None:
        return float(default)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            return float(text[:-1]) * extent / 100.0
        if text.endswith("px"):
            return float(text[:-2])
        value = float(text)
    value = float(value)
    if 0.0 < abs(value) < 1.0:
        return value * extent
    return value


@dataclass
class Rect:
    """An axis-aligned rectangle in pixels."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def inset(self, dx: float, dy: float | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(
            self.x + dx, self.y + dy,
            max(0.0, self.w - 2 * dx), max(0.0, self.h - 2 * dy),
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (
            int(round(self.x)), int(round(self.y)),
            int(round(self.x1)), int(round(self.y1)),
        )

    def size(self) -> tuple[int, int]:
        return max(1, int(round(self.w))), max(1, int(round(self.h)))


def anchor_offset(anchor: str, w: float, h: float) -> tuple[float, float]:
    """Pixel offset that moves a box so ``anchor`` sits at the origin."""
    anchor = (anchor or "top-left").strip().lower().replace("_", "-")
    vertical, _, horizontal = anchor.partition("-")
    if not horizontal:
        vertical, horizontal = "top", vertical
    dx = {"left": 0.0, "center": -w / 2.0, "centre": -w / 2.0, "right": -w}.get(horizontal)
    dy = {"top": 0.0, "middle": -h / 2.0, "center": -h / 2.0, "bottom": -h}.get(vertical)
    if dx is None or dy is None:
        raise ValueError(
            f"unknown anchor {anchor!r}; use e.g. top-left, middle-center, bottom-right"
        )
    return dx, dy


# ---------------------------------------------------------------------------
# Alpha-safe drawing primitives
# ---------------------------------------------------------------------------

def scale_alpha(layer: Image.Image, alpha: float) -> Image.Image:
    """Scale an RGBA image's alpha channel using a fast 256-entry LUT."""
    alpha = max(0.0, min(1.0, float(alpha)))
    if alpha >= 0.999:
        return layer
    out = layer.copy()
    lut = [int(round(i * alpha)) for i in range(256)]
    out.putalpha(out.getchannel("A").point(lut))
    return out


def rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    radius: float,
    fill: RGBA | None = None,
    outline: RGBA | None = None,
    width: int = 1,
) -> None:
    """Rounded rectangle that degrades to a plain one when radius is 0."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    radius = int(max(0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2)))
    if fill is not None and fill[3] == 0:
        fill = None
    if outline is not None and outline[3] == 0:
        outline = None
    if fill is None and outline is None:
        return
    if radius <= 0:
        draw.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=width)
    else:
        draw.rounded_rectangle(
            (x0, y0, x1, y1), radius=radius, fill=fill, outline=outline, width=width
        )


def make_panel_image(
    w: int,
    h: int,
    radius: float,
    fill: RGBA | None,
    border: RGBA | None,
    border_width: int = 1,
) -> Image.Image | None:
    """Pre-render a translucent rounded panel onto its own RGBA layer.

    Compositing this cached layer via ``composite_clipped`` ensures proper
    Porter-Duff source-over blending (unlike ``ImageDraw.Draw(image)`` directly,
    which overwrites canvas alpha and punches holes into opaque backgrounds).
    """
    w, h = int(round(w)), int(round(h))
    if w <= 0 or h <= 0:
        return None
    has_fill = fill is not None and fill[3] > 0
    has_border = border is not None and border[3] > 0
    if not has_fill and not has_border:
        return None
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rounded_rect(
        draw,
        (0, 0, w - 1, h - 1),
        radius,
        fill=fill if has_fill else None,
        outline=border if has_border else None,
        width=max(1, int(border_width)),
    )
    return layer


def text_size(font, text: str) -> tuple[int, int]:
    from .fonts import measure
    return measure(font, text)


def composite_clipped(
    base: Image.Image, layer: Image.Image, x: int, y: int
) -> None:
    """Alpha-composite ``layer`` at (x, y), safely clipping to ``base``'s bounds."""
    bx0, by0 = max(0, x), max(0, y)
    bx1 = min(base.width, x + layer.width)
    by1 = min(base.height, y + layer.height)
    if bx1 <= bx0 or by1 <= by0:
        return
    if (bx0, by0, bx1, by1) != (x, y, x + layer.width, y + layer.height):
        layer = layer.crop((bx0 - x, by0 - y, bx1 - x, by1 - y))
    base.alpha_composite(layer, (bx0, by0))


@functools.lru_cache(maxsize=384)
def _cached_text_layer(
    text: str,
    font_key: tuple[str, int],
    fill: RGBA,
    anchor: str,
    shadow: RGBA | None,
    shadow_offset: tuple[int, int],
    shadow_blur: int,
    stroke_width: int,
    stroke_fill: RGBA | None,
) -> tuple[Image.Image, int, int] | None:
    """Render text (and optional blurred shadow) once onto a tight RGBA sprite.

    Returns ``(sprite, origin_x, origin_y)`` where ``(origin_x, origin_y)`` is
    the anchor point inside the sprite, so placing the sprite at
    ``(round(x) - origin_x, round(y) - origin_y)`` matches direct drawing.
    """
    from .fonts import get_font
    font = get_font(font_key[0], font_key[1])
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = dummy.textbbox((0, 0), text, font=font, anchor=anchor, stroke_width=stroke_width)
    tw = math.ceil(box[2] - box[0])
    th = math.ceil(box[3] - box[1])
    if tw <= 0 or th <= 0:
        return None
    has_shadow = shadow is not None and shadow[3] > 0
    pad_l = (int(shadow_blur * 3) + max(0, -shadow_offset[0]) + 2) if has_shadow else (2 + stroke_width)
    pad_r = (int(shadow_blur * 3) + max(0, shadow_offset[0]) + 2) if has_shadow else (2 + stroke_width)
    pad_t = (int(shadow_blur * 3) + max(0, -shadow_offset[1]) + 2) if has_shadow else (2 + stroke_width)
    pad_b = (int(shadow_blur * 3) + max(0, shadow_offset[1]) + 2) if has_shadow else (2 + stroke_width)
    w = tw + pad_l + pad_r + 1
    h = th + pad_t + pad_b + 1
    ox = pad_l - math.floor(box[0])
    oy = pad_t - math.floor(box[1])

    sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if has_shadow:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).text(
            (ox + shadow_offset[0], oy + shadow_offset[1]), text, font=font,
            fill=shadow, anchor=anchor, stroke_width=stroke_width,
        )
        if shadow_blur > 0:
            sh = sh.filter(ImageFilter.GaussianBlur(shadow_blur))
        sprite.alpha_composite(sh)
    fg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(fg).text(
        (ox, oy), text, font=font, fill=fill, anchor=anchor,
        stroke_width=stroke_width, stroke_fill=stroke_fill,
    )
    sprite.alpha_composite(fg)
    return sprite, ox, oy


def draw_text(
    image: Image.Image,
    xy: tuple[float, float],
    text: str,
    font,
    fill: RGBA,
    anchor: str = "la",
    shadow: RGBA | None = None,
    shadow_offset: tuple[int, int] = (0, 2),
    shadow_blur: int = 0,
    stroke_width: int = 0,
    stroke_fill: RGBA | None = None,
) -> None:
    """Draw text with proper alpha-composited shadow, cached across frames."""
    if not text:
        return
    x, y = xy
    font_path = getattr(font, "path", None)
    font_size = getattr(font, "size", None)
    if font_path and font_size:
        cached = _cached_text_layer(
            text, (str(font_path), int(font_size)), fill, anchor,
            shadow if (shadow is not None and shadow[3] > 0) else None,
            (int(shadow_offset[0]), int(shadow_offset[1])),
            int(shadow_blur), int(stroke_width), stroke_fill,
        )
        if cached is not None:
            sprite, ox, oy = cached
            composite_clipped(image, sprite, int(round(x)) - ox, int(round(y)) - oy)
        return

    # Fallback for bitmap default font
    draw = ImageDraw.Draw(image)
    if shadow is not None and shadow[3] > 0:
        draw.text(
            (x + shadow_offset[0], y + shadow_offset[1]), text, font=font,
            fill=shadow, anchor=anchor, stroke_width=stroke_width,
        )
    draw.text(
        (x, y), text, font=font, fill=fill, anchor=anchor,
        stroke_width=stroke_width, stroke_fill=stroke_fill,
    )


def soft_shadow(
    layer: Image.Image, blur: int, offset: tuple[int, int], color: RGBA
) -> Image.Image:
    """Build a blurred drop shadow from a layer's alpha channel."""
    if blur <= 0 and offset == (0, 0):
        return Image.new("RGBA", layer.size, (0, 0, 0, 0))
    alpha = layer.getchannel("A")
    shadow = Image.new("RGBA", layer.size, color[:3] + (0,))
    lut = [int(round(a * color[3] / 255)) for a in range(256)]
    shadow.putalpha(alpha.point(lut))
    if blur > 0:
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    if offset != (0, 0):
        shifted = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        shifted.paste(shadow, offset)
        shadow = shifted
    return shadow
