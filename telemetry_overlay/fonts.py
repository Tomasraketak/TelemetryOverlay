"""Locating TrueType fonts across platforms, with caching.

Rendering happens in worker processes, so fonts are looked up by name and
cached per process rather than passed around as objects.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

from PIL import ImageFont

__all__ = ["get_font", "resolve_family", "available_families", "measure"]

# Search roots per platform, first match wins.
_FONT_DIRS: tuple[Path, ...] = tuple(
    Path(p) for p in (
        # Linux
        "/usr/share/fonts", "/usr/local/share/fonts", Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        # macOS
        "/System/Library/Fonts", "/Library/Fonts", Path.home() / "Library/Fonts",
        # Windows
        "C:/Windows/Fonts", Path(sys.prefix) / "Fonts",
    )
)

# Preferred file stems for each logical family, in descending order of taste.
# DejaVu is last everywhere because it ships with almost every Linux image and
# covers Czech diacritics, so it makes a dependable floor.
_FAMILY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "sans": (
        "Inter-Regular", "Roboto-Regular", "Helvetica", "Arial",
        "LiberationSans-Regular", "FreeSans", "DejaVuSans",
    ),
    "sans-bold": (
        "Inter-Bold", "Roboto-Bold", "Helvetica-Bold", "Arial-Bold", "arialbd",
        "LiberationSans-Bold", "FreeSansBold", "DejaVuSans-Bold",
    ),
    "mono": (
        "JetBrainsMono-Regular", "RobotoMono-Regular", "Menlo", "Consolas",
        "LiberationMono-Regular", "FreeMono", "DejaVuSansMono",
    ),
    "mono-bold": (
        "JetBrainsMono-Bold", "RobotoMono-Bold", "Menlo-Bold", "consolab",
        "LiberationMono-Bold", "FreeMonoBold", "DejaVuSansMono-Bold",
    ),
    "condensed": (
        "RobotoCondensed-Regular", "Oswald-Regular", "HelveticaNeue-Condensed",
        "LiberationSansNarrow-Regular", "DejaVuSansCondensed", "DejaVuSans",
    ),
    "condensed-bold": (
        "RobotoCondensed-Bold", "Oswald-Bold", "LiberationSansNarrow-Bold",
        "DejaVuSansCondensed-Bold", "DejaVuSans-Bold",
    ),
}

_EXTENSIONS = (".ttf", ".otf", ".ttc")


@functools.lru_cache(maxsize=1)
def _font_index() -> dict[str, Path]:
    """Map lower-cased file stems to font paths, scanning once per process."""
    index: dict[str, Path] = {}
    for root in _FONT_DIRS:
        try:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.suffix.lower() in _EXTENSIONS:
                    index.setdefault(path.stem.lower(), path)
        except (OSError, PermissionError):
            continue
    return index


@functools.lru_cache(maxsize=64)
def resolve_family(name: str) -> str | None:
    """Turn a family name or file path into a usable font file path."""
    if not name:
        return None
    # An explicit path always wins.
    candidate = Path(name).expanduser()
    if candidate.suffix.lower() in _EXTENSIONS and candidate.is_file():
        return str(candidate)

    index = _font_index()
    key = name.strip().lower()

    # A logical family from the table above.
    for stem in _FAMILY_CANDIDATES.get(key, ()):
        hit = index.get(stem.lower())
        if hit:
            return str(hit)

    # A concrete family name, e.g. "DejaVuSans" or "Roboto-Bold".
    direct = index.get(key) or index.get(key.replace(" ", ""))
    if direct:
        return str(direct)
    # Loose match: "roboto condensed" -> "RobotoCondensed-Regular".
    needle = key.replace(" ", "").replace("-", "").replace("_", "")
    for stem, path in index.items():
        flat = stem.replace(" ", "").replace("-", "").replace("_", "")
        if flat == needle or flat.startswith(needle + "regular"):
            return str(path)
    return None


def available_families() -> list[str]:
    """Logical families that actually resolve on this machine."""
    return [name for name in _FAMILY_CANDIDATES if resolve_family(name)]


@functools.lru_cache(maxsize=512)
def get_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a font, falling back through the family table then to Pillow's default."""
    size = max(1, int(round(size)))
    for name in (family, "sans", "DejaVuSans"):
        path = resolve_family(name)
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Last resort: a bitmap font that ignores `size`, but never crashes a render.
    return ImageFont.load_default()


def measure(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    """Pixel width and height of ``text`` as it will actually be drawn."""
    if not text:
        return 0, 0
    try:
        left, top, right, bottom = font.getbbox(text)
        return int(right - left), int(bottom - top)
    except AttributeError:  # pragma: no cover - very old Pillow
        return font.getsize(text)
