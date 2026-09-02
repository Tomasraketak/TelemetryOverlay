"""Loading telemetry from CSV / TSV / XLSX files.

The loader is deliberately forgiving: field separators, decimal marks and
column names differ wildly between logger firmwares, so everything is
sniffed from the file itself unless the user overrides it.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

__all__ = ["Channel", "Telemetry", "load", "LoadOptions"]


# Column names that identify the time axis, in several languages.
_TIME_HINTS = (
    "time", "timestamp", "cas", "čas", "tijd", "zeit", "temps", "tempo", "t",
)
# Suffixes that reveal the unit inside a header such as "Thrust [N]".
_UNIT_RE = re.compile(r"[\[(]\s*([^\])]{1,12}?)\s*[\])]\s*$")

# Units that mean the column is in milliseconds rather than seconds.
_MS_UNITS = {"ms", "msec", "millis", "milliseconds"}
_US_UNITS = {"us", "µs", "usec", "microseconds"}

_TRUE_WORDS = {"1", "true", "yes", "y", "on", "ano", "high", "active", "ok"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "ne", "low", "inactive"}


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
    """One measured quantity over time."""

    key: str
    label: str
    unit: str
    values: np.ndarray
    kind: str = "numeric"  # "numeric" | "boolean" | "text"

    @property
    def is_boolean(self) -> bool:
        return self.kind == "boolean"

    @property
    def finite(self) -> np.ndarray:
        """The values with NaNs removed - handy for range calculations."""
        if self.kind == "text":
            return np.zeros(0)
        return self.values[np.isfinite(self.values)]

    def range(self) -> tuple[float, float]:
        finite = self.finite
        if finite.size == 0:
            return 0.0, 1.0
        lo, hi = float(finite.min()), float(finite.max())
        if lo == hi:
            return lo - 0.5, hi + 0.5
        return lo, hi

    def describe(self) -> dict[str, float]:
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
    def value_at(self, key: str, t: float, interpolate: bool = True) -> float:
        """Value of ``key`` at data time ``t``.

        Booleans and anything outside the logged window use nearest-sample
        lookup so a flag never reads as a physically impossible 0.4.
        """
        channel = self.channels.get(key)
        if channel is None or self.time.size == 0:
            return float("nan")
        if channel.kind == "text":
            return float("nan")
        if not interpolate or channel.is_boolean:
            idx = int(np.clip(np.searchsorted(self.time, t), 0, self.time.size - 1))
            if idx > 0 and abs(self.time[idx - 1] - t) <= abs(self.time[idx] - t):
                idx -= 1
            return float(channel.values[idx])
        if t <= self.time[0]:
            return float(channel.values[0])
        if t >= self.time[-1]:
            return float(channel.values[-1])
        return float(np.interp(t, self.time, channel.values))

    def index_at(self, t: float) -> int:
        """Index of the sample nearest to data time ``t``."""
        if self.time.size == 0:
            return 0
        idx = int(np.clip(np.searchsorted(self.time, t), 0, self.time.size - 1))
        if idx > 0 and abs(self.time[idx - 1] - t) <= abs(self.time[idx] - t):
            idx -= 1
        return idx

    def resolve(self, names: Any) -> str | None:
        """First channel that exists, from a name or a list of candidates.

        Lets one config work across loggers that spell the same measurement
        differently - ``[pyro_active, pyro_energised]`` picks whichever is
        present.
        """
        if names is None:
            return None
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)):
            raise ValueError(
                f"a channel must be a name or a list of names, got {names!r}")
        for name in names:
            if str(name) in self.channels:
                return str(name)
        return None

    def numeric_keys(self) -> list[str]:
        return [k for k, c in self.channels.items() if c.kind == "numeric"]

    def summary(self) -> str:
        lines = [
            f"source        : {self.source}",
            f"samples       : {self.time.size}",
            f"time window   : {self.t0:.3f} s .. {self.t1:.3f} s  ({self.duration:.3f} s)",
            f"sample rate   : {self.sample_rate:.1f} Hz",
            "",
            f"{'key':<24} {'label':<26} {'unit':<8} {'kind':<8} {'min':>12} {'max':>12}",
            "-" * 96,
        ]
        for channel in self.channels.values():
            stats = channel.describe()
            lo = f"{stats['min']:.4g}" if stats else "-"
            hi = f"{stats['max']:.4g}" if stats else "-"
            lines.append(
                f"{channel.key:<24} {channel.label[:26]:<26} {channel.unit:<8} "
                f"{channel.kind:<8} {lo:>12} {hi:>12}"
            )
        return "\n".join(lines)


@dataclass
class LoadOptions:
    """Overrides for the format sniffing."""

    delimiter: str | None = None
    decimal: str | None = None
    time_column: str | None = None
    time_unit: str | None = None       # "s" | "ms" | "us"
    sheet: str | int | None = None
    encoding: str | None = None
    skip_rows: int = 0


# ---------------------------------------------------------------------------
# CSV / text parsing
# ---------------------------------------------------------------------------

def _read_text(path: Path, encoding: str | None) -> str:
    raw = path.read_bytes()
    candidates = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1250", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        pass
    # Fall back to whichever candidate appears most consistently per line.
    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    best, best_score = ",", -1.0
    for cand in (";", ",", "\t", "|"):
        counts = [ln.count(cand) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        # Reward a high, uniform count.
        score = float(np.mean(counts)) - float(np.std(counts)) * 2.0
        if score > best_score:
            best, best_score = cand, score
    return best


def _sniff_decimal(rows: Sequence[Sequence[str]], delimiter: str) -> str:
    """Decide whether ',' is a decimal mark.

    Only meaningful when the delimiter is not itself a comma.
    """
    if delimiter == ",":
        return "."
    comma_numbers = 0
    dot_numbers = 0
    for row in rows[:200]:
        for cell in row:
            cell = cell.strip()
            if not cell:
                continue
            if re.fullmatch(r"[-+]?\d+,\d+([eE][-+]?\d+)?", cell):
                comma_numbers += 1
            elif re.fullmatch(r"[-+]?\d+\.\d+([eE][-+]?\d+)?", cell):
                dot_numbers += 1
    return "," if comma_numbers > dot_numbers else "."


def _to_float(cell: str, decimal: str) -> float:
    cell = cell.strip()
    if not cell:
        return math.nan
    if decimal == ",":
        # Strip thousands dots only when they are clearly grouping separators.
        if cell.count(",") == 1:
            cell = cell.replace(" ", "").replace(" ", "").replace(",", ".")
        else:
            cell = cell.replace(" ", "").replace(",", ".")
    else:
        cell = cell.replace(" ", "").replace(" ", "")
    try:
        return float(cell)
    except ValueError:
        return math.nan


def _parse_column(raw: list[str], decimal: str) -> tuple[np.ndarray, str]:
    """Turn a column of strings into floats, detecting booleans and text."""
    lowered = [c.strip().lower() for c in raw]
    non_empty = [c for c in lowered if c]
    if non_empty and all(c in _TRUE_WORDS or c in _FALSE_WORDS for c in non_empty):
        values = np.array(
            [1.0 if c in _TRUE_WORDS else (0.0 if c in _FALSE_WORDS else math.nan)
             for c in lowered],
            dtype=float,
        )
        # A column of only 0/1 is boolean; only treat wider sets as boolean too.
        return values, "boolean"

    values = np.array([_to_float(c, decimal) for c in raw], dtype=float)
    good = np.isfinite(values).sum()
    if good < max(1, len(values) * 0.5):
        return values, "text"
    unique = np.unique(values[np.isfinite(values)])
    if unique.size <= 2 and np.all(np.isin(unique, (0.0, 1.0))):
        return values, "boolean"
    return values, "numeric"


def _load_csv(path: Path, opts: LoadOptions) -> tuple[list[str], list[list[str]]]:
    text = _read_text(path, opts.encoding)
    if opts.skip_rows:
        lines = text.splitlines()
        text = "\n".join(lines[opts.skip_rows:])
    delimiter = opts.delimiter or _sniff_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"{path.name}: no data rows found")
    header = [cell.strip() for cell in rows[0]]
    body = rows[1:]
    # A header row must not parse as numbers.
    if all(_to_float(cell, ".") == _to_float(cell, ".") for cell in header if cell):
        header = [f"col{i + 1}" for i in range(len(header))]
        body = rows
    width = len(header)
    body = [row + [""] * (width - len(row)) if len(row) < width else row[:width]
            for row in body]
    return header, body


def _load_csv_channels(path: Path, opts: LoadOptions) -> tuple[list[str], list[str], list[np.ndarray], list[str]]:
    header, body = _load_csv(path, opts)
    decimal = opts.decimal or _sniff_decimal(body, opts.delimiter or _sniff_delimiter(
        _read_text(path, opts.encoding)[:8192]))
    columns: list[np.ndarray] = []
    kinds: list[str] = []
    for i in range(len(header)):
        raw = [row[i] for row in body]
        values, kind = _parse_column(raw, decimal)
        columns.append(values)
        kinds.append(kind)
    return header, kinds, columns, [decimal]


# ---------------------------------------------------------------------------
# XLSX parsing
# ---------------------------------------------------------------------------

def _pick_sheet(workbook, requested: str | int | None):
    if requested is not None:
        if isinstance(requested, int):
            return workbook[workbook.sheetnames[requested]]
        return workbook[requested]
    # Prefer an obvious data sheet, otherwise the one with most rows.
    for name in workbook.sheetnames:
        if name.strip().lower() in {"data", "log", "samples", "telemetry", "raw"}:
            return workbook[name]
    return max((workbook[n] for n in workbook.sheetnames), key=lambda ws: ws.max_row or 0)


def _sheet_is_keyvalue(rows: list[tuple]) -> bool:
    """A two-column sheet of label/value pairs is metadata, not samples."""
    if not rows:
        return False
    width = max(len(r) for r in rows)
    return width <= 2


def _load_xlsx(path: Path, opts: LoadOptions):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Reading .xlsx needs openpyxl - install it with: pip install openpyxl"
        ) from exc

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = _pick_sheet(workbook, opts.sheet)
    rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
    rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
    if not rows:
        raise ValueError(f"{path.name}: sheet '{sheet.title}' is empty")

    header = [("" if c is None else str(c).strip()) for c in rows[0]]
    body = rows[1:]
    width = len(header)

    columns: list[np.ndarray] = []
    kinds: list[str] = []
    for i in range(width):
        raw = []
        for row in body:
            cell = row[i] if i < len(row) else None
            raw.append("" if cell is None else str(cell))
        values, kind = _parse_column(raw, ".")
        columns.append(values)
        kinds.append(kind)

    # Any other sheet that looks like label/value pairs becomes metadata.
    meta: dict[str, Any] = {}
    for name in workbook.sheetnames:
        if name == sheet.title:
            continue
        other = workbook[name]
        other_rows = [tuple(r) for r in other.iter_rows(values_only=True)]
        other_rows = [r for r in other_rows if any(c is not None for c in r)]
        if _sheet_is_keyvalue(other_rows):
            for row in other_rows:
                if len(row) >= 2 and row[0] is not None and row[1] is not None:
                    meta[str(row[0]).strip()] = str(row[1]).strip()
    workbook.close()
    return header, kinds, columns, meta


# ---------------------------------------------------------------------------
# Time column selection
# ---------------------------------------------------------------------------

def _score_time_column(name: str, unit: str, values: np.ndarray, kind: str) -> float:
    """Higher is more likely to be the time axis.

    A short log can start 0, 1, ... and be classified as boolean; those values
    are still numbers, so such a column stays eligible and is ranked on its
    header instead.
    """
    if kind not in ("numeric", "boolean"):
        return -1.0
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return -1.0
    # Time must be monotonically non-decreasing.
    if np.any(np.diff(finite) < 0):
        return -1.0

    base, _ = _strip_unit(name)
    slug = _slug(base)
    score = 0.0
    if slug in {"time", "cas", "t", "timestamp"}:
        score += 10.0
    elif any(slug.startswith(h) or h in slug for h in _TIME_HINTS):
        score += 6.0
    # Prefer seconds over milliseconds when both are present.
    unit_l = unit.strip().lower()
    if unit_l in {"s", "sec", "seconds"}:
        score += 4.0
    elif unit_l in _MS_UNITS:
        score += 1.0
    elif unit_l in _US_UNITS:
        score += 0.5
    if "_s" == slug[-2:] or slug.endswith("_sec"):
        score += 2.0
    if slug.endswith("_ms"):
        score += 0.5
    score += 1.0  # monotonic bonus
    return score


def _time_scale(name: str, unit: str, values: np.ndarray, forced: str | None) -> float:
    """Factor converting the column into seconds."""
    if forced:
        forced = forced.strip().lower()
        if forced in _MS_UNITS:
            return 1e-3
        if forced in _US_UNITS:
            return 1e-6
        return 1.0
    unit_l = unit.strip().lower()
    if unit_l in _MS_UNITS:
        return 1e-3
    if unit_l in _US_UNITS:
        return 1e-6
    if unit_l in {"s", "sec", "secs", "seconds"}:
        return 1.0
    slug = _slug(_strip_unit(name)[0])
    if slug.endswith("_ms") or slug.endswith("ms"):
        return 1e-3
    if slug.endswith("_us"):
        return 1e-6
    # No unit given: guess from the median step. Loggers rarely sample slower
    # than 1 Hz, so a median step above ~1 means the column counts milliseconds.
    finite = values[np.isfinite(values)]
    if finite.size >= 2:
        step = float(np.median(np.diff(finite)))
        if step > 1.5:
            return 1e-3
    return 1.0


def load(path: str | Path, options: LoadOptions | None = None) -> Telemetry:
    """Load telemetry from a CSV/TSV/XLSX file."""
    path = Path(path)
    opts = options or LoadOptions()
    if not path.exists():
        raise FileNotFoundError(f"data file not found: {path}")

    meta: dict[str, Any] = {}
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xltx"}:
        header, kinds, columns, meta = _load_xlsx(path, opts)
    else:
        header, kinds, columns, _extra = _load_csv_channels(path, opts)

    if not columns:
        raise ValueError(f"{path.name}: no columns found")

    # Pick the time axis.
    time_idx = None
    if opts.time_column:
        wanted = _slug(opts.time_column)
        for i, name in enumerate(header):
            if _slug(_strip_unit(name)[0]) == wanted or name == opts.time_column:
                time_idx = i
                break
        if time_idx is None:
            raise ValueError(
                f"time column '{opts.time_column}' not found; "
                f"available: {', '.join(header)}"
            )
    else:
        scores = []
        for i, name in enumerate(header):
            base, unit = _strip_unit(name)
            scores.append(_score_time_column(name, unit, columns[i], kinds[i]))
        best = int(np.argmax(scores))
        if scores[best] <= 0:
            raise ValueError(
                f"{path.name}: could not identify a time column. "
                f"Pass --time-column explicitly. Columns: {', '.join(header)}"
            )
        time_idx = best

    base, unit = _strip_unit(header[time_idx])
    scale = _time_scale(header[time_idx], unit, columns[time_idx], opts.time_unit)
    kinds[time_idx] = "numeric"
    time = columns[time_idx] * scale

    # Drop rows with an unusable timestamp and enforce monotonic order.
    valid = np.isfinite(time)
    time = time[valid]
    order = np.argsort(time, kind="stable")

    telemetry = Telemetry(time=time[order], meta=meta, source=str(path))
    used_keys: set[str] = set()
    for i, name in enumerate(header):
        if i == time_idx:
            continue
        label, chan_unit = _strip_unit(name)
        if not label:
            continue
        key = _slug(label)
        # A file may hold a second time column (e.g. ms next to s) - keep it,
        # but do not let it collide with anything.
        suffix = 2
        base_key = key
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key)
        values = columns[i][valid][order]
        telemetry.add(Channel(key=key, label=label, unit=chan_unit,
                              values=values, kind=kinds[i]))
    return telemetry
