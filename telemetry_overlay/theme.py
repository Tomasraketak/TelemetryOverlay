"""Colours, geometry helpers and shared drawing primitives."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter

__all__ = [
    "parse_color", "Theme", "THEMES", "resolve_length", "Rect",
    "rounded_rect", "draw_text", "text_size", "soft_shadow", "PALETTES",
    "composite_clipped",
]

RGBA = tuple[int, int, int, int]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3,8})$")

# Colour words worth having without pulling in a full CSS table.
_NAMED: dict[str, str] = {
    "white": "#ffffff", "black": "#000000", "red": "#ff3b30",
    "green": "#34c759", "blue": "#0a84ff", "yellow": "#ffd60a",
    "orange": "#ff9f0a", "cyan": "#32d6e0", "magenta": "#ff2d92",
    "grey": "#8e8e93", "gray": "#8e8e93", "transparent": "#00000000",
    "amber": "#ffb800", "lime": "#b6f400", "violet": "#bf5af2",
    "teal": "#40c8b0", "pink": "#ff6482", "slate": "#64748b",
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
    # "#ff3b30 @ 60%" - a convenient way to dim a colour in a config file.
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


# Series palettes, tuned to stay readable over both bright flame and dark sky.
PALETTES: dict[str, list[str]] = {
    "default": ["#4ea8ff", "#ffb800", "#34c759", "#ff6482", "#bf5af2", "#40c8b0"],
    "rocket": ["#ff9f0a", "#4ea8ff", "#34c759", "#ff453a", "#bf5af2", "#ffd60a"],
    "mono": ["#ffffff", "#c8c8c8", "#9a9a9a", "#6e6e6e"],
    "hot": ["#ffd60a", "#ff9f0a", "#ff6b00", "#ff3b30", "#c81e12"],
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
        theme = THEMES.get(str(base), THEMES["night"]) if base else THEMES["night"]
        merged = {**theme.__dict__, **data}
        merged.pop("name", None)
        known = {k: v for k, v in merged.items() if k in cls.__dataclass_fields__}
        unknown = set(merged) - set(known)
        if unknown:
            raise ValueError(f"unknown theme keys: {', '.join(sorted(unknown))}")
        return cls(name=str(base or theme.name), **known)


THEMES: dict[str, Theme] = {
    # Default: white on a dark scrim, made for footage of any brightness.
    "night": Theme(),
    # For bright daylight footage - dark text, light scrim.
    "day": Theme(
        name="day", text="#101418", text_dim="#101418 @ 60%", accent="#c2410c",
        panel="#ffffff @ 70%", panel_border="#101418 @ 14%", grid="#101418 @ 12%",
        axis="#101418 @ 40%", shadow="#ffffff @ 55%", palette="default",
    ),
    # Instrument look: no panels, thin lines, monospaced numbers.
    "hud": Theme(
        name="hud", text="#d8faff", text_dim="#8ad4e0", accent="#32d6e0",
        panel="#001018 @ 30%", panel_border="#32d6e0 @ 35%", grid="#32d6e0 @ 16%",
        axis="#32d6e0 @ 45%", font="mono", font_bold="mono-bold",
        palette="default", corner_radius=2,
    ),
    # High-contrast, panel-free - for burning straight over bright flame.
    "flame": Theme(
        name="flame", text="#ffffff", text_dim="#ffe6c0", accent="#ffb800",
        panel="#000000 @ 0%", panel_border="#000000 @ 0%", grid="#ffffff @ 18%",
        axis="#ffffff @ 45%", shadow="#000000 @ 88%", shadow_blur=10,
        palette="hot", corner_radius=0,
    ),
    # Clean broadcast style.
    "clean": Theme(
        name="clean", text="#ffffff", text_dim="#ffffff @ 62%", accent="#4ea8ff",
        panel="#0b1020 @ 60%", panel_border="#ffffff @ 10%", corner_radius=14,
        palette="default",
    ),
}


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def resolve_length(value: Any, extent: int, default: float = 0.0) -> float:
    """Resolve a length that may be pixels, a percentage or a fraction.

    ``120`` -> 120 px, ``"8%"`` -> 8 % of ``extent``, ``0.08`` -> the same.
    Percentages keep a layout resolution independent, which matters because
    the same config is used for 1080p previews and 4K masters.
    """
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
    # A bare fraction below 1 is ambiguous; treat it as a fraction of extent
    # only when it is not an exact pixel count (0 and 1 stay pixels).
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
        return Rect(self.x + dx, self.y + dy, max(0.0, self.w - 2 * dx),
                    max(0.0, self.h - 2 * dy))

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (int(round(self.x)), int(round(self.y)),
                int(round(self.x1)), int(round(self.y1)))

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
# Drawing primitives
# ---------------------------------------------------------------------------

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
        draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill,
                               outline=outline, width=width)


def text_size(font, text: str) -> tuple[int, int]:
    from .fonts import measure
    return measure(font, text)


def composite_clipped(base: Image.Image, layer: Image.Image,
                      x: int, y: int) -> None:
    """Alpha-composite ``layer`` at (x, y), clipping to the base's bounds."""
    bx0, by0 = max(0, x), max(0, y)
    bx1 = min(base.width, x + layer.width)
    by1 = min(base.height, y + layer.height)
    if bx1 <= bx0 or by1 <= by0:
        return
    if (bx0, by0, bx1, by1) != (x, y, x + layer.width, y + layer.height):
        layer = layer.crop((bx0 - x, by0 - y, bx1 - x, by1 - y))
    base.alpha_composite(layer, (bx0, by0))


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
    """Draw text with an optional soft shadow.

    A blurred shadow is rendered on a scratch layer just big enough for the
    glyphs. Blurring the whole frame instead would dominate the render time -
    an overlay draws a dozen strings per frame.
    """
    if not text:
        return
    x, y = xy
    draw = ImageDraw.Draw(image)
    if shadow is not None and shadow[3] > 0:
        if shadow_blur > 0:
            box = draw.textbbox((0, 0), text, font=font, anchor=anchor,
                                stroke_width=stroke_width)
            pad = int(shadow_blur * 3 + 2)
            width = math.ceil(box[2] - box[0]) + pad * 2 + 1
            height = math.ceil(box[3] - box[1]) + pad * 2 + 1
            if width > 0 and height > 0:
                # Offset the scratch layer by whole pixels only, so the
                # fractional coordinate the rasteriser sees is exactly the one
                # a full-frame blur would have used and the glyphs land
                # identically.
                sx = x + shadow_offset[0]
                sy = y + shadow_offset[1]
                ix = math.floor(sx + box[0]) - pad
                iy = math.floor(sy + box[1]) - pad
                layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                ImageDraw.Draw(layer).text(
                    (sx - ix, sy - iy), text, font=font, fill=shadow,
                    anchor=anchor, stroke_width=stroke_width,
                )
                layer = layer.filter(ImageFilter.GaussianBlur(shadow_blur))
                composite_clipped(image, layer, ix, iy)
        else:
            draw.text(
                (x + shadow_offset[0], y + shadow_offset[1]), text, font=font,
                fill=shadow, anchor=anchor, stroke_width=stroke_width,
            )
    draw.text(
        (x, y), text, font=font, fill=fill, anchor=anchor,
        stroke_width=stroke_width, stroke_fill=stroke_fill,
    )


def soft_shadow(layer: Image.Image, blur: int, offset: tuple[int, int],
                color: RGBA) -> Image.Image:
    """Build a blurred drop shadow from a layer's alpha channel."""
    if blur <= 0 and offset == (0, 0):
        return Image.new("RGBA", layer.size, (0, 0, 0, 0))
    alpha = layer.split()[3]
    shadow = Image.new("RGBA", layer.size, color[:3] + (0,))
    scaled = alpha.point(lambda a: int(a * color[3] / 255))
    shadow.putalpha(scaled)
    if blur > 0:
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    if offset != (0, 0):
        shifted = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        shifted.paste(shadow, offset)
        shadow = shifted
    return shadow
