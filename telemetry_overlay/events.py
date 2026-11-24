"""Finding and declaring the moments worth marking on a plot.

Events are timestamps with a label: ignition, peak, burnout, a pyro channel
firing, or detected circuit laps. They can be listed by hand in the config,
or detected automatically from channels so one config works across logs.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .data import Telemetry

__all__ = ["detect_events", "detect_edges", "build_events", "auto_events"]


def _first_crossing(
    values: np.ndarray, time: np.ndarray, level: float,
    rising: bool = True, start: int = 0
) -> float | None:
    """Time of the first crossing of ``level``, linearly interpolated."""
    if values.size < 2:
        return None
    above = values >= level
    for i in range(max(1, start), values.size):
        if not (math.isfinite(values[i]) and math.isfinite(values[i - 1])):
            continue
        crossed = (above[i] and not above[i - 1]) if rising else \
                  (not above[i] and above[i - 1])
        if crossed:
            y0, y1 = values[i - 1], values[i]
            if y1 == y0:
                return float(time[i])
            frac = (level - y0) / (y1 - y0)
            return float(time[i - 1] + frac * (time[i] - time[i - 1]))
    return None


def _last_crossing(values: np.ndarray, time: np.ndarray, level: float) -> float | None:
    """Time of the last fall below ``level`` (ignoring NaN samples)."""
    if values.size < 2:
        return None
    above = values >= level
    for i in range(values.size - 1, 0, -1):
        if not (math.isfinite(values[i]) and math.isfinite(values[i - 1])):
            continue
        if above[i - 1] and not above[i]:
            y0, y1 = values[i - 1], values[i]
            if y1 == y0:
                return float(time[i])
            frac = (level - y0) / (y1 - y0)
            return float(time[i - 1] + frac * (time[i] - time[i - 1]))
    return None


def detect_events(
    telemetry: Telemetry, channel: Any,
    threshold: float | None = None,
    fraction: float = 0.05,
) -> list[dict[str, Any]]:
    """Detect ignition, peak and burnout on a thrust-like channel."""
    key = telemetry.resolve(channel)
    source = telemetry.get(key) if key else None
    if source is None:
        raise ValueError(
            f"cannot detect events: no channel {channel!r}; available: "
            f"{', '.join(sorted(telemetry.channels))}"
        )
    values = np.asarray(source.values, dtype=float)
    time = telemetry.time
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return []
    peak = float(finite.max())
    if threshold is None:
        threshold = peak * float(fraction)

    events: list[dict[str, Any]] = []
    ignition = _first_crossing(values, time, threshold, rising=True)
    if ignition is not None:
        events.append({"time": ignition, "label": "IGNITION", "key": "ignition"})

    peak_idx = int(np.nanargmax(np.where(np.isfinite(values), values, -np.inf)))
    events.append({
        "time": float(time[peak_idx]),
        "label": f"PEAK {peak:.0f} {source.unit}".strip(),
        "key": "peak",
    })

    burnout = _last_crossing(values, time, threshold)
    if burnout is not None and (ignition is None or burnout > ignition):
        events.append({"time": burnout, "label": "BURNOUT", "key": "burnout"})
    return events


def detect_edges(
    telemetry: Telemetry, channel: Any, label: str = "",
    threshold: float = 0.5, edge: str = "rising",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Mark the transitions of a boolean or thresholded channel."""
    key = telemetry.resolve(channel)
    source = telemetry.get(key) if key else None
    if source is None:
        raise ValueError(
            f"cannot detect edges: no channel {channel!r}; available: "
            f"{', '.join(sorted(telemetry.channels))}"
        )
    channel = key
    values = np.asarray(source.values, dtype=float)
    time = telemetry.time
    above = np.isfinite(values) & (values >= threshold)
    events: list[dict[str, Any]] = []
    base = label or source.label.upper()
    for i in range(1, values.size):
        rising = bool(above[i] and not above[i - 1])
        falling = bool(above[i - 1] and not above[i])
        if edge in ("rising", "both") and rising:
            events.append({"time": float(time[i]), "label": f"{base} ON",
                           "key": f"{channel}_on"})
        elif edge in ("falling", "both") and falling:
            events.append({"time": float(time[i]), "label": f"{base} OFF",
                           "key": f"{channel}_off"})
        if len(events) >= limit:
            break
    return events


def _lap_events(telemetry: Telemetry, color: str | None = None) -> list[dict[str, Any]]:
    laps = telemetry.meta.get("laps")
    if not isinstance(laps, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, lap in enumerate(laps, start=1):
        t = lap.get("start_t")
        if t is None or not math.isfinite(float(t)):
            continue
        ev: dict[str, Any] = {
            "time": float(t),
            "label": f"LAP {idx}",
            "key": f"lap_{idx}",
        }
        if color:
            ev["color"] = color
        out.append(ev)
    return out


def auto_events(telemetry: Telemetry, primary: str | None = None) -> list[dict[str, Any]]:
    """A sensible default set of events with no configuration at all."""
    if primary is None:
        numeric = telemetry.numeric_keys()
        for candidate in numeric:
            if any(word in candidate for word in ("thrust", "force", "tah")):
                primary = candidate
                break
        else:
            # For GPS / motorsport / cycling logs prefer speed or altitude over lat/lon
            for candidate in numeric:
                if any(word in candidate for word in ("speed", "altitude", "power", "rpm")):
                    primary = candidate
                    break
            else:
                primary = numeric[0] if numeric else None
    if primary is None:
        return []
    events = detect_events(telemetry, primary)
    for key, channel in telemetry.channels.items():
        if channel.is_boolean and channel.values.size and np.any(channel.values > 0):
            events.extend(detect_edges(telemetry, key, edge="rising", limit=2))
    if isinstance(telemetry.meta.get("laps"), list):
        events.extend(_lap_events(telemetry))
    return sorted(events, key=lambda e: e["time"])


def build_events(telemetry: Telemetry, specs: Any) -> list[dict[str, Any]]:
    """Turn the config's ``events`` block into a list of marker dicts."""
    if specs is None or specs is False:
        return []
    if specs is True:
        return auto_events(telemetry)
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, (list, tuple)):
        raise ValueError("'events' must be true, false, or a list")

    events: list[dict[str, Any]] = []
    for spec in specs:
        if isinstance(spec, str):
            if spec == "auto":
                events.extend(auto_events(telemetry))
                continue
            if spec == "laps":
                events.extend(_lap_events(telemetry))
                continue
            raise ValueError(f"unrecognised event entry {spec!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"event must be a mapping, got {spec!r}")

        if "detect" in spec:
            channel = spec["detect"]
            if spec.get("optional") and telemetry.resolve(channel) is None:
                continue
            found = detect_events(
                telemetry, channel,
                threshold=spec.get("threshold"),
                fraction=float(spec.get("fraction", 0.05)),
            )
            only = spec.get("only")
            if only:
                wanted = {only} if isinstance(only, str) else set(only)
                found = [e for e in found if e["key"] in wanted]
            for event in found:
                if "color" in spec:
                    event["color"] = spec["color"]
            events.extend(found)
        elif "edges" in spec:
            if spec.get("optional") and telemetry.resolve(spec["edges"]) is None:
                continue
            found = detect_edges(
                telemetry, spec["edges"],
                label=str(spec.get("label", "")),
                threshold=float(spec.get("threshold", 0.5)),
                edge=str(spec.get("edge", "rising")),
                limit=int(spec.get("limit", 8)),
            )
            for event in found:
                if "color" in spec:
                    event["color"] = spec["color"]
            events.extend(found)
        elif spec.get("laps"):
            events.extend(_lap_events(telemetry, color=spec.get("color")))
        elif "time" in spec:
            events.append({
                "time": float(spec["time"]),
                "label": str(spec.get("label", "")),
                "key": str(spec.get("key", "custom")),
                **({"color": spec["color"]} if "color" in spec else {}),
            })
        else:
            raise ValueError(f"event needs 'time', 'detect', 'edges' or 'laps': {spec!r}")
    return sorted(events, key=lambda e: e["time"])
