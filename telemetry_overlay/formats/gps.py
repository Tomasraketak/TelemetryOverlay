"""GPS & aviation telemetry loaders: GPX, KML/KMZ, TCX, NMEA 0183, IGC."""

from __future__ import annotations

import datetime as dt
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

from ..data import Channel, LoadOptions, Telemetry, _slug
from .tabular import parse_timestamp_cell

__all__ = ["load_gpx", "load_kml", "load_tcx", "load_nmea", "load_igc"]


def _local(tag: str) -> str:
    """Strip XML namespace prefix from a tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _build_gps_telemetry(
    path: Path,
    times: list[float],
    series: dict[str, tuple[str, str, list[float]]],
    meta: dict[str, Any],
    fmt_name: str,
) -> Telemetry:
    if not times:
        raise ValueError(f"{path.name}: no telemetry samples found")

    t_arr = np.asarray(times, dtype=float)
    valid = np.isfinite(t_arr)
    t_arr = t_arr[valid]
    if t_arr.size == 0:
        raise ValueError(f"{path.name}: no valid timestamps found")

    order = np.argsort(t_arr, kind="stable")
    t_arr = t_arr[order]
    if t_arr[0] > 1e8:
        start_epoch = float(t_arr[0])
        meta.setdefault("start_epoch", start_epoch)
        meta.setdefault(
            "start_time_utc",
            dt.datetime.fromtimestamp(start_epoch, tz=dt.timezone.utc).isoformat(),
        )
        t_arr = t_arr - start_epoch

    meta.setdefault("format", fmt_name)
    tel = Telemetry(time=t_arr, meta=meta, source=str(path))
    for key, (label, unit, vals) in series.items():
        arr = np.asarray(vals, dtype=float)[valid][order]
        if np.isfinite(arr).any():
            tel.add(Channel(key=key, label=label, unit=unit, values=arr, kind="numeric"))
    return tel


# ---------------------------------------------------------------------------
# GPX (GPS Exchange Format 1.0 / 1.1 + Garmin/Strava/Wahoo extensions)
# ---------------------------------------------------------------------------

_GPX_EXT_MAP: dict[str, tuple[str, str, str]] = {
    "hr": ("heart_rate", "Heart Rate", "bpm"),
    "heartrate": ("heart_rate", "Heart Rate", "bpm"),
    "cad": ("cadence", "Cadence", "rpm"),
    "cadence": ("cadence", "Cadence", "rpm"),
    "atemp": ("temperature", "Temperature", "°C"),
    "temp": ("temperature", "Temperature", "°C"),
    "wtemp": ("water_temp", "Water Temp", "°C"),
    "power": ("power", "Power", "W"),
    "watts": ("power", "Power", "W"),
    "speed": ("speed", "Speed", "m/s"),
    "course": ("bearing", "Bearing", "°"),
    "depth": ("depth", "Depth", "m"),
    "pressure": ("pressure", "Pressure", "hPa"),
}


def load_gpx(path: Path, opts: LoadOptions) -> Telemetry:
    """Load a GPX 1.0/1.1 track with elevation, speed and fitness extensions."""
    tree = ET.parse(path)
    root = tree.getroot()
    meta: dict[str, Any] = {}

    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "name" and elem.text and "name" not in meta:
            meta["name"] = elem.text.strip()
        elif tag == "desc" and elem.text and "description" not in meta:
            meta["description"] = elem.text.strip()

    pts = [e for e in root.iter() if _local(e.tag) in ("trkpt", "rtept")]
    if not pts:
        raise ValueError(f"{path.name}: no trackpoints (<trkpt>) found in GPX")

    times: list[float] = []
    data: dict[str, list[float]] = {"lat": [], "lon": [], "altitude": []}
    labels = {"lat": ("Latitude", "°"), "lon": ("Longitude", "°"), "altitude": ("Altitude", "m")}
    ext_keys: dict[str, tuple[str, str]] = {}

    # Pre-scan extension keys so every row has aligned arrays
    raw_rows: list[tuple[float, float, float, float, dict[str, float]]] = []
    for idx, pt in enumerate(pts):
        lat = float(pt.attrib.get("lat", "nan"))
        lon = float(pt.attrib.get("lon", "nan"))
        ele = math.nan
        t = math.nan
        extras: dict[str, float] = {}
        for child in pt:
            ctag = _local(child.tag)
            if ctag == "ele" and child.text:
                try:
                    ele = float(child.text.strip())
                except ValueError:
                    pass
            elif ctag == "time" and child.text:
                t = parse_timestamp_cell(child.text.strip())
            elif ctag == "speed" and child.text:
                try:
                    extras["speed"] = float(child.text.strip())
                    ext_keys.setdefault("speed", ("Speed", "m/s"))
                except ValueError:
                    pass
            elif ctag == "course" and child.text:
                try:
                    extras["bearing"] = float(child.text.strip())
                    ext_keys.setdefault("bearing", ("Bearing", "°"))
                except ValueError:
                    pass
            elif ctag == "sat" and child.text:
                try:
                    extras["satellites"] = float(child.text.strip())
                    ext_keys.setdefault("satellites", ("Satellites", ""))
                except ValueError:
                    pass
            elif ctag == "hdop" and child.text:
                try:
                    extras["hdop"] = float(child.text.strip())
                    ext_keys.setdefault("hdop", ("HDOP", ""))
                except ValueError:
                    pass
            elif ctag == "extensions":
                for sub in child.iter():
                    if sub is child or not sub.text or not sub.text.strip():
                        continue
                    stag = _local(sub.tag).lower()
                    try:
                        val = float(sub.text.strip())
                    except ValueError:
                        continue
                    if stag in _GPX_EXT_MAP:
                        k, lbl, u = _GPX_EXT_MAP[stag]
                    else:
                        k = _slug(stag)
                        lbl, u = stag.replace("_", " ").title(), ""
                    extras[k] = val
                    ext_keys.setdefault(k, (lbl, u))
        if not math.isfinite(t):
            t = float(idx)
        raw_rows.append((t, lat, lon, ele, extras))

    for k in ext_keys:
        data.setdefault(k, [])
    for t, lat, lon, ele, extras in raw_rows:
        times.append(t)
        data["lat"].append(lat)
        data["lon"].append(lon)
        data["altitude"].append(ele)
        for k in ext_keys:
            data[k].append(extras.get(k, math.nan))

    series: dict[str, tuple[str, str, list[float]]] = {}
    for k, vals in data.items():
        lbl, u = labels.get(k) or ext_keys.get(k, (k, ""))
        series[k] = (lbl, u, vals)
    return _build_gps_telemetry(path, times, series, meta, "gpx")


# ---------------------------------------------------------------------------
# KML / KMZ (Google Earth)
# ---------------------------------------------------------------------------

def load_kml(path: Path, opts: LoadOptions) -> Telemetry:
    """Load KML or zipped KMZ telemetry (gx:Track or LineString)."""
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path, "r") as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError(f"{path.name}: no .kml entry inside KMZ archive")
            xml_bytes = zf.read(kml_names[0])
        root = ET.fromstring(xml_bytes)
        fmt = "kmz"
    else:
        root = ET.parse(path).getroot()
        fmt = "kml"

    meta: dict[str, Any] = {}
    times: list[float] = []
    lats: list[float] = []
    lons: list[float] = []
    alts: list[float] = []
    extra_arrays: dict[str, list[float]] = {}

    # 1. Prefer gx:Track (has timestamps + extended arrays)
    tracks = [e for e in root.iter() if _local(e.tag) == "Track"]
    if tracks:
        for trk in tracks:
            whens = [parse_timestamp_cell(c.text.strip()) for c in trk if _local(c.tag) == "when" and c.text]
            coords = [c.text.strip().split() for c in trk if _local(c.tag) == "coord" and c.text]
            n = min(len(whens), len(coords)) if whens else len(coords)
            for i in range(n):
                parts = coords[i]
                if len(parts) < 2:
                    continue
                times.append(whens[i] if i < len(whens) else float(len(times)))
                lons.append(float(parts[0]))
                lats.append(float(parts[1]))
                alts.append(float(parts[2]) if len(parts) >= 3 else math.nan)
            for arr in trk.iter():
                if _local(arr.tag) == "SimpleArrayData":
                    name = _slug(arr.attrib.get("name", "extra"))
                    vals = []
                    for v in arr:
                        if _local(v.tag) == "value" and v.text:
                            try:
                                vals.append(float(v.text.strip()))
                            except ValueError:
                                vals.append(math.nan)
                    if len(vals) == n:
                        extra_arrays.setdefault(name, []).extend(vals)
    else:
        # 2. Fallback: LineString / coordinates
        for elem in root.iter():
            if _local(elem.tag) == "coordinates" and elem.text:
                tokens = elem.text.strip().split()
                for idx, tok in enumerate(tokens):
                    parts = tok.split(",")
                    if len(parts) < 2:
                        continue
                    times.append(float(len(times)))
                    lons.append(float(parts[0]))
                    lats.append(float(parts[1]))
                    alts.append(float(parts[2]) if len(parts) >= 3 else math.nan)

    series: dict[str, tuple[str, str, list[float]]] = {
        "lat": ("Latitude", "°", lats),
        "lon": ("Longitude", "°", lons),
        "altitude": ("Altitude", "m", alts),
    }
    for k, vals in extra_arrays.items():
        if len(vals) == len(times):
            series[k] = (k.replace("_", " ").title(), "", vals)
    return _build_gps_telemetry(path, times, series, meta, fmt)


# ---------------------------------------------------------------------------
# TCX (Garmin Training Center XML)
# ---------------------------------------------------------------------------

def load_tcx(path: Path, opts: LoadOptions) -> Telemetry:
    """Load Garmin TCX fitness/GPS XML logs."""
    root = ET.parse(path).getroot()
    meta: dict[str, Any] = {}
    for elem in root.iter():
        if _local(elem.tag) == "Activity" and "Sport" in elem.attrib:
            meta["sport"] = elem.attrib["Sport"]
            break

    tps = [e for e in root.iter() if _local(e.tag) == "Trackpoint"]
    if not tps:
        raise ValueError(f"{path.name}: no <Trackpoint> elements found in TCX")

    times: list[float] = []
    cols: dict[str, list[float]] = {
        "lat": [], "lon": [], "altitude": [], "distance": [],
        "heart_rate": [], "cadence": [], "speed": [], "power": [],
    }
    for idx, tp in enumerate(tps):
        t = math.nan
        row = {k: math.nan for k in cols}
        for child in tp:
            tag = _local(child.tag)
            if tag == "Time" and child.text:
                t = parse_timestamp_cell(child.text.strip())
            elif tag == "Position":
                for p in child:
                    ptag = _local(p.tag)
                    if ptag == "LatitudeDegrees" and p.text:
                        row["lat"] = float(p.text.strip())
                    elif ptag == "LongitudeDegrees" and p.text:
                        row["lon"] = float(p.text.strip())
            elif tag == "AltitudeMeters" and child.text:
                row["altitude"] = float(child.text.strip())
            elif tag == "DistanceMeters" and child.text:
                row["distance"] = float(child.text.strip())
            elif tag == "HeartRateBpm":
                for v in child:
                    if _local(v.tag) == "Value" and v.text:
                        row["heart_rate"] = float(v.text.strip())
            elif tag == "Cadence" and child.text:
                row["cadence"] = float(child.text.strip())
            elif tag == "Extensions":
                for sub in child.iter():
                    stag = _local(sub.tag)
                    if stag == "Speed" and sub.text:
                        row["speed"] = float(sub.text.strip())
                    elif stag == "Watts" and sub.text:
                        row["power"] = float(sub.text.strip())
                    elif stag == "RunCadence" and sub.text and not math.isfinite(row["cadence"]):
                        row["cadence"] = float(sub.text.strip())
        times.append(t if math.isfinite(t) else float(idx))
        for k in cols:
            cols[k].append(row[k])

    labels = {
        "lat": ("Latitude", "°"),
        "lon": ("Longitude", "°"),
        "altitude": ("Altitude", "m"),
        "distance": ("Distance", "m"),
        "heart_rate": ("Heart Rate", "bpm"),
        "cadence": ("Cadence", "rpm"),
        "speed": ("Speed", "m/s"),
        "power": ("Power", "W"),
    }
    series = {k: (labels[k][0], labels[k][1], v) for k, v in cols.items()}
    return _build_gps_telemetry(path, times, series, meta, "tcx")


# ---------------------------------------------------------------------------
# NMEA 0183 ($GPGGA, $GPRMC, $GNGGA, $GNRMC, $GPVTG, $xxMWV, $xxHDT)
# ---------------------------------------------------------------------------

def _nmea_coord(raw: str, hemi: str) -> float:
    if not raw or not hemi:
        return math.nan
    try:
        dot = raw.index(".")
        deg_digits = dot - 2
        deg = float(raw[:deg_digits])
        minutes = float(raw[deg_digits:])
    except (ValueError, IndexError):
        return math.nan
    val = deg + minutes / 60.0
    if hemi.upper() in ("S", "W"):
        val = -val
    return val


def _nmea_time_sec(hhmmss: str) -> float:
    if not hhmmss or len(hhmmss) < 6:
        return math.nan
    try:
        h = int(hhmmss[0:2])
        m = int(hhmmss[2:4])
        s = float(hhmmss[4:])
        return h * 3600.0 + m * 60.0 + s
    except ValueError:
        return math.nan


def load_nmea(path: Path, opts: LoadOptions) -> Telemetry:
    """Load NMEA 0183 GPS / marine / aviation sentences."""
    text = path.read_text(encoding=opts.encoding or "ascii", errors="replace")
    # Group sentences by UTC second-of-day so GGA + RMC + MWV combine cleanly
    buckets: dict[float, dict[str, float]] = {}
    order_t: list[float] = []
    last_t = math.nan
    day_offset = 0.0
    date_prefix: dt.date | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("$"):
            continue
        if "*" in line:
            line = line.split("*", 1)[0]
        parts = line.split(",")
        if len(parts) < 4:
            continue
        sentence = parts[0][-3:].upper()

        if sentence == "RMC" and len(parts) >= 10:
            t = _nmea_time_sec(parts[1])
            if not math.isfinite(t):
                continue
            if math.isfinite(last_t) and t + day_offset < last_t - 43200.0:
                day_offset += 86400.0
            t += day_offset
            last_t = t
            if parts[9] and len(parts[9]) == 6 and date_prefix is None:
                try:
                    d, mo, yr = int(parts[9][0:2]), int(parts[9][2:4]), int(parts[9][4:6])
                    date_prefix = dt.date(2000 + yr, mo, d)
                except ValueError:
                    pass
            rec = buckets.setdefault(t, {})
            if len(buckets) > len(order_t):
                order_t.append(t)
            if parts[2].upper() == "A":
                lat = _nmea_coord(parts[3], parts[4])
                lon = _nmea_coord(parts[5], parts[6])
                if math.isfinite(lat) and math.isfinite(lon):
                    rec["lat"] = lat
                    rec["lon"] = lon
            if parts[7]:
                try:
                    rec["speed"] = float(parts[7]) * 0.514444  # knots -> m/s
                except ValueError:
                    pass
            if parts[8]:
                try:
                    rec["bearing"] = float(parts[8])
                except ValueError:
                    pass

        elif sentence == "GGA" and len(parts) >= 10:
            t = _nmea_time_sec(parts[1])
            if not math.isfinite(t):
                continue
            if math.isfinite(last_t) and t + day_offset < last_t - 43200.0:
                day_offset += 86400.0
            t += day_offset
            last_t = t
            rec = buckets.setdefault(t, {})
            if len(buckets) > len(order_t):
                order_t.append(t)
            lat = _nmea_coord(parts[2], parts[3])
            lon = _nmea_coord(parts[4], parts[5])
            if math.isfinite(lat) and math.isfinite(lon):
                rec["lat"] = lat
                rec["lon"] = lon
            for idx_col, key in ((6, "gps_fix"), (7, "satellites"), (8, "hdop"), (9, "altitude")):
                if parts[idx_col]:
                    try:
                        rec[key] = float(parts[idx_col])
                    except ValueError:
                        pass

        elif sentence == "MWV" and len(parts) >= 5 and math.isfinite(last_t):
            # Marine wind speed & angle
            rec = buckets.setdefault(last_t, {})
            try:
                rec["wind_angle"] = float(parts[1])
                wspd = float(parts[3])
                u = parts[4].upper()
                if u == "N":
                    wspd *= 0.514444
                elif u == "K":
                    wspd /= 3.6
                rec["wind_speed"] = wspd
            except ValueError:
                pass

        elif sentence in ("HDT", "HDM") and len(parts) >= 2 and math.isfinite(last_t):
            rec = buckets.setdefault(last_t, {})
            try:
                rec["heading"] = float(parts[1])
            except ValueError:
                pass

    if not order_t:
        raise ValueError(f"{path.name}: no NMEA GPS sentences ($GPGGA/$GPRMC) found")

    all_keys = sorted({k for rec in buckets.values() for k in rec})
    epoch_base = 0.0
    meta: dict[str, Any] = {}
    if date_prefix is not None:
        epoch_base = dt.datetime(
            date_prefix.year, date_prefix.month, date_prefix.day, tzinfo=dt.timezone.utc
        ).timestamp()

    times = [epoch_base + t for t in order_t]
    labels = {
        "lat": ("Latitude", "°"),
        "lon": ("Longitude", "°"),
        "altitude": ("Altitude", "m"),
        "speed": ("Speed", "m/s"),
        "bearing": ("Bearing", "°"),
        "heading": ("Heading", "°"),
        "satellites": ("Satellites", ""),
        "hdop": ("HDOP", ""),
        "gps_fix": ("GPS Fix", ""),
        "wind_angle": ("Wind Angle", "°"),
        "wind_speed": ("Wind Speed", "m/s"),
    }
    series: dict[str, tuple[str, str, list[float]]] = {}
    for k in all_keys:
        lbl, u = labels.get(k, (k.replace("_", " ").title(), ""))
        series[k] = (lbl, u, [buckets[t].get(k, math.nan) for t in order_t])
    return _build_gps_telemetry(path, times, series, meta, "nmea")


# ---------------------------------------------------------------------------
# IGC (FAI Gliding / Aviation Flight Recorder)
# ---------------------------------------------------------------------------

_IGC_B_RE = re.compile(
    r"^B(\d{2})(\d{2})(\d{2})(\d{2})(\d{5})([NS])(\d{3})(\d{5})([EW])([AV])([+-]?\d{4,5})([+-]?\d{4,5})(.*)$"
)
_IGC_DATE_RE = re.compile(r"^HFDT(?:E(?:DATE:)?|E)(\d{2})(\d{2})(\d{2})", re.IGNORECASE)


def load_igc(path: Path, opts: LoadOptions) -> Telemetry:
    """Load an FAI IGC aviation/gliding flight log."""
    text = path.read_text(encoding=opts.encoding or "utf-8", errors="replace")
    meta: dict[str, Any] = {}
    flight_date: dt.date | None = None
    ext_defs: list[tuple[int, int, str]] = []

    times: list[float] = []
    lats: list[float] = []
    lons: list[float] = []
    baro_alt: list[float] = []
    gnss_alt: list[float] = []
    extras: dict[str, list[float]] = {}
    day_offset = 0.0
    last_t = math.nan

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("H"):
            m_date = _IGC_DATE_RE.match(line)
            if m_date:
                d, mo, yr = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
                try:
                    flight_date = dt.date(2000 + yr, mo, d)
                except ValueError:
                    pass
            elif ":" in line:
                k, _, v = line[1:].partition(":")
                if k.strip() and v.strip():
                    meta[k.strip()] = v.strip()
        elif line.startswith("I") and len(line) >= 3:
            # I-record defines fixed-width extensions on B-records
            try:
                count = int(line[1:3])
                pos = 3
                for _ in range(count):
                    if pos + 7 <= len(line):
                        s_idx = int(line[pos:pos + 2]) - 1
                        e_idx = int(line[pos + 2:pos + 4])
                        code = line[pos + 4:pos + 7].lower()
                        ext_defs.append((s_idx, e_idx, code))
                        extras.setdefault(code, [])
                        pos += 7
            except ValueError:
                pass
        elif line.startswith("B"):
            m = _IGC_B_RE.match(line)
            if not m:
                continue
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
            t = hh * 3600.0 + mm * 60.0 + ss
            if math.isfinite(last_t) and t + day_offset < last_t - 43200.0:
                day_offset += 86400.0
            t += day_offset
            last_t = t
            lat = int(m.group(4)) + int(m.group(5)) / 60000.0
            if m.group(6) == "S":
                lat = -lat
            lon = int(m.group(7)) + int(m.group(8)) / 60000.0
            if m.group(9) == "W":
                lon = -lon
            b_alt = float(m.group(11))
            g_alt = float(m.group(12))
            times.append(t)
            lats.append(lat)
            lons.append(lon)
            baro_alt.append(b_alt)
            gnss_alt.append(g_alt)
            for s_idx, e_idx, code in ext_defs:
                chunk = line[s_idx:e_idx].strip() if e_idx <= len(line) else ""
                try:
                    extras[code].append(float(chunk))
                except ValueError:
                    extras[code].append(math.nan)

    if not times:
        raise ValueError(f"{path.name}: no B-records found in IGC file")

    if flight_date is not None:
        epoch_base = dt.datetime(
            flight_date.year, flight_date.month, flight_date.day, tzinfo=dt.timezone.utc
        ).timestamp()
        times = [epoch_base + t for t in times]

    # Prefer GNSS altitude when valid, else baro altitude
    alt_best = [g if g != 0.0 else b for b, g in zip(baro_alt, gnss_alt)]
    series: dict[str, tuple[str, str, list[float]]] = {
        "lat": ("Latitude", "°", lats),
        "lon": ("Longitude", "°", lons),
        "altitude": ("Altitude", "m", alt_best),
        "baro_altitude": ("Baro Altitude", "m", baro_alt),
        "gnss_altitude": ("GNSS Altitude", "m", gnss_alt),
    }
    ext_labels = {
        "tas": ("True Airspeed", "km/h"),
        "ias": ("Indicated Airspeed", "km/h"),
        "oat": ("Air Temp", "°C"),
        "vat": ("Vario", "m/s"),
        "enl": ("Engine Noise", ""),
        "fxa": ("Fix Accuracy", "m"),
    }
    for code, vals in extras.items():
        if len(vals) == len(times):
            lbl, u = ext_labels.get(code, (code.upper(), ""))
            series[code] = (lbl, u, vals)
    return _build_gps_telemetry(path, times, series, meta, "igc")
