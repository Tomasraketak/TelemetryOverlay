"""DJI drone & Osmo SRT telemetry parser (standalone .srt or embedded in .mp4)."""

from __future__ import annotations

import datetime as dt
import math
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from ..data import Channel, LoadOptions, Telemetry

__all__ = ["load_dji_srt", "extract_embedded_srt"]

_TC_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_BRACKET_KV_RE = re.compile(r"\[([^\]]+)\]")
_PAIR_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_ ]*?)\s*[:=]\s*([-+]?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)")
_CLASSIC_GPS_RE = re.compile(
    r"GPS\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)(?:\s*,\s*([-+]?\d+(?:\.\d+)?))?\s*\)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(\d{4}[-.]\d{2}[-.]\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def _tc_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600.0 + int(m) * 60.0 + int(s) + int(ms) / 1000.0


def _parse_num_or_frac(text: str) -> float:
    text = text.strip()
    if "/" in text:
        a, _, b = text.partition("/")
        try:
            bv = float(b)
            return float(a) / bv if bv != 0 else math.nan
        except ValueError:
            return math.nan
    try:
        return float(text)
    except ValueError:
        return math.nan


_KEY_NORMALIZE: dict[str, tuple[str, str, str, float]] = {
    # normalized raw key -> (channel_key, label, unit, multiplier)
    "latitude": ("lat", "Latitude", "°", 1.0),
    "lat": ("lat", "Latitude", "°", 1.0),
    "longitude": ("lon", "Longitude", "°", 1.0),
    "long": ("lon", "Longitude", "°", 1.0),
    "lon": ("lon", "Longitude", "°", 1.0),
    "rel_alt": ("rel_altitude", "Relative Altitude", "m", 1.0),
    "abs_alt": ("altitude", "Altitude", "m", 1.0),
    "altitude": ("altitude", "Altitude", "m", 1.0),
    "barometer": ("rel_altitude", "Relative Altitude", "m", 1.0),
    "iso": ("iso", "ISO", "", 1.0),
    "shutter": ("shutter", "Shutter", "s", 1.0),
    "fnum": ("fnum", "Aperture", "f/", 1.0),
    "ev": ("ev", "EV", "EV", 1.0),
    "ct": ("color_temp", "Color Temp", "K", 1.0),
    "focal_len": ("focal_length", "Focal Length", "mm", 1.0),
    "gb_yaw": ("gimbal_yaw", "Gimbal Yaw", "°", 1.0),
    "gb_pitch": ("gimbal_pitch", "Gimbal Pitch", "°", 1.0),
    "gb_roll": ("gimbal_roll", "Gimbal Roll", "°", 1.0),
    "yaw": ("yaw", "Yaw", "°", 1.0),
    "pitch": ("pitch", "Pitch", "°", 1.0),
    "roll": ("roll", "Roll", "°", 1.0),
    "speed": ("speed", "Speed", "m/s", 1.0),
    "h": ("rel_altitude", "Relative Altitude", "m", 1.0),
    "d": ("home_distance", "Home Distance", "m", 1.0),
    "h_s": ("speed", "Horizontal Speed", "m/s", 1.0),
    "v_s": ("vspeed", "Vertical Speed", "m/s", 1.0),
}


def parse_dji_srt_text(text: str, source: str) -> Telemetry:
    """Parse DJI SRT subtitle text into a Telemetry object."""
    blocks = re.split(r"\r?\n\r?\n+", text.strip())
    times: list[float] = []
    rows: list[dict[str, float]] = []
    meta: dict[str, Any] = {"format": "dji_srt"}
    first_dt: float | None = None

    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        tc_match = None
        body_start = 0
        for i, ln in enumerate(lines):
            m = _TC_RE.search(ln)
            if m:
                tc_match = m
                body_start = i + 1
                break
        if tc_match is None:
            continue
        t = _tc_seconds(*tc_match.groups()[:4])
        body = " ".join(lines[body_start:])
        # Strip simple HTML font tags used by DJI Mini/Mavic 3 SRTs
        body = re.sub(r"<[^>]+>", " ", body)

        if first_dt is None:
            dm = _DATE_RE.search(body)
            if dm:
                try:
                    raw_d = dm.group(1).replace(".", "-")
                    # Some DJI dates have YYYY-MM-DD HH:MM:SS.fff
                    parsed = dt.datetime.fromisoformat(raw_d)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=dt.timezone.utc)
                    first_dt = parsed.timestamp() - t
                except ValueError:
                    pass

        rec: dict[str, float] = {}
        # Classic DJI GPS (lon, lat, sat)
        cm = _CLASSIC_GPS_RE.search(body)
        if cm:
            rec["lon"] = float(cm.group(1))
            rec["lat"] = float(cm.group(2))
            if cm.group(3):
                rec["satellites"] = float(cm.group(3))

        # Modern bracketed tokens like [rel_alt: 45.2 abs_alt: 312.5] [iso: 100]
        chunks = _BRACKET_KV_RE.findall(body)
        scan_text = " ".join(chunks) if chunks else body
        for k_raw, v_raw in _PAIR_RE.findall(scan_text):
            k_norm = re.sub(r"[^a-z0-9]+", "_", k_raw.strip().lower()).strip("_")
            val = _parse_num_or_frac(v_raw)
            if not math.isfinite(val):
                continue
            if k_norm in _KEY_NORMALIZE:
                ch_key, _, _, mult = _KEY_NORMALIZE[k_norm]
                # DJI fnum is often stored as 280 for f/2.8
                if ch_key == "fnum" and val > 50:
                    val /= 100.0
                if ch_key == "focal_length" and val > 100:
                    val /= 10.0
                rec[ch_key] = val * mult

        if rec:
            times.append(t)
            rows.append(rec)

    if not times:
        raise ValueError(f"{Path(source).name}: no DJI telemetry entries found in SRT")

    if first_dt is not None:
        meta["start_epoch"] = first_dt
        meta["start_time_utc"] = dt.datetime.fromtimestamp(
            first_dt, tz=dt.timezone.utc
        ).isoformat()

    t_arr = np.asarray(times, dtype=float)
    order = np.argsort(t_arr, kind="stable")
    t_arr = t_arr[order]
    tel = Telemetry(time=t_arr, meta=meta, source=source)

    label_info = {v[0]: (v[1], v[2]) for v in _KEY_NORMALIZE.values()}
    label_info["satellites"] = ("Satellites", "")
    all_keys = sorted({k for r in rows for k in r})
    for key in all_keys:
        vals = np.array([r.get(key, math.nan) for r in rows], dtype=float)[order]
        if np.isfinite(vals).any():
            lbl, unit = label_info.get(key, (key.replace("_", " ").title(), ""))
            tel.add(Channel(key=key, label=lbl, unit=unit, values=vals, kind="numeric"))

    # If only rel_altitude is present and no abs altitude, alias altitude -> rel_altitude
    if "altitude" not in tel and "rel_altitude" in tel:
        rel = tel["rel_altitude"]
        tel.add(Channel(key="altitude", label="Altitude", unit="m",
                        values=rel.values.copy(), kind="numeric"))
    return tel


def load_dji_srt(path: Path, opts: LoadOptions) -> Telemetry:
    """Load a standalone DJI .SRT telemetry subtitle file."""
    text = path.read_text(encoding=opts.encoding or "utf-8-sig", errors="replace")
    return parse_dji_srt_text(text, str(path))


def extract_embedded_srt(video_path: Path) -> Telemetry | None:
    """Extract an embedded DJI subtitle telemetry track from an MP4/MOV via ffmpeg."""
    from ..export import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-map", "0:s:0", "-f", "srt", "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    text = res.stdout.decode("utf-8", errors="replace")
    try:
        tel = parse_dji_srt_text(text, str(video_path))
        tel.meta["format"] = "dji_mp4_srt"
        return tel
    except ValueError:
        return None
