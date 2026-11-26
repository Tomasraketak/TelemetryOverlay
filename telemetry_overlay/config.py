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

__all__ = [
    "Config", "load_config", "save_config", "suggest_config",
    "RESOLUTIONS", "parse_resolution",
]

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2k": (2048, 1080),
    "4k": (3840, 2160),
    "uhd": (3840, 2160),
    "dci4k": (4096, 2160),
    "5.3k": (5312, 2988),
    "8k": (7680, 4320),
    "vertical1080": (1080, 1920),
    "vertical4k": (2160, 3840),
    "square1080": (1080, 1080),
}

_RES_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", re.IGNORECASE)

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
    filters: list[dict[str, Any]] = field(default_factory=list)
    events: Any = True
    data: LoadOptions = field(default_factory=LoadOptions)
    background: Any = None
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
        except ImportError as exc:  # pragma: no cover
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
    "output", "time", "theme", "widgets", "derived", "filters", "events",
    "data", "background", "name", "description", "notes",
}


def load_config(
    path: str | Path | None, overrides: dict[str, Any] | None = None
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

    theme_raw = dict(raw.get("theme") or {})
    if overrides.get("theme") is not None:
        theme_raw["base"] = overrides["theme"]
    theme = Theme.from_dict(theme_raw)

    widgets = raw.get("widgets") or []
    if not isinstance(widgets, list):
        raise ValueError("'widgets' must be a list")

    filters = list(raw.get("filters") or [])
    if not isinstance(filters, list):
        raise ValueError("'filters' must be a list")

    data_cfg = dict(raw.get("data") or {})
    for key in ("delimiter", "decimal", "time_column", "time_unit", "sheet",
                "encoding", "skip_rows", "format", "enrich", "detect_laps",
                "time_offset"):
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
        format=data_cfg.get("format"),
        enrich=bool(data_cfg.get("enrich", True)),
        detect_laps=bool(data_cfg.get("detect_laps", False)),
        time_offset=float(data_cfg.get("time_offset", 0.0)),
        filters=list(data_cfg.get("filters") or []),
        merge=list(data_cfg.get("merge") or []),
    )

    max_duration = output.get("max_duration", 900.0)
    if overrides.get("max_duration") is not None:
        max_duration = overrides["max_duration"]

    bg = output.get("background")
    if bg is None and raw.get("background") is not None:
        bg = raw.get("background")

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
        filters=filters,
        events=raw.get("events", True),
        data=load_options,
        background=bg,
        max_duration=None if max_duration in (None, 0) else float(max_duration),
        source_path=source,
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _build_ramp(spec: Any) -> Ramp:
    if not isinstance(spec, dict):
        raise ValueError(f"time.ramps entries must be mappings, got {spec!r}")
    if "speed" not in spec:
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
    return Hold(
        at=float(spec["at"]), duration=float(spec["duration"]),
        label=str(spec.get("label", "")),
    )


def save_config(config: dict[str, Any], path: str | Path) -> Path:
    """Write a config dict as YAML when possible, otherwise JSON."""
    path = Path(path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
            text = yaml.safe_dump(
                config, sort_keys=False, allow_unicode=True,
                default_flow_style=False, width=100,
            )
        except ImportError:
            path = path.with_suffix(".json")
            text = json.dumps(config, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(config, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def suggest_config(
    telemetry: Telemetry, resolution: str = "1080p", fps: float = 60.0
) -> dict[str, Any]:
    """Build a domain-aware starter config tailored to the loaded telemetry.

    Detects Cycling/Fitness, Aviation/Drone, Motorsport/Action-Cam, Rocketry
    or generic logger logs and picks matching widgets, themes and time ramps.
    """
    keys = set(telemetry.channels)
    has_gps = telemetry.has_gps
    has_fitness = bool(keys & {"heart_rate", "cadence", "power"})
    has_attitude = bool(keys & {"pitch"}) and bool(keys & {"roll"})
    has_motorsport = bool(keys & {"rpm", "throttle", "brake", "gear", "lap"})
    has_thrust = any(any(w in k for w in ("thrust", "force", "tah")) for k in keys)

    # 1) Cycling / Fitness GPS
    if has_gps and has_fitness:
        widgets: list[dict[str, Any]] = [
            {"type": "map", "name": "route", "x": "3%", "y": "5%",
             "width": 300, "height": 300, "title": "ROUTE"},
            {"type": "gauge", "name": "speed_dial", "channel": ["speed_kmh", "speed"],
             "x": "96%", "y": "6%", "anchor": "top-right",
             "width": 250, "height": 250, "label": "Speed", "show_scale": True,
             "format": "{:.0f}"},
            {"type": "elevation", "name": "elevation_profile", "x": "3%", "y": "79%",
             "width": "44%", "height": "16%", "optional": True},
        ]
        x_cursor = 52
        for ch, lbl, fmt in (
            ("power", "Power", "{:.0f}"),
            ("heart_rate", "Heart Rate", "{:.0f}"),
            ("cadence", "Cadence", "{:.0f}"),
        ):
            if ch in telemetry:
                widgets.append({
                    "type": "sparkline", "name": ch, "channel": ch, "label": lbl,
                    "x": f"{x_cursor}%", "y": "82%", "width": "14%", "height": "13%",
                    "format": fmt,
                })
                x_cursor += 15
        return {
            "name": "Cycling & Fitness Telemetry",
            "output": {"resolution": resolution, "fps": fps},
            "time": {"speed": 1.0},
            "theme": {"base": "cycling"},
            "events": False,
            "widgets": widgets,
        }

    # 2) Aviation / Drone Flight
    if has_gps and has_attitude:
        widgets = [
            {"type": "compass", "name": "heading", "x": "50%", "y": "3%",
             "anchor": "top-center", "width": 440, "height": 74, "style": "tape"},
            {"type": "attitude", "name": "horizon", "x": "50%", "y": "50%",
             "anchor": "middle-center", "width": 260, "height": 260, "opacity": 0.88},
            {"type": "tape", "name": "spd_tape", "channel": ["speed_kmh", "speed"],
             "label": "SPD", "side": "left", "x": "3%", "y": "24%", "width": 125, "height": 340},
            {"type": "tape", "name": "alt_tape", "channel": ["altitude", "rel_altitude"],
             "label": "ALT", "side": "right", "x": "97%", "y": "24%",
             "anchor": "top-right", "width": 125, "height": 340, "optional": True},
            {"type": "map", "name": "flight_path", "x": "3%", "y": "68%",
             "width": 280, "height": 280, "title": "FLIGHT PATH"},
        ]
        return {
            "name": "Aviation / Drone HUD",
            "output": {"resolution": resolution, "fps": fps},
            "time": {"speed": 1.0},
            "theme": {"base": "aviation"},
            "events": False,
            "widgets": widgets,
        }

    # 3) Motorsport / Action-cam GPS
    if has_gps or has_motorsport:
        widgets = [
            {"type": "gauge", "name": "speed", "channel": ["speed_kmh", "speed"],
             "x": "96%", "y": "94%", "anchor": "bottom-right",
             "width": 270, "height": 270, "label": "Speed", "show_scale": True,
             "shift_lights": True, "peak_marker": True, "format": "{:.0f}"},
            {"type": "gforce", "name": "g_ball", "x": "78%", "y": "94%",
             "anchor": "bottom-right", "width": 230, "height": 245, "optional": True},
            {"type": "map", "name": "track", "x": "3%", "y": "5%",
             "width": 310, "height": 310, "title": "TRACK", "optional": True},
            {"type": "pedals", "name": "driver_inputs", "x": "3%", "y": "94%",
             "anchor": "bottom-left", "width": 260, "height": 145, "optional": True},
            {"type": "timer", "name": "clock", "x": "96%", "y": "5%",
             "anchor": "top-right", "align": "right", "size": 36, "style": "clock",
             "decimals": 1, "panel": False},
        ]
        return {
            "name": "Action / Motorsport Telemetry",
            "output": {"resolution": resolution, "fps": fps},
            "time": {"speed": 1.0},
            "theme": {"base": "motorsport"},
            "events": False,
            "widgets": widgets,
        }

    # 4) Rocketry (Flight with 3D attitude, Static Fire test, or standalone IMU)
    has_quat = all(k in keys for k in ("quat_w", "quat_x", "quat_y", "quat_z"))
    has_3d = has_attitude or has_quat
    booleans = [k for k, c in telemetry.channels.items() if c.is_boolean]

    if has_thrust or has_3d:
        thrust_key = next(
            (k for k in telemetry.numeric_keys()
             if any(w in k for w in ("thrust", "force", "tah"))),
            None,
        )
        widgets = []
        if has_thrust and has_3d:
            # Full rocket flight: 3D attitude + motor burn card + flight gauges
            widgets.extend([
                {"type": "label", "name": "title", "text": "ROCKET FLIGHT",
                 "x": "4%", "y": "4%", "size": 34, "tracking": 4, "uppercase": True},
                {"type": "timer", "name": "clock", "x": "4%", "y": "10%",
                 "label": "mission elapsed", "size": 44, "style": "t+", "decimals": 2},
                {"type": "badge", "name": "phase", "channel": ["phase", "state", "mode"],
                 "x": "4%", "y": "20%", "prefix": "PHASE: ", "optional": True},
                {"type": "motor_burn", "name": "motor",
                 "channel": thrust_key or ["thrust", "force", "tah"],
                 "x": "4%", "y": "59%", "width": "41%", "height": "32%",
                 "title": "Rocket Motor", "zoom_burn": True},
                {"type": "rocket_3d", "name": "attitude_3d",
                 "x": "70%", "y": "5%", "width": 320, "height": 370,
                 "title": "3D Attitude",
                 "thrust_channel": thrust_key or ["thrust", "force", "tah"]},
            ])
            if "altitude" in telemetry:
                widgets.append({
                    "type": "tape", "name": "alt_tape", "channel": "altitude",
                    "label": "ALT", "side": "right", "x": "96%", "y": "5%",
                    "anchor": "top-right", "width": 130, "height": 370,
                })
                widgets.append({
                    "type": "graph", "name": "alt_graph",
                    "series": [{"channel": "altitude", "fill": True}],
                    "x": "48%", "y": "59%", "width": "48%", "height": "32%",
                    "mode": "full", "title": "Altitude Profile", "show_value": True,
                })
            if "velocity" in telemetry:
                widgets.append({
                    "type": "readout", "name": "vel", "channel": "velocity",
                    "x": "4%", "y": "31%", "label": "Velocity", "value_size": 54,
                    "format": "{:.1f}",
                })
            widgets.append({
                "type": "progress", "name": "timeline",
                "x": "4%", "y": "96%", "width": "92%", "height": 10,
            })
            return {
                "name": "Rocket Flight & Motor Telemetry",
                "output": {"resolution": resolution, "fps": fps},
                "time": {"speed": 1.0},
                "theme": {"base": "night", "palette": "rocket"},
                "events": True,
                "widgets": widgets,
            }

        if has_thrust and thrust_key:
            # Static fire motor test stand
            channel = telemetry[thrust_key]
            lo, hi = channel.range()
            widgets.append({
                "type": "motor_burn", "name": "motor", "channel": thrust_key,
                "x": "4%", "y": "62%", "width": "43%", "height": "33%",
                "title": "Rocket Motor", "show_nozzle": True,
            })
            widgets.append({
                "type": "gauge", "name": f"{thrust_key}_gauge", "channel": thrust_key,
                "x": "96%", "y": "62%", "anchor": "top-right",
                "width": 270, "height": 270, "label": channel.label,
                "show_scale": True, "peak_marker": True,
                "min": max(0.0, lo), "max": hi * 1.05, "format": "{:.0f}",
            })
            for i, key in enumerate(booleans[:4]):
                widgets.append({
                    "type": "led", "name": key, "channel": key,
                    "x": "4%", "y": f"{55 - i * 5}%", "label": telemetry[key].label,
                })
            widgets.append({
                "type": "progress", "name": "timeline",
                "x": "4%", "y": "97.5%", "width": "92%", "height": 8,
            })
            import numpy as np
            values = channel.values
            finite = np.where(np.isfinite(values), values, -np.inf)
            peak_t = float(telemetry.time[int(np.argmax(finite))])
            span = telemetry.duration
            half = max(0.25, min(1.0, span * 0.12))
            time_cfg: dict[str, Any] = {
                "speed": 1.0,
                "ramps": [{
                    "from": round(peak_t - half * 3, 3),
                    "to": round(peak_t + half, 3),
                    "speed": 0.25, "ease": round(half * 0.5, 3),
                    "label": "burn",
                }],
            }
            return {
                "name": "Rocket Motor Static Fire",
                "output": {"resolution": resolution, "fps": fps},
                "time": time_cfg,
                "theme": {"base": "night", "palette": "rocket"},
                "events": True,
                "widgets": widgets,
            }

        # Standalone 3D attitude / IMU log (no GPS, no thrust)
        attitude_series = [
            {"channel": k} for k in ("pitch", "roll", "yaw") if k in telemetry
        ][:3]
        widgets = [
            {"type": "label", "name": "title", "text": "3D ATTITUDE TELEMETRY",
             "x": "4%", "y": "5%", "size": 36, "tracking": 4, "uppercase": True},
            {"type": "timer", "name": "clock", "x": "4%", "y": "12%",
             "label": "time", "size": 46, "style": "t+", "decimals": 2},
            {"type": "rocket_3d", "name": "attitude_3d",
             "x": "96%", "y": "6%", "anchor": "top-right",
             "width": 380, "height": 440, "title": "3D Attitude"},
        ]
        if attitude_series:
            widgets.append({
                "type": "graph", "name": "attitude_graph", "series": attitude_series,
                "x": "4%", "y": "60%", "width": "54%", "height": "32%",
                "mode": "full", "title": "Orientation Angles", "show_value": True,
            })
        return {
            "name": "3D Rocket Attitude Telemetry",
            "output": {"resolution": resolution, "fps": fps},
            "time": {"speed": 1.0},
            "theme": {"base": "night", "palette": "rocket"},
            "events": False,
            "widgets": widgets,
        }

    # 5) Generic Engineering / Data Logger
    numeric = [k for k in telemetry.numeric_keys()
               if not k.startswith("time") and k != "time"]

    def interest(key: str) -> float:
        channel = telemetry[key]
        finite = channel.finite
        if finite.size < 2:
            return -1.0
        span = float(finite.max() - finite.min())
        bonus = 5.0 if any(w in key for w in
                           ("thrust", "force", "tah", "press", "accel", "alt")) else 0.0
        return bonus + (1.0 if span > 0 else 0.0)

    primary = max(numeric, key=interest) if numeric else None
    secondary = [k for k in numeric if k != primary][:2]

    widgets = [
        {"type": "label", "name": "title",
         "text": "TELEMETRY",
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
            "width": 240, "height": 240, "show_scale": True, "peak_marker": True,
            "min": max(0.0, lo), "max": hi * 1.05,
        })

    for i, key in enumerate(booleans[:4]):
        widgets.append({
            "type": "led", "name": key, "channel": key,
            "x": "4%", "y": f"{44 + i * 5}%", "label": telemetry[key].label,
        })

    time_cfg = {"speed": 1.0}
    return {
        "name": "Telemetry overlay",
        "output": {"resolution": resolution, "fps": fps},
        "time": time_cfg,
        "theme": {"base": "night", "palette": "rocket"},
        "events": True,
        "widgets": widgets,
    }
