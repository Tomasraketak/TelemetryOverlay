"""Transparent telemetry overlays for video editing.

Reads a CSV/XLSX log, renders high resolution RGBA frames with readouts,
gauges and live plots, and writes a PNG sequence that drops straight into
DaVinci Resolve (including the free edition) on a track above the footage.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .config import Config, load_config, save_config, suggest_config
from .data import Channel, LoadOptions, Telemetry, load
from .events import auto_events, build_events, detect_events
from .expressions import add_derived, evaluate
from .export import export_frames, export_video
from .render import OverlayRenderer, build_renderer
from .theme import THEMES, Theme
from .timeline import Hold, Ramp, Timeline, build_timeline

__all__ = [
    "__version__",
    "Channel", "Telemetry", "LoadOptions", "load",
    "Config", "load_config", "save_config", "suggest_config",
    "Theme", "THEMES",
    "Timeline", "Ramp", "Hold", "build_timeline",
    "OverlayRenderer", "build_renderer",
    "export_frames", "export_video",
    "evaluate", "add_derived",
    "detect_events", "auto_events", "build_events",
]
