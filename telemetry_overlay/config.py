"""Reading, validating and defaulting the overlay configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .data import LoadOptions, Telemetry
from .theme import THEMES, Theme
from .timeline import Hold, Ramp

__all__ = ["Config", "load_config", "save_config", "suggest_config",
           "RESOLUTIONS", "parse_resolution"]

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2k": (2048, 1080),
    "4k": (3840, 2160),
    "uhd": (3840, 2160),
    "dci4k": (4096, 2160),
    "8k": (7680, 4320),
    "vertical1080": (1080, 1920),
    "vertical4k": (2160, 3840),
    "square1080": (1080, 1080),
}

_RES_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", re.IGNORECASE)

# The design canvas every size/position in a config is expressed against.
# Rendering at another resolution scales the whole layout by width ratio.
DESIGN_WIDTH = 1920
DESIGN_HEIGHT = 1080


def parse_resolution(value: Any) -> tuple[int, int]:
    """Accept ``"4k"``, ``"1920x1080"`` or ``[1920, 1080]``."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    text = str(value).strip().lower()
    if text in RESOLUTIONS:
        return RESOLUTIONS[text]
    match = _RES_RE.match(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    raise ValueError(
        f"unrecognised resolution {value!r}; use WxH or one of: "
        f"{', '.join(sorted(RESOLUTIONS))}"
    )


@dataclass
class Config:
    """A whole overlay definition."""

    width: int = DESIGN_WIDTH
    height: int = DESIGN_HEIGHT
    fps: float = 60.0
    speed: float = 1.0
    ramps: list[Ramp] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)
    time_start: float | None = None
    time_end: float | None = None
    t0: float = 0.0
    theme: Theme = field(default_factory=Theme)
    widgets: list[dict[str, Any]] = field(default_factory=list)
    derived: list[dict[str, Any]] = field(default_factory=list)
    events: Any = True
    data: LoadOptions = field(default_factory=LoadOptions)
    background: Any = None          # None keeps the frame transparent
    max_duration: float | None = 900.0
    source_path: Path | None = None

    @property
    def scale(self) -> float:
        """Layout multiplier from the 1920-wide design canvas."""
        return self.width / DESIGN_WIDTH


def _read_structured(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Reading YAML configs needs PyYAML - install it with: "
                "pip install pyyaml  (or use a .json config)"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: the config must be a mapping at the top level")
    return data


_KNOWN_TOP_LEVEL = {
    "output", "time", "theme", "widgets", "derived", "events", "data",
    "background", "name", "description", "notes",
}


def load_config(path: str | Path | None, overrides: dict[str, Any] | None = None
                ) -> Config:
    """Load a YAML/JSON config, apply overrides, and validate it."""
    raw: dict[str, Any] = {}
    source: Path | None = None
    if path is not None:
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"config not found: {source}")
        raw = _read_structured(source)

    unknown = set(raw) - _KNOWN_TOP_LEVEL
    if unknown:
        raise ValueError(
            f"unknown config section(s): {', '.join(sorted(unknown))}. "
            f"Expected any of: {', '.join(sorted(_KNOWN_TOP_LEVEL))}"
        )

    overrides = overrides or {}
    output = dict(raw.get("output") or {})
    time_cfg = dict(raw.get("time") or {})

    for key in ("resolution", "fps", "background"):
        if overrides.get(key) is not None:
            output[key] = overrides[key]
    for key in ("speed", "start", "end", "t0"):
        if overrides.get(key) is not None:
            time_cfg[key] = overrides[key]

    width, height = parse_resolution(output.get("resolution", "1080p"))
    if width <= 0 or height <= 0:
        raise ValueError("resolution must be positive")

    fps = float(output.get("fps", 60))
    if fps <= 0:
        raise ValueError("fps must be positive")

    speed = float(time_cfg.get("speed", 1.0))
    if speed <= 0:
        raise ValueError("time.speed must be positive (0.25 means 4x slower)")

    ramps = [_build_ramp(r) for r in (time_cfg.get("ramps") or [])]
    holds = [_build_hold(h) for h in (time_cfg.get("holds") or [])]

    theme = Theme.from_dict(raw.get("theme"))

    widgets = raw.get("widgets") or []
    if not isinstance(widgets, list):
        raise ValueError("'widgets' must be a list")

    data_cfg = dict(raw.get("data") or {})
    for key in ("delimiter", "decimal", "time_column", "time_unit", "sheet",
                "encoding"):
        if overrides.get(key) is not None:
            data_cfg[key] = overrides[key]
    load_options = LoadOptions(
        delimiter=data_cfg.get("delimiter"),
        decimal=data_cfg.get("decimal"),
        time_column=data_cfg.get("time_column"),
        time_unit=data_cfg.get("time_unit"),
        sheet=data_cfg.get("sheet"),
        encoding=data_cfg.get("encoding"),
        skip_rows=int(data_cfg.get("skip_rows", 0)),
    )

    max_duration = output.get("max_duration", 900.0)
    if overrides.get("max_duration") is not None:
        max_duration = overrides["max_duration"]

    return Config(
        width=width,
        height=height,
        fps=fps,
        speed=speed,
        ramps=ramps,
        holds=holds,
        time_start=_opt_float(time_cfg.get("start")),
        time_end=_opt_float(time_cfg.get("end")),
        t0=float(time_cfg.get("t0", 0.0)),
        theme=theme,
        widgets=widgets,
        derived=raw.get("derived") or [],
        events=raw.get("events", True),
        data=load_options,
        background=output.get("background"),
        max_duration=None if max_duration in (None, 0) else float(max_duration),
        source_path=source,
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _build_ramp(spec: Any) -> Ramp:
    if not isinstance(spec, dict):
        raise ValueError(f"time.ramps entries must be mappings, got {spec!r}")
    missing = {"speed"} - set(spec)
    if missing:
        raise ValueError(f"ramp is missing 'speed': {spec!r}")
    start = spec.get("from", spec.get("start"))
    end = spec.get("to", spec.get("end"))
    if start is None or end is None:
        raise ValueError(f"ramp needs 'from' and 'to' times: {spec!r}")
    return Ramp(
        start=float(start), end=float(end), speed=float(spec["speed"]),
        ease=float(spec.get("ease", 0.0)), label=str(spec.get("label", "")),
    )


def _build_hold(spec: Any) -> Hold:
    if not isinstance(spec, dict):
        raise ValueError(f"time.holds entries must be mappings, got {spec!r}")
    if "at" not in spec or "duration" not in spec:
        raise ValueError(f"hold needs 'at' and 'duration': {spec!r}")
    return Hold(at=float(spec["at"]), duration=float(spec["duration"]),
                label=str(spec.get("label", "")))


def save_config(config: dict[str, Any], path: str | Path) -> Path:
    """Write a config dict as YAML when possible, otherwise JSON."""
    path = Path(path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
            text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                                  default_flow_style=False, width=100)
        except ImportError:
            path = path.with_suffix(".json")
            text = json.dumps(config, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(config, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def suggest_config(telemetry: Telemetry, resolution: str = "1080p",
                   fps: float = 60.0) -> dict[str, Any]:
    """Build a starter config that already fits the given log.

    Picks the most plot-worthy channel for the big graph, adds a readout for
    it, a lamp for every boolean, and a slow-motion ramp around the peak.
    """
    numeric = [k for k in telemetry.numeric_keys()
               if not k.startswith("time") and k != "time"]
    booleans = [k for k, c in telemetry.channels.items() if c.is_boolean]

    def interest(key: str) -> float:
        channel = telemetry[key]
        finite = channel.finite
        if finite.size < 2:
            return -1.0
        span = float(finite.max() - finite.min())
        # Prefer a channel whose name reads like a primary measurement.
        bonus = 5.0 if any(w in key for w in
                           ("thrust", "force", "tah", "press", "accel", "alt")) else 0.0
        return bonus + (1.0 if span > 0 else 0.0)

    primary = max(numeric, key=interest) if numeric else None
    secondary = [k for k in numeric if k != primary][:2]

    widgets: list[dict[str, Any]] = [
        {"type": "label", "name": "title", "text": "STATIC FIRE",
         "x": "4%", "y": "6%", "size": 40, "tracking": 4, "uppercase": True},
        {"type": "timer", "name": "clock", "x": "4%", "y": "12%",
         "label": "mission time", "size": 52, "style": "t+", "decimals": 2},
    ]

    if primary:
        channel = telemetry[primary]
        lo, hi = channel.range()
        widgets.append({
            "type": "readout", "name": primary, "channel": primary,
            "x": "4%", "y": "24%", "value_size": 82,
            "format": "{:.1f}", "label": channel.label,
        })
        widgets.append({
            "type": "graph", "name": "main_graph",
            "series": [{"channel": primary, "fill": True}] +
                      [{"channel": k} for k in secondary],
            "x": "4%", "y": "62%", "width": "50%", "height": "30%",
            "mode": "full", "show_value": True, "y_label": channel.unit,
            "x_label": "t [s]",
        })
        widgets.append({
            "type": "gauge", "name": f"{primary}_gauge", "channel": primary,
            "x": "88%", "y": "10%", "anchor": "top-center",
            "width": 230, "height": 230,
            "min": max(0.0, lo), "max": hi * 1.05,
        })

    for i, key in enumerate(booleans[:4]):
        widgets.append({
            "type": "led", "name": key, "channel": key,
            "x": "4%", "y": f"{44 + i * 5}%", "label": telemetry[key].label,
        })

    # Slow the region around the peak so the interesting part is readable.
    time_cfg: dict[str, Any] = {"speed": 1.0}
    if primary:
        import numpy as np
        values = telemetry[primary].values
        finite = np.where(np.isfinite(values), values, -np.inf)
        peak_t = float(telemetry.time[int(np.argmax(finite))])
        span = telemetry.duration
        half = max(0.25, min(1.0, span * 0.12))
        time_cfg["ramps"] = [{
            "from": round(peak_t - half * 3, 3),
            "to": round(peak_t + half, 3),
            "speed": 0.25, "ease": round(half * 0.5, 3),
            "label": "burn",
        }]

    return {
        "name": "Telemetry overlay",
        "output": {"resolution": resolution, "fps": fps},
        "time": time_cfg,
        "theme": {"base": "night", "palette": "rocket"},
        "events": True,
        "widgets": widgets,
    }
