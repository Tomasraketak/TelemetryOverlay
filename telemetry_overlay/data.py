"""Loading, enriching, filtering and merging telemetry across formats.

Supports CSV, TSV, XLSX, JSON/JSONL/GeoJSON, GPX, KML/KMZ, TCX, Garmin/Wahoo
binary FIT, NMEA 0183, FAI IGC, DJI SRT subtitles, and GoPro GPMF directly
from MP4/MOV/LRV video files.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

__all__ = [
    "Channel",
    "Telemetry",
    "LoadOptions",
    "load",
    "load_many",
    "_strip_unit",
    "_slug",
]

# Suffixes that reveal the unit inside a header such as "Thrust [N]".
_UNIT_RE = re.compile(r"[\[(]\s*([^\])]{1,12}?)\s*[\])]\s*$")


def _strip_unit(name: str) -> tuple[str, str]:
    """Split ``"Thrust [N]"`` into ``("Thrust", "N")``."""
    match = _UNIT_RE.search(name)
    if not match:
        return name.strip(), ""
    return name[: match.start()].strip(), match.group(1).strip()


def _slug(name: str) -> str:
    """Normalise a header into a key usable from config files."""
    out = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_")
    return out or "column"


@dataclass
class Channel:
    """One measured quantity (numeric, boolean or text/enum) over time."""

    key: str
    label: str
    unit: str
    values: np.ndarray
    kind: str = "numeric"  # "numeric" | "boolean" | "text"
    text_values: np.ndarray | None = None
    _cum_max: np.ndarray | None = field(default=None, repr=False, compare=False)
    _cum_min: np.ndarray | None = field(default=None, repr=False, compare=False)
    _range_cache: tuple[float, float] | None = field(default=None, repr=False, compare=False)

    def invalidate_cache(self) -> None:
        self._cum_max = None
        self._cum_min = None
        self._range_cache = None

    @property
    def is_boolean(self) -> bool:
        return self.kind == "boolean"

    @property
    def is_text(self) -> bool:
        return self.kind == "text"

    @property
    def finite(self) -> np.ndarray:
        """The values with NaNs removed - handy for range calculations."""
        if self.kind == "text":
            return np.zeros(0)
        return self.values[np.isfinite(self.values)]

    def range(self) -> tuple[float, float]:
        if self._range_cache is not None:
            return self._range_cache
        finite = self.finite
        if finite.size == 0:
            res = (0.0, 1.0)
        else:
            lo, hi = float(finite.min()), float(finite.max())
            res = (lo - 0.5, hi + 0.5) if lo == hi else (lo, hi)
        self._range_cache = res
        return res

    def running_max_at(self, idx: int) -> float:
        """O(1) cumulative maximum up to sample index ``idx``."""
        if self.kind == "text" or self.values.size == 0:
            return math.nan
        if self._cum_max is None:
            safe = np.where(np.isfinite(self.values), self.values, -np.inf)
            self._cum_max = np.maximum.accumulate(safe)
        i = int(np.clip(idx, 0, self._cum_max.size - 1))
        val = float(self._cum_max[i])
        return val if math.isfinite(val) else math.nan

    def running_min_at(self, idx: int) -> float:
        """O(1) cumulative minimum up to sample index ``idx``."""
        if self.kind == "text" or self.values.size == 0:
            return math.nan
        if self._cum_min is None:
            safe = np.where(np.isfinite(self.values), self.values, np.inf)
            self._cum_min = np.minimum.accumulate(safe)
        i = int(np.clip(idx, 0, self._cum_min.size - 1))
        val = float(self._cum_min[i])
        return val if math.isfinite(val) else math.nan

    def describe(self) -> dict[str, Any]:
        if self.kind == "text":
            if self.text_values is None or self.text_values.size == 0:
                return {}
            uniq = sorted({str(v) for v in self.text_values if str(v).strip()})
            return {"unique": uniq[:6], "count": int(self.text_values.size)}
        finite = self.finite
        if finite.size == 0:
            return {}
        return {
            "min": float(finite.min()),
            "max": float(finite.max()),
            "mean": float(finite.mean()),
            "count": int(finite.size),
        }


@dataclass
class Telemetry:
    """A time base plus the channels sampled against it."""

    time: np.ndarray
    channels: dict[str, Channel] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        self.time = np.asarray(self.time, dtype=float)

    # -- basic properties -------------------------------------------------
    @property
    def t0(self) -> float:
        return float(self.time[0]) if self.time.size else 0.0

    @property
    def t1(self) -> float:
        return float(self.time[-1]) if self.time.size else 0.0

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    @property
    def sample_rate(self) -> float:
        """Median sample rate in Hz (robust against dropped samples)."""
        if self.time.size < 2:
            return 0.0
        deltas = np.diff(self.time)
        deltas = deltas[deltas > 0]
        if deltas.size == 0:
            return 0.0
        return float(1.0 / np.median(deltas))

    @property
    def has_gps(self) -> bool:
        return (
            self.resolve(["lat", "latitude", "gps_lat"]) is not None
            and self.resolve(["lon", "lng", "longitude", "gps_lon"]) is not None
        )

    def __contains__(self, key: str) -> bool:
        return key in self.channels

    def __getitem__(self, key: str) -> Channel:
        return self.channels[key]

    def get(self, key: str) -> Channel | None:
        return self.channels.get(key)

    def add(self, channel: Channel) -> Channel:
        self.channels[channel.key] = channel
        return channel

    # -- sampling ---------------------------------------------------------
    def index_at(self, t: float) -> int:
        """Index of the sample nearest to data time ``t``."""
        if self.time.size == 0:
            return 0
        idx = int(np.clip(np.searchsorted(self.time, t), 0, self.time.size - 1))
        if idx > 0 and abs(self.time[idx - 1] - t) <= abs(self.time[idx] - t):
            idx -= 1
        return idx

    def value_at(self, key: str, t: float, interpolate: bool = True) -> float:
        """Value of ``key`` at data time ``t``.

        Booleans and anything outside the logged window use nearest-sample
        lookup so a flag never reads as a physically impossible 0.4.
        """
        channel = self.channels.get(key)
        if channel is None or self.time.size == 0 or channel.kind == "text":
            return float("nan")
        if not interpolate or channel.is_boolean:
            return float(channel.values[self.index_at(t)])
        if t <= self.time[0]:
            return float(channel.values[0])
        if t >= self.time[-1]:
            return float(channel.values[-1])
        return float(np.interp(t, self.time, channel.values))

    def text_at(self, key: str, t: float) -> str:
        """Text value of a channel at data time ``t`` (for text/enum channels)."""
        channel = self.channels.get(key)
        if channel is None or self.time.size == 0:
            return ""
        idx = self.index_at(t)
        if channel.text_values is not None and idx < channel.text_values.size:
            return str(channel.text_values[idx])
        val = channel.values[idx]
        return "" if not math.isfinite(val) else f"{val:g}"

    def resolve(self, names: Any) -> str | None:
        """First channel that exists, from a name or a list of candidates."""
        if names is None:
            return None
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)):
            raise ValueError(
                f"a channel must be a name or a list of names, got {names!r}"
            )
        for name in names:
            if str(name) in self.channels:
                return str(name)
        return None

    def numeric_keys(self) -> list[str]:
        return [k for k, c in self.channels.items() if c.kind == "numeric"]

    def text_keys(self) -> list[str]:
        return [k for k, c in self.channels.items() if c.kind == "text"]

    def merge(
        self,
        other: "Telemetry",
        offset: float | None = None,
        prefix: str = "",
        overwrite: bool = False,
    ) -> "Telemetry":
        from .formats.enrichment import merge_telemetry
        return merge_telemetry(self, other, offset=offset, prefix=prefix, overwrite=overwrite)

    def summary(self) -> str:
        fmt = self.meta.get("format", "unknown")
        lines = [
            f"source        : {self.source}",
            f"format        : {fmt}",
            f"samples       : {self.time.size}",
            f"time window   : {self.t0:.3f} s .. {self.t1:.3f} s  ({self.duration:.3f} s)",
            f"sample rate   : {self.sample_rate:.1f} Hz",
        ]
        if "start_time_utc" in self.meta:
            lines.append(f"start UTC     : {self.meta['start_time_utc']}")
        if self.has_gps:
            lat_k = self.resolve(["lat", "latitude", "gps_lat"])
            lon_k = self.resolve(["lon", "lng", "longitude", "gps_lon"])
            assert lat_k and lon_k
            lat_r = self[lat_k].range()
            lon_r = self[lon_k].range()
            dist_str = ""
            if "distance" in self and self["distance"].finite.size:
                d_m = float(self["distance"].finite.max())
                dist_str = f"  ({d_m / 1000:.2f} km)" if d_m >= 1000 else f"  ({d_m:.0f} m)"
            lines.append(
                f"GPS bounds    : lat {lat_r[0]:.5f}..{lat_r[1]:.5f}, "
                f"lon {lon_r[0]:.5f}..{lon_r[1]:.5f}{dist_str}"
            )
        if "laps" in self.meta and isinstance(self.meta["laps"], list):
            lines.append(f"laps detected : {len(self.meta['laps'])}")
        lines.extend([
            "",
            f"{'key':<24} {'label':<26} {'unit':<8} {'kind':<8} {'min':>12} {'max':>12}",
            "-" * 96,
        ])
        for channel in self.channels.values():
            stats = channel.describe()
            if channel.kind == "text":
                uniq = stats.get("unique", [])
                sample_str = ", ".join(uniq[:3])
                if len(uniq) > 3:
                    sample_str += ", ..."
                lines.append(
                    f"{channel.key:<24} {channel.label[:26]:<26} {channel.unit:<8} "
                    f"{channel.kind:<8} {sample_str:>25}"
                )
            else:
                lo = f"{stats['min']:.4g}" if stats else "-"
                hi = f"{stats['max']:.4g}" if stats else "-"
                lines.append(
                    f"{channel.key:<24} {channel.label[:26]:<26} {channel.unit:<8} "
                    f"{channel.kind:<8} {lo:>12} {hi:>12}"
                )
        return "\n".join(lines)


@dataclass
class LoadOptions:
    """Overrides for format sniffing, enrichment, filtering and multi-source sync."""

    delimiter: str | None = None
    decimal: str | None = None
    time_column: str | None = None
    time_unit: str | None = None       # "s" | "ms" | "us" | "ns"
    sheet: str | int | None = None
    encoding: str | None = None
    skip_rows: int = 0
    format: str | None = None          # force format: csv, gpx, fit, gpmf, ...
    enrich: bool = True                # auto-compute GPS/IMU derived metrics
    detect_laps: bool = False          # auto-detect GPS circuit laps
    time_offset: float = 0.0           # shift timeline by N seconds
    filters: list[dict[str, Any]] = field(default_factory=list)
    merge: list[dict[str, Any]] = field(default_factory=list)


def load(path: str | Path, options: LoadOptions | None = None) -> Telemetry:
    """Load telemetry from any supported format (CSV, XLSX, GPX, FIT, GoPro MP4, DJI SRT, ...)."""
    from .formats import load_by_format
    from .formats.enrichment import apply_filters, detect_laps, enrich_telemetry, merge_telemetry

    path = Path(path)
    opts = options or LoadOptions()
    if not path.exists():
        raise FileNotFoundError(f"data file not found: {path}")

    telemetry = load_by_format(path, opts)

    if opts.time_offset:
        telemetry.time = telemetry.time + float(opts.time_offset)

    # Merge any secondary telemetry files configured in LoadOptions.merge
    for entry in opts.merge or []:
        if isinstance(entry, (str, Path)):
            sec_path, offset, prefix = Path(entry), None, ""
        elif isinstance(entry, dict):
            sec_path = Path(entry["path"])
            offset = entry.get("offset")
            prefix = str(entry.get("prefix", ""))
        else:
            continue
        sec = load(sec_path, LoadOptions(enrich=opts.enrich))
        merge_telemetry(telemetry, sec, offset=offset, prefix=prefix)

    if opts.filters:
        apply_filters(telemetry, opts.filters)

    if opts.enrich:
        enrich_telemetry(telemetry)

    if opts.detect_laps:
        detect_laps(telemetry)

    return telemetry


def load_many(
    paths: Sequence[str | Path],
    options: LoadOptions | None = None,
    *,
    mode: str = "concat",
    sync_offsets: Sequence[float | None] | None = None,
) -> Telemetry:
    """Load multiple files either by concatenating consecutive parts or merging sensors."""
    from .formats.enrichment import concat_telemetry, merge_telemetry

    if not paths:
        raise ValueError("at least one data path is required")
    if len(paths) == 1:
        return load(paths[0], options)

    opts = options or LoadOptions()
    base_opts = LoadOptions(
        delimiter=opts.delimiter, decimal=opts.decimal,
        time_column=opts.time_column, time_unit=opts.time_unit,
        sheet=opts.sheet, encoding=opts.encoding, skip_rows=opts.skip_rows,
        format=opts.format, enrich=opts.enrich, detect_laps=False,
        time_offset=opts.time_offset, filters=opts.filters,
    )
    loaded = [load(p, base_opts) for p in paths]
    if mode == "merge":
        base = loaded[0]
        for idx, other in enumerate(loaded[1:], start=1):
            off = sync_offsets[idx] if (sync_offsets and idx < len(sync_offsets)) else None
            merge_telemetry(base, other, offset=off)
        out = base
    else:
        out = concat_telemetry(loaded)

    if opts.enrich:
        from .formats.enrichment import enrich_telemetry
        enrich_telemetry(out)
    if opts.detect_laps:
        from .formats.enrichment import detect_laps
        detect_laps(out)
    return out
