"""Automatic GPS/IMU metric enrichment, lap detection, filtering and merging."""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from ..data import Channel, Telemetry

__all__ = [
    "enrich_telemetry",
    "detect_laps",
    "apply_filters",
    "merge_telemetry",
    "concat_telemetry",
    "haversine_meters",
]

_EARTH_RADIUS_M = 6_371_000.0
_G = 9.80665


def haversine_meters(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Vectorised great-circle distance in metres between WGS-84 coordinates."""
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = phi2 - phi1
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi * 0.5) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam * 0.5) ** 2
    return 2.0 * _EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _bearing_deg(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dlam = np.radians(lon2 - lon1)
    y = np.sin(dlam) * np.cos(phi2)
    x = np.cos(phi1) * np.sin(phi2) - np.sin(phi1) * np.cos(phi2) * np.cos(dlam)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def _smooth_array(y: np.ndarray, window: int) -> np.ndarray:
    n = max(1, int(window))
    if n <= 1 or y.size < 2:
        return y.copy()
    finite = np.isfinite(y)
    if not finite.any():
        return y.copy()
    idx = np.arange(y.size)
    filled = np.interp(idx, idx[finite], y[finite]) if not finite.all() else y
    kernel = np.ones(n, dtype=float) / n
    padded = np.pad(filled, (n // 2, n - 1 - n // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _median_filter(y: np.ndarray, window: int) -> np.ndarray:
    n = max(3, int(window) | 1)
    if y.size < n:
        return y.copy()
    half = n // 2
    padded = np.pad(y, (half, half), mode="edge")
    # Vectorised strided view for fast sliding median
    shape = (y.size, n)
    strides = (padded.strides[0], padded.strides[0])
    windows = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)
    return np.nanmedian(windows, axis=1)


def _ema_filter(y: np.ndarray, alpha: float) -> np.ndarray:
    a = float(np.clip(alpha, 1e-4, 1.0))
    out = y.astype(float, copy=True)
    if out.size == 0:
        return out
    last = out[0] if math.isfinite(out[0]) else 0.0
    for i in range(out.size):
        v = out[i]
        if math.isfinite(v):
            last = a * v + (1.0 - a) * last
            out[i] = last
        else:
            out[i] = last
    return out


def _lowpass_filter(y: np.ndarray, t: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """Zero-phase (forward-backward) first-order RC low-pass filter."""
    if y.size < 3 or cutoff_hz <= 0:
        return y.copy()
    finite = np.isfinite(y)
    if not finite.any():
        return y.copy()
    idx = np.arange(y.size)
    filled = np.interp(idx, idx[finite], y[finite]) if not finite.all() else y.astype(float)
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)

    def _pass(arr: np.ndarray, dt_arr: np.ndarray) -> np.ndarray:
        res = np.empty_like(arr)
        res[0] = arr[0]
        for i in range(1, arr.size):
            dt_i = max(0.0, dt_arr[i - 1])
            alpha = dt_i / (rc + dt_i) if (rc + dt_i) > 0 else 1.0
            res[i] = res[i - 1] + alpha * (arr[i] - res[i - 1])
        return res

    dts = np.diff(t)
    fwd = _pass(filled, dts)
    bwd = _pass(fwd[::-1], dts[::-1])[::-1]
    return bwd


# ---------------------------------------------------------------------------
# Auto-enrichment for GPS & IMU logs
# ---------------------------------------------------------------------------

def enrich_telemetry(telemetry: Telemetry) -> Telemetry:
    """Compute standard derived GPS & IMU streams without overwriting existing ones.

    Only activates when GPS coordinates (``lat``/``lon``) or 3-axis accelerometer
    channels are present, leaving custom laboratory/rocketry logs untouched.
    """
    t = telemetry.time
    if t.size < 2:
        return telemetry

    lat_key = telemetry.resolve(["lat", "latitude", "gps_lat"])
    lon_key = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
    alt_key = telemetry.resolve(["altitude", "alt", "ele", "elevation", "gps_alt"])

    if lat_key and lon_key:
        lat = telemetry[lat_key].values
        lon = telemetry[lon_key].values
        valid = np.isfinite(lat) & np.isfinite(lon)
        if valid.sum() >= 2:
            # Interpolate brief GPS dropouts for smooth derived metrics
            idx = np.arange(t.size)
            lat_f = np.interp(idx, idx[valid], lat[valid]) if not valid.all() else lat
            lon_f = np.interp(idx, idx[valid], lon[valid]) if not valid.all() else lon

            seg_d = haversine_meters(lat_f[:-1], lon_f[:-1], lat_f[1:], lon_f[1:])
            dt_s = np.diff(t)
            safe_dt = np.where(dt_s > 1e-6, dt_s, np.nan)

            if "distance" not in telemetry:
                dist = np.concatenate(([0.0], np.cumsum(np.nan_to_num(seg_d, nan=0.0))))
                telemetry.add(Channel("distance", "Distance", "m", dist, "numeric"))

            # Speed (m/s and km/h)
            if "speed" not in telemetry:
                spd_seg = np.nan_to_num(seg_d / safe_dt, nan=0.0)
                spd = np.concatenate(([spd_seg[0]], spd_seg))
                # Light smoothing proportional to sample rate to calm GPS jitter
                sr = telemetry.sample_rate
                if sr >= 5:
                    spd = _smooth_array(spd, max(3, int(round(sr * 0.4))))
                telemetry.add(Channel("speed", "Speed", "m/s", spd, "numeric"))

            speed_vals = telemetry["speed"].values
            speed_unit = telemetry["speed"].unit.lower()
            speed_ms = (
                speed_vals / 3.6 if "km" in speed_unit
                else (speed_vals * 0.44704 if "mph" in speed_unit else speed_vals)
            )
            if "speed_kmh" not in telemetry:
                telemetry.add(Channel("speed_kmh", "Speed", "km/h", speed_ms * 3.6, "numeric"))
            if "speed_mph" not in telemetry:
                telemetry.add(Channel("speed_mph", "Speed", "mph", speed_ms * 2.236936, "numeric"))

            # Heading / bearing
            if "bearing" not in telemetry and "heading" not in telemetry:
                brg_seg = _bearing_deg(lat_f[:-1], lon_f[:-1], lat_f[1:], lon_f[1:])
                # Hold last bearing when stationary (< 0.25 m step)
                brg = np.empty(t.size, dtype=float)
                last_b = float(brg_seg[0]) if brg_seg.size else 0.0
                brg[0] = last_b
                for i, (d_m, b) in enumerate(zip(seg_d, brg_seg), start=1):
                    if d_m >= 0.25 and math.isfinite(b):
                        last_b = float(b)
                    brg[i] = last_b
                telemetry.add(Channel("bearing", "Bearing", "°", brg, "numeric"))

            # Altitude-based vertical speed, grade (%) and elevation gain
            if alt_key:
                alt = telemetry[alt_key].values
                alt_valid = np.isfinite(alt)
                if alt_valid.sum() >= 2:
                    alt_f = np.interp(idx, idx[alt_valid], alt[alt_valid]) if not alt_valid.all() else alt
                    d_alt = np.diff(alt_f)
                    if "vspeed" not in telemetry:
                        vs_seg = np.nan_to_num(d_alt / safe_dt, nan=0.0)
                        vs = np.concatenate(([vs_seg[0]], vs_seg))
                        telemetry.add(Channel("vspeed", "Vertical Speed", "m/s", vs, "numeric"))
                    if "grade" not in telemetry and "slope" not in telemetry:
                        # Compute grade over a smoothing span so 1 Hz GPS alt isn't noisy
                        win = max(1, int(round(max(1.0, telemetry.sample_rate) * 2.0)))
                        sm_alt = _smooth_array(alt_f, win)
                        sm_dist = telemetry["distance"].values
                        d_h = np.gradient(sm_alt)
                        d_s = np.gradient(sm_dist)
                        grade = np.where(d_s > 0.5, (d_h / np.where(d_s > 0.5, d_s, 1.0)) * 100.0, 0.0)
                        grade = np.clip(grade, -45.0, 45.0)
                        telemetry.add(Channel("grade", "Slope", "%", grade, "numeric"))
                    if "elevation_gain" not in telemetry:
                        gain = np.concatenate(([0.0], np.cumsum(np.maximum(0.0, d_alt))))
                        telemetry.add(Channel("elevation_gain", "Elevation Gain", "m", gain, "numeric"))

            # GPS-derived longitudinal & lateral G-force and lean angle (when no IMU G present)
            if "g_lon" not in telemetry:
                win = max(1, int(round(max(1.0, telemetry.sample_rate) * 0.5)))
                sm_v = _smooth_array(speed_ms, win)
                dv_dt = np.gradient(sm_v, t)
                g_lon = np.clip(dv_dt / _G, -6.0, 6.0)
                telemetry.add(Channel("g_lon", "Longitudinal G", "G", g_lon, "numeric"))

            if "g_lat" not in telemetry:
                brg_key = telemetry.resolve(["bearing", "heading", "yaw"])
                if brg_key:
                    brg_rad = np.unwrap(np.radians(telemetry[brg_key].values))
                    omega = np.gradient(brg_rad, t)
                    g_lat = np.clip((speed_ms * omega) / _G, -6.0, 6.0)
                    win = max(1, int(round(max(1.0, telemetry.sample_rate) * 0.5)))
                    g_lat = _smooth_array(g_lat, win)
                    telemetry.add(Channel("g_lat", "Lateral G", "G", g_lat, "numeric"))

            if "g_total" not in telemetry and "g_lon" in telemetry and "g_lat" in telemetry:
                g_tot = np.hypot(telemetry["g_lon"].values, telemetry["g_lat"].values)
                telemetry.add(Channel("g_total", "Total G", "G", g_tot, "numeric"))

            if "lean_angle" not in telemetry and "g_lat" in telemetry:
                lean = np.degrees(np.arctan(telemetry["g_lat"].values))
                telemetry.add(Channel("lean_angle", "Lean Angle", "°", lean, "numeric"))

    # 3-axis accelerometer enrichment (if present without g_total)
    ax_k = telemetry.resolve(["accel_x", "ax", "acc_x"])
    ay_k = telemetry.resolve(["accel_y", "ay", "acc_y"])
    az_k = telemetry.resolve(["accel_z", "az", "acc_z"])
    if ax_k and ay_k and az_k and "g_total" not in telemetry:
        ax, ay, az = telemetry[ax_k].values, telemetry[ay_k].values, telemetry[az_k].values
        u = telemetry[ax_k].unit.lower()
        div = 1.0 if u in ("g", "gn") else _G
        g_tot = np.sqrt(ax ** 2 + ay ** 2 + az ** 2) / div
        telemetry.add(Channel("g_total", "Total G", "G", g_tot, "numeric"))
        if "g_lon" not in telemetry:
            telemetry.add(Channel("g_lon", "Longitudinal G", "G", ax / div, "numeric"))
        if "g_lat" not in telemetry:
            telemetry.add(Channel("g_lat", "Lateral G", "G", ay / div, "numeric"))

    return telemetry


# ---------------------------------------------------------------------------
# Automatic GPS circuit lap detection
# ---------------------------------------------------------------------------

def detect_laps(
    telemetry: Telemetry,
    gate_lat: float | None = None,
    gate_lon: float | None = None,
    gate_radius_m: float = 25.0,
    min_lap_seconds: float = 15.0,
) -> list[dict[str, float]]:
    """Detect start/finish line crossings on a GPS circuit and populate lap channels.

    Adds ``lap``, ``lap_time``, ``last_lap``, ``best_lap``, and ``lap_delta``
    channels to ``telemetry`` and returns a list of lap summary dicts.
    """
    lat_k = telemetry.resolve(["lat", "latitude", "gps_lat"])
    lon_k = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
    if not lat_k or not lon_k or telemetry.time.size < 10:
        return []

    t = telemetry.time
    lat = telemetry[lat_k].values
    lon = telemetry[lon_k].values
    valid = np.isfinite(lat) & np.isfinite(lon)
    if valid.sum() < 10:
        return []

    first_valid = int(np.flatnonzero(valid)[0])
    g_lat = float(lat[first_valid]) if gate_lat is None else float(gate_lat)
    g_lon = float(lon[first_valid]) if gate_lon is None else float(gate_lon)

    dist = haversine_meters(lat, lon, np.full_like(lat, g_lat), np.full_like(lon, g_lon))
    crossings: list[float] = [float(t[first_valid])]

    # Walk the distance trace: after leaving the gate (> 1.5 * radius), find local
    # minimum each time the vehicle re-enters within gate_radius_m.
    left_gate = False
    in_zone = False
    zone_best_d = math.inf
    zone_best_t = float(t[first_valid])

    for i in range(first_valid + 1, t.size):
        d = float(dist[i])
        if not math.isfinite(d):
            continue
        if not left_gate:
            if d > gate_radius_m * 1.6:
                left_gate = True
            continue
        if d <= gate_radius_m:
            if not in_zone:
                in_zone = True
                zone_best_d = d
                zone_best_t = float(t[i])
            elif d < zone_best_d:
                zone_best_d = d
                zone_best_t = float(t[i])
        elif in_zone:
            in_zone = False
            if zone_best_t - crossings[-1] >= min_lap_seconds:
                crossings.append(zone_best_t)
                left_gate = False

    if len(crossings) < 2:
        return []

    laps: list[dict[str, float]] = []
    for idx in range(len(crossings) - 1):
        laps.append({
            "lap": idx + 1,
            "start_t": crossings[idx],
            "end_t": crossings[idx + 1],
            "duration": crossings[idx + 1] - crossings[idx],
        })

    # Build per-sample lap channels
    n = t.size
    lap_num = np.ones(n, dtype=float)
    lap_time = np.zeros(n, dtype=float)
    last_lap = np.full(n, math.nan, dtype=float)
    best_lap = np.full(n, math.nan, dtype=float)
    lap_delta = np.full(n, math.nan, dtype=float)

    best_so_far = math.inf
    last_dur = math.nan
    c_idx = 0
    for i in range(n):
        ti = float(t[i])
        while c_idx + 1 < len(crossings) and ti >= crossings[c_idx + 1]:
            dur = crossings[c_idx + 1] - crossings[c_idx]
            last_dur = dur
            if dur < best_so_far:
                best_so_far = dur
            c_idx += 1
        lap_num[i] = float(c_idx + 1)
        lap_time[i] = max(0.0, ti - crossings[c_idx])
        last_lap[i] = last_dur
        if math.isfinite(best_so_far):
            best_lap[i] = best_so_far
            if math.isfinite(last_dur):
                lap_delta[i] = last_dur - best_so_far

    telemetry.add(Channel("lap", "Lap", "", lap_num, "numeric"))
    telemetry.add(Channel("lap_time", "Lap Time", "s", lap_time, "numeric"))
    telemetry.add(Channel("last_lap", "Last Lap", "s", last_lap, "numeric"))
    telemetry.add(Channel("best_lap", "Best Lap", "s", best_lap, "numeric"))
    telemetry.add(Channel("lap_delta", "Lap Delta", "s", lap_delta, "numeric"))
    telemetry.meta["laps"] = laps
    return laps


# ---------------------------------------------------------------------------
# Signal filtering & cleaning pipeline
# ---------------------------------------------------------------------------

def apply_filters(
    telemetry: Telemetry, filters: Sequence[dict[str, Any]] | None
) -> Telemetry:
    """Apply a declarative list of signal cleaning/smoothing filters in-place."""
    if not filters:
        return telemetry

    for spec in filters:
        if not isinstance(spec, dict):
            raise ValueError(f"filter entry must be a mapping, got {spec!r}")
        kind = str(spec.get("type", "")).strip().lower()
        if not kind:
            raise ValueError(f"filter is missing 'type': {spec!r}")

        if kind == "resample":
            hz = float(spec.get("hz", spec.get("rate", 60.0)))
            if hz <= 0:
                raise ValueError("resample filter 'hz' must be positive")
            if telemetry.time.size >= 2:
                new_t = np.arange(telemetry.t0, telemetry.t1 + 1e-9, 1.0 / hz)
                old_t = telemetry.time
                for ch in telemetry.channels.values():
                    if ch.kind == "numeric":
                        ch.values = np.interp(new_t, old_t, ch.values)
                    else:
                        idx = np.clip(np.searchsorted(old_t, new_t), 0, old_t.size - 1)
                        ch.values = ch.values[idx]
                        if ch.text_values is not None:
                            ch.text_values = ch.text_values[idx]
                    ch.invalidate_cache()
                telemetry.time = new_t
            continue

        if kind == "gps_filter":
            lat_k = telemetry.resolve(["lat", "latitude", "gps_lat"])
            lon_k = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
            if lat_k and lon_k and telemetry.time.size >= 3:
                max_speed = float(spec.get("max_speed", 120.0))  # m/s
                max_hdop = spec.get("max_hdop")
                lat = telemetry[lat_k].values.copy()
                lon = telemetry[lon_k].values.copy()
                bad = ~np.isfinite(lat) | ~np.isfinite(lon) | ((lat == 0.0) & (lon == 0.0))
                if max_hdop is not None and "hdop" in telemetry:
                    bad |= telemetry["hdop"].values > float(max_hdop)
                # Reject impossible teleportation jumps
                good_idx = np.flatnonzero(~bad)
                if good_idx.size >= 2:
                    prev_i = good_idx[0]
                    for cur_i in good_idx[1:]:
                        dt_i = telemetry.time[cur_i] - telemetry.time[prev_i]
                        d_m = float(haversine_meters(
                            np.array([lat[prev_i]]), np.array([lon[prev_i]]),
                            np.array([lat[cur_i]]), np.array([lon[cur_i]]),
                        )[0])
                        if dt_i > 0 and (d_m / dt_i) > max_speed:
                            bad[cur_i] = True
                        else:
                            prev_i = cur_i
                keep = ~bad
                if keep.sum() >= 2 and bad.any():
                    idx = np.arange(telemetry.time.size)
                    telemetry[lat_k].values = np.interp(idx, idx[keep], lat[keep])
                    telemetry[lon_k].values = np.interp(idx, idx[keep], lon[keep])
                    telemetry[lat_k].invalidate_cache()
                    telemetry[lon_k].invalidate_cache()
            continue

        # Channel-targeted filters
        raw_channels = spec.get("channels") or spec.get("channel")
        if raw_channels is None or raw_channels == "all":
            target_keys = telemetry.numeric_keys()
        elif isinstance(raw_channels, str):
            k = telemetry.resolve(raw_channels)
            target_keys = [k] if k else []
        else:
            target_keys = []
            for item in raw_channels:
                k = telemetry.resolve(item)
                if k:
                    target_keys.append(k)

        for key in target_keys:
            ch = telemetry.get(key)
            if ch is None or ch.kind != "numeric":
                continue
            vals = ch.values
            if kind in ("smooth", "moving_average", "ma"):
                ch.values = _smooth_array(vals, int(spec.get("window", 5)))
            elif kind in ("median", "despike"):
                med = _median_filter(vals, int(spec.get("window", 5)))
                sigma = spec.get("sigma")
                if sigma is not None:
                    diff = np.abs(vals - med)
                    mad = float(np.nanmedian(diff)) * 1.4826
                    thresh = float(sigma) * max(mad, 1e-9)
                    ch.values = np.where(diff > thresh, med, vals)
                else:
                    ch.values = med
            elif kind == "ema":
                ch.values = _ema_filter(vals, float(spec.get("alpha", 0.2)))
            elif kind == "lowpass":
                ch.values = _lowpass_filter(
                    vals, telemetry.time, float(spec.get("cutoff_hz", spec.get("cutoff", 2.0)))
                )
            elif kind == "clip":
                lo = float(spec.get("min", -math.inf))
                hi = float(spec.get("max", math.inf))
                ch.values = np.clip(vals, lo, hi)
            else:
                raise ValueError(
                    f"unknown filter type {kind!r}; expected one of: "
                    "smooth, median, despike, ema, lowpass, clip, gps_filter, resample"
                )
            ch.invalidate_cache()

    return telemetry


# ---------------------------------------------------------------------------
# Multi-source merging & chapter concatenation
# ---------------------------------------------------------------------------

def merge_telemetry(
    base: Telemetry,
    other: Telemetry,
    *,
    offset: float | None = None,
    prefix: str = "",
    overwrite: bool = False,
) -> Telemetry:
    """Merge channels from ``other`` onto ``base``'s time base.

    If ``offset`` is ``None`` and both logs carry UTC ``start_epoch`` metadata
    (e.g. GoPro MP4 + Garmin FIT or GPX), they are automatically aligned by
    wall-clock UTC time! Otherwise ``offset`` (seconds) shifts ``other``
    relative to ``base`` (positive = ``other`` started later than ``base``).
    """
    if offset is None:
        base_ep = base.meta.get("start_epoch")
        other_ep = other.meta.get("start_epoch")
        if isinstance(base_ep, (int, float)) and isinstance(other_ep, (int, float)):
            shift = float(other_ep) - float(base_ep)
        else:
            shift = 0.0
    else:
        shift = float(offset)

    other_t = other.time + shift
    if other_t.size == 0:
        return base

    for key, ch in other.channels.items():
        target_key = f"{prefix}{key}" if prefix else key
        if target_key in base.channels and not overwrite:
            continue
        if ch.kind == "numeric":
            vals = np.interp(base.time, other_t, ch.values, left=math.nan, right=math.nan)
            base.add(Channel(target_key, ch.label, ch.unit, vals, "numeric"))
        else:
            idx = np.clip(np.searchsorted(other_t, base.time), 0, other_t.size - 1)
            vals = ch.values[idx]
            txt = ch.text_values[idx] if ch.text_values is not None else None
            base.add(Channel(target_key, ch.label, ch.unit, vals, ch.kind, txt))
    return base


def concat_telemetry(parts: Sequence[Telemetry], gap: float = 0.0) -> Telemetry:
    """Concatenate consecutive telemetry files (e.g. GoPro chapters) end-to-end."""
    parts = [p for p in parts if p.time.size > 0]
    if not parts:
        raise ValueError("cannot concatenate an empty list of telemetry logs")
    if len(parts) == 1:
        return parts[0]

    # Use UTC start_epoch if every part has it, else stack end-to-end
    all_epochs = all(isinstance(p.meta.get("start_epoch"), (int, float)) for p in parts)
    t_arrays: list[np.ndarray] = []
    if all_epochs:
        # Sort chronologically by start_epoch
        parts = sorted(parts, key=lambda p: float(p.meta["start_epoch"]))
        base_ep = float(parts[0].meta["start_epoch"])
        for p in parts:
            t_arrays.append(p.time + (float(p.meta["start_epoch"]) - base_ep))
    else:
        cursor = 0.0
        for i, p in enumerate(parts):
            t_shift = p.time - p.t0 + cursor
            t_arrays.append(t_shift)
            step = 1.0 / p.sample_rate if p.sample_rate > 0 else 0.01
            cursor = float(t_shift[-1]) + max(gap, step)

    all_t = np.concatenate(t_arrays)
    order = np.argsort(all_t, kind="stable")
    all_t = all_t[order]

    merged_meta = dict(parts[0].meta)
    merged_meta["parts"] = [p.source for p in parts]
    out = Telemetry(time=all_t, meta=merged_meta, source=parts[0].source)

    # Union of all channel keys across parts
    keys: list[str] = []
    seen: set[str] = set()
    for p in parts:
        for k in p.channels:
            if k not in seen:
                seen.add(k)
                keys.append(k)

    for k in keys:
        template = next(p.channels[k] for p in parts if k in p.channels)
        val_chunks: list[np.ndarray] = []
        txt_chunks: list[np.ndarray] = []
        has_txt = template.kind == "text"
        for p in parts:
            if k in p.channels:
                ch = p.channels[k]
                val_chunks.append(ch.values)
                if has_txt:
                    txt_chunks.append(
                        ch.text_values
                        if ch.text_values is not None
                        else np.full(p.time.size, "", dtype=object)
                    )
            else:
                val_chunks.append(np.full(p.time.size, math.nan, dtype=float))
                if has_txt:
                    txt_chunks.append(np.full(p.time.size, "", dtype=object))
        vals = np.concatenate(val_chunks)[order]
        txt = np.concatenate(txt_chunks)[order] if has_txt else None
        out.add(Channel(k, template.label, template.unit, vals, template.kind, txt))
    return out
