"""Tabular & structured telemetry loaders: CSV, TSV, XLSX, JSON, JSONL, GeoJSON."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..data import Channel, LoadOptions, Telemetry, _slug, _strip_unit

__all__ = [
    "load_csv",
    "load_xlsx",
    "load_json",
    "parse_timestamp_cell",
]

_TIME_HINTS = (
    "time", "timestamp", "cas", "čas", "tijd", "zeit", "temps", "tempo",
    "t", "elapsed", "utc", "datetime", "date", "seconds", "millis",
)
_MS_UNITS = {"ms", "msec", "millis", "milliseconds"}
_US_UNITS = {"us", "µs", "usec", "microseconds"}
_NS_UNITS = {"ns", "nsec", "nanoseconds"}
_TRUE_WORDS = {"1", "true", "yes", "y", "on", "ano", "high", "active", "ok"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "ne", "low", "inactive"}

# ISO-8601 / wall-clock timestamps like "2025-06-01T12:03:04.250Z" or "12:03:04.25"
_ISO_DT_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[T ]\d{1,2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_HMS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)$")


def parse_timestamp_cell(cell: Any) -> float:
    """Parse a datetime, ISO-8601 string or HH:MM:SS.fff into epoch/seconds."""
    if isinstance(cell, dt.datetime):
        if cell.tzinfo is None:
            return cell.replace(tzinfo=dt.timezone.utc).timestamp()
        return cell.timestamp()
    if isinstance(cell, dt.time):
        return (
            cell.hour * 3600.0
            + cell.minute * 60.0
            + cell.second
            + cell.microsecond * 1e-6
        )
    text = str(cell).strip()
    if not text:
        return math.nan
    m_hms = _HMS_RE.match(text)
    if m_hms:
        h, m, s = int(m_hms.group(1)), int(m_hms.group(2)), float(m_hms.group(3))
        return h * 3600.0 + m * 60.0 + s
    if _ISO_DT_RE.match(text):
        norm = text.replace("/", "-")
        if norm.endswith("Z"):
            norm = norm[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(norm)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return math.nan
    return math.nan


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
    lines = [
        ln for ln in sample.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ][:25]
    # Semicolon/tab/pipe with uniform non-zero count per line beats decimal commas
    # (e.g. a 2-column European CSV "0,5;12,3" has more commas than semicolons!).
    if len(lines) >= 2:
        for strong in ("\t", ";", "|"):
            counts = [ln.count(strong) for ln in lines]
            if min(counts) >= 1 and max(counts) == min(counts):
                return strong
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        pass
    best, best_score = ",", -1.0
    for cand in (";", ",", "\t", "|"):
        counts = [ln.count(cand) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        score = float(np.mean(counts)) - float(np.std(counts)) * 2.0
        if score > best_score:
            best, best_score = cand, score
    return best


def _sniff_decimal(rows: Sequence[Sequence[str]], delimiter: str) -> str:
    """Decide whether ',' is a decimal mark (only when delimiter != ',')."""
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
        if cell.count(",") == 1:
            cell = cell.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        else:
            cell = cell.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    else:
        cell = cell.replace(" ", "").replace("\u00a0", "")
    try:
        return float(cell)
    except ValueError:
        return math.nan


def _parse_column(
    raw: list[Any], decimal: str
) -> tuple[np.ndarray, str, np.ndarray | None, bool]:
    """Parse a raw column into (values, kind, text_values, is_epoch_datetime)."""
    n = len(raw)
    if n == 0:
        return np.zeros(0, dtype=float), "numeric", None, False

    # Fast path for native Python/openpyxl numeric & datetime columns
    first_non_none = next((c for c in raw if c is not None and c != ""), None)
    if isinstance(first_non_none, (dt.datetime, dt.time)):
        parsed = np.array([parse_timestamp_cell(c) if c is not None else math.nan for c in raw], dtype=float)
        if np.isfinite(parsed).sum() >= max(1, n * 0.5):
            return parsed, "numeric", None, isinstance(first_non_none, dt.datetime)

    if isinstance(first_non_none, bool):
        vals = np.array([1.0 if c is True else (0.0 if c is False else math.nan) for c in raw], dtype=float)
        return vals, "boolean", None, False

    if isinstance(first_non_none, (int, float)) and not isinstance(first_non_none, bool):
        vals = np.empty(n, dtype=float)
        ok = True
        for i, c in enumerate(raw):
            if c is None or c == "":
                vals[i] = math.nan
            elif isinstance(c, (int, float)) and not isinstance(c, bool):
                vals[i] = float(c)
            elif isinstance(c, bool):
                vals[i] = 1.0 if c else 0.0
            else:
                v = _to_float(str(c), decimal)
                vals[i] = v
                if not math.isfinite(v) and str(c).strip():
                    ok = False
        good = int(np.isfinite(vals).sum())
        if ok or good >= max(1, n * 0.5):
            finite = vals[np.isfinite(vals)]
            unique = np.unique(finite)
            if unique.size <= 2 and unique.size > 0 and np.all(np.isin(unique, (0.0, 1.0))):
                return vals, "boolean", None, False
            return vals, "numeric", None, False

    # String-based parsing
    str_raw = ["" if c is None else str(c).strip() for c in raw]
    lowered = [c.lower() for c in str_raw]
    non_empty = [c for c in lowered if c]
    if non_empty and all(c in _TRUE_WORDS or c in _FALSE_WORDS for c in non_empty):
        values = np.array(
            [1.0 if c in _TRUE_WORDS else (0.0 if c in _FALSE_WORDS else math.nan)
             for c in lowered],
            dtype=float,
        )
        return values, "boolean", None, False

    # Check numeric floats first
    values = np.array([_to_float(c, decimal) for c in str_raw], dtype=float)
    good = int(np.isfinite(values).sum())
    if good >= max(1, n * 0.5):
        unique = np.unique(values[np.isfinite(values)])
        if unique.size <= 2 and np.all(np.isin(unique, (0.0, 1.0))):
            return values, "boolean", None, False
        return values, "numeric", None, False

    # Check ISO-8601 / HH:MM:SS timestamps
    ts_sample = [c for c in str_raw[:30] if c]
    if ts_sample and any(_ISO_DT_RE.match(c) or _HMS_RE.match(c) for c in ts_sample):
        ts_vals = np.array([parse_timestamp_cell(c) for c in str_raw], dtype=float)
        if np.isfinite(ts_vals).sum() >= max(1, n * 0.5):
            is_epoch = any(_ISO_DT_RE.match(c) for c in ts_sample)
            return ts_vals, "numeric", None, is_epoch

    # Otherwise preserve as a real text/enum channel!
    text_arr = np.array(str_raw, dtype=object)
    return values, "text", text_arr, False


def _detect_preamble(lines: list[str]) -> tuple[int, dict[str, Any]]:
    """Detect RaceChrono / AiM / comment preamble lines before the real CSV header."""
    meta: dict[str, Any] = {}
    start = 0
    # Skip leading '#' comment lines (preserving '# Key: Value' as metadata)
    while start < len(lines):
        stripped = lines[start].strip()
        if not stripped:
            start += 1
            continue
        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            if ":" in body:
                k, _, v = body.partition(":")
                if k.strip() and v.strip():
                    meta[k.strip()] = v.strip()
            start += 1
            continue
        break

    # Detect multi-line metadata block (RaceChrono / AiM): several 1-2 col lines
    # followed by a wide header row (>= 3 columns)
    if start < len(lines) - 3:
        head_window = lines[start:start + 25]
        first_line = head_window[0]
        if first_line.lower().startswith(("this file is created using racechrono", "format,aim", "format,\"aim")):
            for idx, ln in enumerate(head_window):
                if idx == 0:
                    continue
                parts = [p.strip().strip('"') for p in ln.split(",")]
                if len(parts) >= 4 and any(
                    any(h in p.lower() for h in ("time", "timestamp", "lap", "speed", "latitude"))
                    for p in parts
                ):
                    start += idx
                    break
                elif len(parts) == 2 and parts[0] and parts[1]:
                    meta[parts[0]] = parts[1]
    return start, meta


def _score_time_column(name: str, unit: str, values: np.ndarray, kind: str) -> float:
    if kind not in ("numeric", "boolean"):
        return -1.0
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return -1.0
    if np.any(np.diff(finite) < 0):
        return -1.0

    base, _ = _strip_unit(name)
    slug = _slug(base)
    score = 0.0
    if slug in {"time", "cas", "t", "timestamp", "elapsed", "elapsed_time"}:
        score += 10.0
    elif any(slug.startswith(h) or h in slug for h in _TIME_HINTS):
        score += 6.0
    unit_l = unit.strip().lower()
    if unit_l in {"s", "sec", "secs", "seconds"}:
        score += 4.0
    elif unit_l in _MS_UNITS:
        score += 1.0
    elif unit_l in _US_UNITS or unit_l in _NS_UNITS:
        score += 0.5
    if slug.endswith("_s") or slug.endswith("_sec"):
        score += 2.0
    if slug.endswith("_ms"):
        score += 0.5
    score += 1.0
    return score


def _time_scale(
    name: str, unit: str, values: np.ndarray, forced: str | None, is_epoch: bool
) -> float:
    if is_epoch:
        return 1.0
    if forced:
        forced_l = forced.strip().lower()
        if forced_l in _MS_UNITS:
            return 1e-3
        if forced_l in _US_UNITS:
            return 1e-6
        if forced_l in _NS_UNITS:
            return 1e-9
        return 1.0
    unit_l = unit.strip().lower()
    if unit_l in _MS_UNITS:
        return 1e-3
    if unit_l in _US_UNITS:
        return 1e-6
    if unit_l in _NS_UNITS:
        return 1e-9
    if unit_l in {"s", "sec", "secs", "seconds"}:
        return 1.0
    slug = _slug(_strip_unit(name)[0])
    if slug.endswith("_ms") or slug.endswith("ms"):
        return 1e-3
    if slug.endswith("_us"):
        return 1e-6
    if slug.endswith("_ns"):
        return 1e-9
    finite = values[np.isfinite(values)]
    if finite.size >= 2:
        diffs = np.diff(finite)
        pos = diffs[diffs > 0]
        if pos.size:
            step = float(np.median(pos))
            if step > 1.5 and finite[-1] < 1e9:
                return 1e-3
    return 1.0


def _assemble_telemetry(
    path: Path,
    header: list[str],
    kinds: list[str],
    columns: list[np.ndarray],
    text_columns: list[np.ndarray | None],
    is_epoch_flags: list[bool],
    meta: dict[str, Any],
    opts: LoadOptions,
    fmt_name: str,
) -> Telemetry:
    if not columns:
        raise ValueError(f"{path.name}: no columns found")

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
            _, unit = _strip_unit(name)
            scores.append(_score_time_column(name, unit, columns[i], kinds[i]))
        best = int(np.argmax(scores))
        if scores[best] <= 0:
            raise ValueError(
                f"{path.name}: could not identify a time column. "
                f"Pass --time-column explicitly. Columns: {', '.join(header)}"
            )
        time_idx = best

    _, unit = _strip_unit(header[time_idx])
    is_epoch = is_epoch_flags[time_idx]
    scale = _time_scale(header[time_idx], unit, columns[time_idx], opts.time_unit, is_epoch)
    kinds[time_idx] = "numeric"
    time = columns[time_idx] * scale

    valid = np.isfinite(time)
    time = time[valid]
    order = np.argsort(time, kind="stable")
    time = time[order]

    # If the time axis is a Unix epoch (> 1e8, e.g. ISO datetimes), shift to
    # start at t = 0 and store the absolute start epoch in metadata!
    if time.size and (is_epoch or time[0] > 1e8):
        start_epoch = float(time[0])
        meta.setdefault("start_epoch", start_epoch)
        meta.setdefault(
            "start_time_utc",
            dt.datetime.fromtimestamp(start_epoch, tz=dt.timezone.utc).isoformat(),
        )
        time = time - start_epoch

    meta.setdefault("format", fmt_name)
    telemetry = Telemetry(time=time, meta=meta, source=str(path))
    used_keys: set[str] = set()
    for i, name in enumerate(header):
        if i == time_idx:
            continue
        label, chan_unit = _strip_unit(name)
        if not label:
            continue
        key = _slug(label)
        suffix = 2
        base_key = key
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key)
        values = columns[i][valid][order]
        txt = text_columns[i][valid][order] if text_columns[i] is not None else None
        telemetry.add(
            Channel(
                key=key,
                label=label,
                unit=chan_unit,
                values=values,
                kind=kinds[i],
                text_values=txt,
            )
        )
    return telemetry


def load_csv(path: Path, opts: LoadOptions) -> Telemetry:
    """Load CSV or TSV telemetry with full sniffing and preamble support."""
    text = _read_text(path, opts.encoding)
    lines = text.splitlines()
    if opts.skip_rows:
        lines = lines[opts.skip_rows:]
        meta: dict[str, Any] = {}
    else:
        preamble_end, meta = _detect_preamble(lines)
        lines = lines[preamble_end:]

    cleaned = "\n".join(lines)
    delimiter = opts.delimiter or _sniff_delimiter(cleaned[:8192])
    reader = csv.reader(io.StringIO(cleaned), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"{path.name}: no data rows found")

    decimal = opts.decimal or _sniff_decimal(rows[1:] or rows, delimiter)
    header = [cell.strip() for cell in rows[0]]
    body = rows[1:]

    # A header row must not consist entirely of numbers (using the detected decimal!)
    non_empty_hdr = [cell for cell in header if cell]
    if non_empty_hdr and all(math.isfinite(_to_float(cell, decimal)) for cell in non_empty_hdr):
        header = [f"col{i + 1}" for i in range(len(header))]
        body = rows

    # Handle RaceChrono-style secondary units row (e.g. "s,m/s,deg,...")
    if body and len(body) > 1:
        row0 = [c.strip() for c in body[0]]
        row1 = [c.strip() for c in body[1]]
        r0_num = sum(math.isfinite(_to_float(c, decimal)) for c in row0 if c)
        r1_num = sum(math.isfinite(_to_float(c, decimal)) for c in row1 if c)
        if r0_num == 0 and r1_num >= max(1, len(row1) // 2):
            # Merge row0 as units into header when header has no units yet
            merged_hdr = []
            for h, u in zip(header, row0 + [""] * max(0, len(header) - len(row0))):
                if u and not _strip_unit(h)[1] and len(u) <= 12:
                    merged_hdr.append(f"{h} [{u}]")
                else:
                    merged_hdr.append(h)
            header = merged_hdr
            body = body[1:]

    width = len(header)
    body = [
        row + [""] * (width - len(row)) if len(row) < width else row[:width]
        for row in body
    ]

    columns: list[np.ndarray] = []
    kinds: list[str] = []
    text_columns: list[np.ndarray | None] = []
    epoch_flags: list[bool] = []
    for i in range(width):
        raw = [row[i] for row in body]
        values, kind, txt, is_epoch = _parse_column(raw, decimal)
        columns.append(values)
        kinds.append(kind)
        text_columns.append(txt)
        epoch_flags.append(is_epoch)

    fmt_name = "tsv" if delimiter == "\t" else "csv"
    return _assemble_telemetry(
        path, header, kinds, columns, text_columns, epoch_flags, meta, opts, fmt_name
    )


def _pick_sheet(workbook, requested: str | int | None):
    if requested is not None:
        if isinstance(requested, int):
            return workbook[workbook.sheetnames[requested]]
        return workbook[requested]
    for name in workbook.sheetnames:
        if name.strip().lower() in {"data", "log", "samples", "telemetry", "raw"}:
            return workbook[name]
    return max((workbook[n] for n in workbook.sheetnames), key=lambda ws: ws.max_row or 0)


def _sheet_is_keyvalue(rows: list[tuple]) -> bool:
    if not rows:
        return False
    return max(len(r) for r in rows) <= 2


def load_xlsx(path: Path, opts: LoadOptions) -> Telemetry:
    """Load an Excel (.xlsx/.xlsm) workbook, keeping metadata sheets."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Reading .xlsx needs openpyxl - install it with: pip install openpyxl"
        ) from exc

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = _pick_sheet(workbook, opts.sheet)
    rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
    if opts.skip_rows:
        rows = rows[opts.skip_rows:]
    rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
    if not rows:
        workbook.close()
        raise ValueError(f"{path.name}: sheet '{sheet.title}' is empty")

    header = [("" if c is None else str(c).strip()) for c in rows[0]]
    body = rows[1:]
    width = len(header)

    columns: list[np.ndarray] = []
    kinds: list[str] = []
    text_columns: list[np.ndarray | None] = []
    epoch_flags: list[bool] = []
    for i in range(width):
        raw = [row[i] if i < len(row) else None for row in body]
        values, kind, txt, is_epoch = _parse_column(raw, ".")
        columns.append(values)
        kinds.append(kind)
        text_columns.append(txt)
        epoch_flags.append(is_epoch)

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

    return _assemble_telemetry(
        path, header, kinds, columns, text_columns, epoch_flags, meta, opts, "xlsx"
    )


def _load_geojson(data: dict[str, Any], path: Path, opts: LoadOptions) -> Telemetry:
    """Parse a GeoJSON FeatureCollection or Feature into Telemetry."""
    features = data.get("features") if data.get("type") == "FeatureCollection" else [data]
    rows: list[dict[str, Any]] = []
    for feat in features or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        if gtype == "Point" and len(coords) >= 2:
            rec = dict(props)
            rec["lon"] = coords[0]
            rec["lat"] = coords[1]
            if len(coords) >= 3:
                rec.setdefault("altitude", coords[2])
            rows.append(rec)
        elif gtype == "LineString" and coords:
            times = props.get("coordTimes") or props.get("times") or props.get("timestamps")
            for idx, pt in enumerate(coords):
                if len(pt) < 2:
                    continue
                rec: dict[str, Any] = {"lon": pt[0], "lat": pt[1]}
                if len(pt) >= 3:
                    rec["altitude"] = pt[2]
                if isinstance(times, list) and idx < len(times):
                    rec["time"] = times[idx]
                else:
                    rec["time"] = float(idx)
                for k, v in props.items():
                    if isinstance(v, list) and len(v) == len(coords) and k not in ("coordTimes", "times", "timestamps"):
                        rec[k] = v[idx]
                rows.append(rec)
    if not rows:
        raise ValueError(f"{path.name}: no coordinates found in GeoJSON")
    return _rows_to_telemetry(rows, path, opts, {"format": "geojson"}, "geojson")


def _rows_to_telemetry(
    rows: list[dict[str, Any]],
    path: Path,
    opts: LoadOptions,
    meta: dict[str, Any],
    fmt_name: str,
) -> Telemetry:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            for k in row:
                if k not in seen:
                    seen.add(k)
                    keys.append(str(k))
    columns: list[np.ndarray] = []
    kinds: list[str] = []
    text_columns: list[np.ndarray | None] = []
    epoch_flags: list[bool] = []
    for k in keys:
        raw = [row.get(k) if isinstance(row, dict) else None for row in rows]
        values, kind, txt, is_epoch = _parse_column(raw, ".")
        columns.append(values)
        kinds.append(kind)
        text_columns.append(txt)
        epoch_flags.append(is_epoch)
    return _assemble_telemetry(
        path, keys, kinds, columns, text_columns, epoch_flags, meta, opts, fmt_name
    )


def load_json(path: Path, opts: LoadOptions) -> Telemetry:
    """Load JSON, JSONL (newline-delimited JSON) or GeoJSON telemetry."""
    text = _read_text(path, opts.encoding).strip()
    if not text:
        raise ValueError(f"{path.name}: empty JSON file")

    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        return _rows_to_telemetry(rows, path, opts, {}, "jsonl")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try JSONL fallback
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        return _rows_to_telemetry(rows, path, opts, {}, "jsonl")

    if isinstance(data, dict) and data.get("type") in ("FeatureCollection", "Feature"):
        return _load_geojson(data, path, opts)

    if isinstance(data, list):
        return _rows_to_telemetry(data, path, opts, {}, "json")

    if isinstance(data, dict):
        meta = dict(data.get("meta") or data.get("metadata") or {})
        # Check wrapped list-of-records
        for key in ("records", "samples", "data", "telemetry", "points", "frames"):
            val = data.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return _rows_to_telemetry(val, path, opts, meta, "json")
        # Columnar dict: {"time": [...], "speed": [...], ...}
        list_keys = [k for k, v in data.items() if isinstance(v, list)]
        if list_keys:
            n = max(len(data[k]) for k in list_keys)
            columns: list[np.ndarray] = []
            kinds: list[str] = []
            text_columns: list[np.ndarray | None] = []
            epoch_flags: list[bool] = []
            for k in list_keys:
                raw = list(data[k])
                if len(raw) < n:
                    raw.extend([None] * (n - len(raw)))
                values, kind, txt, is_epoch = _parse_column(raw, ".")
                columns.append(values)
                kinds.append(kind)
                text_columns.append(txt)
                epoch_flags.append(is_epoch)
            return _assemble_telemetry(
                path, list_keys, kinds, columns, text_columns, epoch_flags, meta, opts, "json"
            )

    raise ValueError(f"{path.name}: unsupported JSON structure for telemetry")
