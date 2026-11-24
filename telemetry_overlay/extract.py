"""Export / convert extracted telemetry to CSV, JSON, GPX, KML, GeoJSON, MGJSON and SRT.

Gives users everything GoPro Telemetry Extractor does — offline, scriptable,
and for *any* input format (GoPro MP4, DJI SRT, Garmin FIT, GPX, IGC, NMEA,
CSV, XLSX, JSON), including computed derived channels and filtered streams.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

import numpy as np

from .data import Telemetry

__all__ = ["export_telemetry", "EXPORT_FORMATS"]

EXPORT_FORMATS: dict[str, str] = {
    "csv": "Comma-separated values (Excel / Python / MATLAB / DaVinci)",
    "json": "Structured JSON with metadata and per-channel arrays",
    "gpx": "GPX 1.1 GPS track with TrackPointExtension (HR, cadence, power, temp)",
    "kml": "Google Earth 3D KML track (gx:Track + LineString)",
    "geojson": "GIS-ready GeoJSON FeatureCollection",
    "mgjson": "Adobe After Effects Motion Graphics JSON (.mgjson)",
    "srt": "SubRip telemetry subtitle overlay (.srt)",
}

_EXT_MAP = {
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".gpx": "gpx",
    ".kml": "kml",
    ".geojson": "geojson",
    ".mgjson": "mgjson",
    ".srt": "srt",
}


def _iso_utc(t_sec: float, start_epoch: float | None) -> str:
    base = start_epoch if start_epoch is not None else 0.0
    ts = dt.datetime.fromtimestamp(base + t_sec, tz=dt.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fmt_srt_tc(seconds: float) -> str:
    s = max(0.0, seconds)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int(round((s - math.floor(s)) * 1000))
    if ms >= 1000:
        ms = 0
        sec += 1
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _pick_channels(telemetry: Telemetry, channels: Sequence[str] | None) -> list[str]:
    if not channels:
        return list(telemetry.channels.keys())
    out = []
    for c in channels:
        k = telemetry.resolve(c)
        if k is None:
            raise ValueError(
                f"unknown channel {c!r}; available: {', '.join(sorted(telemetry.channels))}"
            )
        out.append(k)
    return out


def _export_csv(
    telemetry: Telemetry, path: Path, keys: list[str], delimiter: str = ","
) -> None:
    headers = ["time [s]"]
    for k in keys:
        ch = telemetry[k]
        headers.append(f"{ch.label} [{ch.unit}]" if ch.unit else ch.label)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        writer.writerow(headers)
        for i in range(telemetry.time.size):
            row: list[Any] = [f"{telemetry.time[i]:.6f}"]
            for k in keys:
                ch = telemetry[k]
                if ch.kind == "text":
                    row.append(str(ch.text_values[i]) if ch.text_values is not None else "")
                else:
                    v = float(ch.values[i])
                    row.append("" if not math.isfinite(v) else f"{v:.6g}")
            writer.writerow(row)


def _export_json(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    payload: dict[str, Any] = {
        "source": telemetry.source,
        "meta": telemetry.meta,
        "time": [round(float(x), 6) for x in telemetry.time],
        "channels": {},
    }
    for k in keys:
        ch = telemetry[k]
        if ch.kind == "text":
            vals = [str(v) for v in (ch.text_values if ch.text_values is not None else [])]
        else:
            vals = [None if not math.isfinite(v) else float(v) for v in ch.values]
        payload["channels"][k] = {
            "label": ch.label,
            "unit": ch.unit,
            "kind": ch.kind,
            "values": vals,
        }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _export_gpx(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    lat_k = telemetry.resolve(["lat", "latitude", "gps_lat"])
    lon_k = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
    if not lat_k or not lon_k:
        raise ValueError("GPX export requires GPS latitude and longitude channels")
    alt_k = telemetry.resolve(["altitude", "alt", "ele", "elevation"])
    hr_k = telemetry.resolve(["heart_rate", "hr"])
    cad_k = telemetry.resolve(["cadence", "cad"])
    pwr_k = telemetry.resolve(["power", "watts"])
    tmp_k = telemetry.resolve(["temperature", "atemp", "temp"])
    spd_k = telemetry.resolve(["speed"])
    start_ep = telemetry.meta.get("start_epoch")
    start_ep_f = float(start_ep) if isinstance(start_ep, (int, float)) else None
    name = escape(str(telemetry.meta.get("name", Path(telemetry.source).stem or "Telemetry")))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="TelemetryOverlay" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        f"  <trk><name>{name}</name><trkseg>",
    ]
    lats = telemetry[lat_k].values
    lons = telemetry[lon_k].values
    for i in range(telemetry.time.size):
        lat, lon = float(lats[i]), float(lons[i])
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
        if alt_k and math.isfinite(telemetry[alt_k].values[i]):
            lines.append(f"      <ele>{telemetry[alt_k].values[i]:.2f}</ele>")
        lines.append(f"      <time>{_iso_utc(float(telemetry.time[i]), start_ep_f)}</time>")
        ext_parts: list[str] = []
        if hr_k and math.isfinite(telemetry[hr_k].values[i]):
            ext_parts.append(f"<gpxtpx:hr>{int(round(telemetry[hr_k].values[i]))}</gpxtpx:hr>")
        if cad_k and math.isfinite(telemetry[cad_k].values[i]):
            ext_parts.append(f"<gpxtpx:cad>{int(round(telemetry[cad_k].values[i]))}</gpxtpx:cad>")
        if tmp_k and math.isfinite(telemetry[tmp_k].values[i]):
            ext_parts.append(f"<gpxtpx:atemp>{telemetry[tmp_k].values[i]:.2f}</gpxtpx:atemp>")
        if spd_k and math.isfinite(telemetry[spd_k].values[i]):
            ext_parts.append(f"<gpxtpx:speed>{telemetry[spd_k].values[i]:.3f}</gpxtpx:speed>")
        pwr_xml = (
            f"<power>{int(round(telemetry[pwr_k].values[i]))}</power>"
            if (pwr_k and math.isfinite(telemetry[pwr_k].values[i]))
            else ""
        )
        if ext_parts or pwr_xml:
            lines.append("      <extensions>")
            if pwr_xml:
                lines.append(f"        {pwr_xml}")
            if ext_parts:
                lines.append(
                    "        <gpxtpx:TrackPointExtension>"
                    + "".join(ext_parts)
                    + "</gpxtpx:TrackPointExtension>"
                )
            lines.append("      </extensions>")
        lines.append("    </trkpt>")
    lines.append("  </trkseg></trk>")
    lines.append("</gpx>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_kml(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    lat_k = telemetry.resolve(["lat", "latitude", "gps_lat"])
    lon_k = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
    if not lat_k or not lon_k:
        raise ValueError("KML export requires GPS latitude and longitude channels")
    alt_k = telemetry.resolve(["altitude", "alt", "ele", "elevation"])
    start_ep = telemetry.meta.get("start_epoch")
    start_ep_f = float(start_ep) if isinstance(start_ep, (int, float)) else None
    name = escape(str(telemetry.meta.get("name", Path(telemetry.source).stem or "Telemetry")))

    lats = telemetry[lat_k].values
    lons = telemetry[lon_k].values
    whens: list[str] = []
    coords: list[str] = []
    for i in range(telemetry.time.size):
        lat, lon = float(lats[i]), float(lons[i])
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        alt = float(telemetry[alt_k].values[i]) if (alt_k and math.isfinite(telemetry[alt_k].values[i])) else 0.0
        whens.append(f"        <when>{_iso_utc(float(telemetry.time[i]), start_ep_f)}</when>")
        coords.append(f"        <gx:coord>{lon:.7f} {lat:.7f} {alt:.2f}</gx:coord>")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">',
        "  <Document>",
        f"    <name>{name}</name>",
        "    <Placemark>",
        f"      <name>{name}</name>",
        "      <gx:Track>",
        "        <altitudeMode>absolute</altitudeMode>",
        *whens,
        *coords,
        "      </gx:Track>",
        "    </Placemark>",
        "  </Document>",
        "</kml>",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_geojson(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    lat_k = telemetry.resolve(["lat", "latitude", "gps_lat"])
    lon_k = telemetry.resolve(["lon", "lng", "longitude", "gps_lon"])
    if not lat_k or not lon_k:
        raise ValueError("GeoJSON export requires GPS latitude and longitude channels")
    alt_k = telemetry.resolve(["altitude", "alt", "ele", "elevation"])
    start_ep = telemetry.meta.get("start_epoch")
    start_ep_f = float(start_ep) if isinstance(start_ep, (int, float)) else None

    lats = telemetry[lat_k].values
    lons = telemetry[lon_k].values
    coords: list[list[float]] = []
    coord_times: list[str] = []
    extra_props: dict[str, list[Any]] = {
        k: [] for k in keys if k not in (lat_k, lon_k, alt_k)
    }
    for i in range(telemetry.time.size):
        lat, lon = float(lats[i]), float(lons[i])
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if alt_k and math.isfinite(telemetry[alt_k].values[i]):
            coords.append([round(lon, 7), round(lat, 7), round(float(telemetry[alt_k].values[i]), 2)])
        else:
            coords.append([round(lon, 7), round(lat, 7)])
        coord_times.append(_iso_utc(float(telemetry.time[i]), start_ep_f))
        for k in extra_props:
            ch = telemetry[k]
            if ch.kind == "text":
                extra_props[k].append(str(ch.text_values[i]) if ch.text_values is not None else "")
            else:
                v = float(ch.values[i])
                extra_props[k].append(None if not math.isfinite(v) else round(v, 5))

    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "name": Path(telemetry.source).stem or "Telemetry",
                "coordTimes": coord_times,
                **extra_props,
            },
        }],
    }
    path.write_text(json.dumps(fc, indent=2, ensure_ascii=False), encoding="utf-8")


def _export_mgjson(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    """Adobe After Effects Motion Graphics JSON (.mgjson) format."""
    start_ep = telemetry.meta.get("start_epoch")
    start_ep_f = float(start_ep) if isinstance(start_ep, (int, float)) else None
    t = telemetry.time
    n = t.size
    outlines: list[dict[str, Any]] = []
    data_samples: list[dict[str, Any]] = []

    for k in keys:
        ch = telemetry[k]
        if ch.kind == "text":
            continue
        lo, hi = ch.range()
        match_name = f"TelemetryOverlay_{k}"
        outlines.append({
            "objectType": "dataDynamic",
            "displayName": f"{ch.label} ({ch.unit})" if ch.unit else ch.label,
            "matchName": match_name,
            "dataType": {
                "type": "numberString",
                "numberStringProperties": {
                    "pattern": {"digitsInteger": 6, "digitsDecimal": 3, "isSigned": True},
                    "range": {"occuring": {"min": lo, "max": hi}, "legal": {"min": -1e9, "max": 1e9}},
                },
            },
            "interpolation": "linear",
            "sampleCount": n,
        })
        samples = [
            {
                "time": _iso_utc(float(t[i]), start_ep_f),
                "value": 0.0 if not math.isfinite(ch.values[i]) else round(float(ch.values[i]), 5),
            }
            for i in range(n)
        ]
        data_samples.append({"matchName": match_name, "samples": samples})

    doc = {
        "version": "MGJSON2.0.0",
        "creator": "TelemetryOverlay",
        "dynamicSamplesPresentB": True,
        "dynamicDataInfo": {
            "useTimecodeB": False,
            "utcInfo": {
                "precisionLength": 3,
                "isGMT": True,
            },
        },
        "dataOutline": outlines,
        "dataDynamicSamples": data_samples,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _export_srt(telemetry: Telemetry, path: Path, keys: list[str]) -> None:
    """Export telemetry as a standard SubRip (.srt) subtitle overlay."""
    t = telemetry.time
    n = t.size
    # Pick up to 6 concise channels for readable subtitles
    show_keys = keys[:6]
    blocks: list[str] = []
    for i in range(n):
        t0 = float(t[i] - t[0]) if n else 0.0
        t1 = float(t[i + 1] - t[0]) if i + 1 < n else t0 + 0.1
        if t1 <= t0:
            t1 = t0 + 0.033
        parts: list[str] = []
        for k in show_keys:
            ch = telemetry[k]
            if ch.kind == "text":
                val_s = str(ch.text_values[i]) if ch.text_values is not None else ""
            else:
                v = float(ch.values[i])
                val_s = "--" if not math.isfinite(v) else f"{v:.2f}"
            unit_s = f" {ch.unit}" if ch.unit else ""
            parts.append(f"{ch.label}: {val_s}{unit_s}")
        blocks.append(f"{i + 1}\n{_fmt_srt_tc(t0)} --> {_fmt_srt_tc(t1)}\n" + " | ".join(parts))
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def export_telemetry(
    telemetry: Telemetry,
    output: str | Path,
    *,
    fmt: str | None = None,
    channels: Sequence[str] | None = None,
    delimiter: str = ",",
) -> dict[str, Any]:
    """Export a Telemetry object into CSV, JSON, GPX, KML, GeoJSON, MGJSON or SRT."""
    out = Path(output)
    target_fmt = (fmt or _EXT_MAP.get(out.suffix.lower()) or "csv").strip().lower()
    if target_fmt not in EXPORT_FORMATS:
        raise ValueError(
            f"unknown export format {target_fmt!r}; available: {', '.join(sorted(EXPORT_FORMATS))}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    keys = _pick_channels(telemetry, channels)

    if target_fmt == "csv":
        _export_csv(telemetry, out, keys, delimiter=delimiter)
    elif target_fmt == "json":
        _export_json(telemetry, out, keys)
    elif target_fmt == "gpx":
        _export_gpx(telemetry, out, keys)
    elif target_fmt == "kml":
        _export_kml(telemetry, out, keys)
    elif target_fmt == "geojson":
        _export_geojson(telemetry, out, keys)
    elif target_fmt == "mgjson":
        _export_mgjson(telemetry, out, keys)
    elif target_fmt == "srt":
        _export_srt(telemetry, out, keys)

    return {
        "output": str(out),
        "format": target_fmt,
        "samples": int(telemetry.time.size),
        "channels": len(keys),
        "bytes": out.stat().st_size if out.exists() else 0,
    }
