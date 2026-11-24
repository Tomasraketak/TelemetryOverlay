"""Pluggable telemetry format registry and dispatcher."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..data import LoadOptions, Telemetry
from .dji import extract_embedded_srt, load_dji_srt
from .fit import load_fit
from .gpmf import load_gpmf
from .gps import load_gpx, load_igc, load_kml, load_nmea, load_tcx
from .tabular import load_csv, load_json, load_xlsx

__all__ = ["load_by_format", "SUPPORTED_FORMATS", "describe_formats"]

LoaderFunc = Callable[[Path, LoadOptions], Telemetry]

SUPPORTED_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "csv": ((".csv", ".tsv", ".txt", ".log", ".dat"), "CSV / TSV / RaceChrono / AiM / AirData / custom logger"),
    "xlsx": ((".xlsx", ".xlsm", ".xltx"), "Excel workbook (with automatic metadata sheet extraction)"),
    "json": ((".json", ".jsonl", ".ndjson", ".geojson"), "JSON / JSONL / GeoJSON telemetry"),
    "gpx": ((".gpx",), "GPX 1.0/1.1 GPS & fitness tracks (Strava, Garmin, Wahoo, HealthFit)"),
    "kml": ((".kml", ".kmz"), "Google Earth KML / zipped KMZ tracks (gx:Track & LineString)"),
    "tcx": ((".tcx",), "Garmin Training Center XML (GPS, HR, cadence, power)"),
    "fit": ((".fit",), "Garmin / Wahoo / Bryton binary FIT activity logs"),
    "nmea": ((".nmea", ".nma"), "NMEA 0183 GPS / marine / aviation sentences"),
    "igc": ((".igc",), "FAI IGC aviation & gliding flight recorder logs"),
    "dji_srt": ((".srt",), "DJI drone / Osmo telemetry subtitles"),
    "gopro_mp4": ((".mp4", ".mov", ".lrv", ".gpmf", ".bin"), "GoPro GPMF binary metadata & DJI MP4 embedded SRT"),
}


def _load_video_telemetry(path: Path, opts: LoadOptions) -> Telemetry:
    """Extract embedded telemetry from a video file (GoPro GPMF or DJI SRT)."""
    # 1. Try pure-Python GoPro GPMF extraction first (instant, no ffmpeg needed)
    try:
        return load_gpmf(path, opts)
    except ValueError:
        pass
    # 2. Try embedded DJI SRT subtitle track via ffmpeg
    srt_tel = extract_embedded_srt(path)
    if srt_tel is not None:
        return srt_tel
    # 3. Check for a sidecar .srt or .gpx next to the video with the same stem!
    for ext, loader in ((".srt", load_dji_srt), (".SRT", load_dji_srt),
                        (".gpx", load_gpx), (".GPX", load_gpx)):
        sidecar = path.with_suffix(ext)
        if sidecar.is_file():
            return loader(sidecar, opts)
    raise ValueError(
        f"{path.name}: no embedded GoPro GPMF or DJI SRT telemetry found "
        f"(and no matching sidecar .srt/.gpx file)"
    )


def _sniff_text_format(path: Path) -> str:
    """Peek at the first bytes of a .txt/.log file to catch NMEA or IGC."""
    try:
        head = path.read_bytes()[:512].decode("latin-1", errors="ignore")
    except OSError:
        return "csv"
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()][:10]
    if any(ln.startswith(("$GP", "$GN", "$GL", "$GA", "$II", "$WI")) for ln in lines):
        return "nmea"
    if lines and lines[0].startswith("A") and any(ln.startswith(("HFDTE", "B")) for ln in lines):
        return "igc"
    return "csv"


_LOADERS: dict[str, LoaderFunc] = {
    "csv": load_csv,
    "tsv": load_csv,
    "xlsx": load_xlsx,
    "json": load_json,
    "jsonl": load_json,
    "geojson": load_json,
    "gpx": load_gpx,
    "kml": load_kml,
    "kmz": load_kml,
    "tcx": load_tcx,
    "fit": load_fit,
    "nmea": load_nmea,
    "igc": load_igc,
    "dji_srt": load_dji_srt,
    "srt": load_dji_srt,
    "gpmf": load_gpmf,
    "gopro": load_gpmf,
    "gopro_mp4": _load_video_telemetry,
    "video": _load_video_telemetry,
}

_EXT_TO_FORMAT: dict[str, str] = {
    ext: fmt
    for fmt, (exts, _) in SUPPORTED_FORMATS.items()
    for ext in exts
}


def load_by_format(path: Path, opts: LoadOptions) -> Telemetry:
    """Dispatch ``path`` to the appropriate telemetry parser."""
    fmt = (opts.format or "").strip().lower()
    if not fmt:
        suffix = path.suffix.lower()
        if suffix in (".txt", ".log"):
            fmt = _sniff_text_format(path)
        else:
            fmt = _EXT_TO_FORMAT.get(suffix, "csv")
    loader = _LOADERS.get(fmt)
    if loader is None:
        raise ValueError(
            f"unknown input format {fmt!r}; supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    return loader(path, opts)


def describe_formats() -> str:
    """Human-readable table of supported telemetry input formats."""
    lines = ["Supported telemetry input formats:", ""]
    for name, (exts, desc) in SUPPORTED_FORMATS.items():
        ext_str = ", ".join(exts)
        lines.append(f"  {name:<12} {ext_str:<30} {desc}")
    return "\n".join(lines)
