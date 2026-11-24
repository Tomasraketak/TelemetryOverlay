"""Universal telemetry extractor, converter and animated overlay renderer.

Reads CSV, TSV, XLSX, JSON/GeoJSON, GPX, KML/KMZ, TCX, Garmin FIT, NMEA 0183,
FAI IGC, DJI SRT and GoPro GPMF MP4 files, renders alpha-transparent overlays
or composites directly onto footage, and converts telemetry between formats.
"""

from __future__ import annotations

__version__ = "2.0.0"

from .config import Config, load_config, save_config, suggest_config
from .data import Channel, LoadOptions, Telemetry, load, load_many
from .events import auto_events, build_events, detect_events
from .expressions import add_derived, evaluate
from .export import (
    export_composite_video, export_frames, export_video, probe_video,
)
from .extract import EXPORT_FORMATS, export_telemetry
from .formats import SUPPORTED_FORMATS
from .formats.enrichment import (
    apply_filters, concat_telemetry, detect_laps, enrich_telemetry,
    merge_telemetry,
)
from .render import OverlayRenderer, build_renderer
from .theme import THEMES, Theme
from .timeline import Hold, Ramp, Timeline, build_timeline

__all__ = [
    "__version__",
    "Channel", "Telemetry", "LoadOptions", "load", "load_many",
    "Config", "load_config", "save_config", "suggest_config",
    "Theme", "THEMES",
    "Timeline", "Ramp", "Hold", "build_timeline",
    "OverlayRenderer", "build_renderer",
    "export_frames", "export_video", "export_composite_video", "probe_video",
    "export_telemetry", "EXPORT_FORMATS", "SUPPORTED_FORMATS",
    "enrich_telemetry", "detect_laps", "apply_filters",
    "merge_telemetry", "concat_telemetry",
    "evaluate", "add_derived",
    "detect_events", "auto_events", "build_events",
]
