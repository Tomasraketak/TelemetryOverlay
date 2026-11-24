"""Pure-Python Garmin / Wahoo / Bryton binary .FIT telemetry parser.

Decodes the ANT+ FIT binary protocol directly with zero external dependencies:
- Normal & developer-field definition messages (little- and big-endian)
- Compressed timestamp data headers
- Record messages (global mesg 20): GPS lat/lon (semicircles -> degrees),
  altitude / enhanced_altitude, speed / enhanced_speed, heart rate, cadence,
  power, distance, grade, temperature, vertical speed, GPS accuracy, battery
- Lap messages (global mesg 19) stored in ``telemetry.meta['laps']``
"""

from __future__ import annotations

import datetime as dt
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np

from ..data import Channel, LoadOptions, Telemetry

__all__ = ["load_fit"]

# FIT timestamps are seconds since 1989-12-31 00:00:00 UTC
_FIT_EPOCH_OFFSET = 631065600
_SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)

# Base type number -> (struct_code, byte_size, invalid_value)
_BASE_TYPES: dict[int, tuple[str, int, int | None]] = {
    0x00: ("B", 1, 0xFF),          # enum
    0x01: ("b", 1, 0x7F),          # sint8
    0x02: ("B", 1, 0xFF),          # uint8
    0x03: ("h", 2, 0x7FFF),        # sint16
    0x04: ("H", 2, 0xFFFF),        # uint16
    0x05: ("i", 4, 0x7FFFFFFF),    # sint32
    0x06: ("I", 4, 0xFFFFFFFF),    # uint32
    0x07: ("s", 1, None),          # string
    0x08: ("f", 4, None),          # float32
    0x09: ("d", 8, None),          # float64
    0x0A: ("B", 1, 0x00),          # uint8z
    0x0B: ("H", 2, 0x0000),        # uint16z
    0x0C: ("I", 4, 0x00000000),    # uint32z
    0x0D: ("B", 1, 0xFF),          # byte
    0x0E: ("q", 8, 0x7FFFFFFFFFFFFFFF),
    0x0F: ("Q", 8, 0xFFFFFFFFFFFFFFFF),
    0x10: ("Q", 8, 0x0000000000000000),
}

# Global message 20 ("record") field definitions:
# field_num -> (channel_key, label, unit, scale, offset)
_RECORD_FIELDS: dict[int, tuple[str, str, str, float, float]] = {
    0: ("lat", "Latitude", "°", 1.0 / _SEMICIRCLE_TO_DEG, 0.0),
    1: ("lon", "Longitude", "°", 1.0 / _SEMICIRCLE_TO_DEG, 0.0),
    2: ("altitude", "Altitude", "m", 5.0, 500.0),
    3: ("heart_rate", "Heart Rate", "bpm", 1.0, 0.0),
    4: ("cadence", "Cadence", "rpm", 1.0, 0.0),
    5: ("distance", "Distance", "m", 100.0, 0.0),
    6: ("speed", "Speed", "m/s", 1000.0, 0.0),
    7: ("power", "Power", "W", 1.0, 0.0),
    9: ("grade", "Grade", "%", 100.0, 0.0),
    13: ("temperature", "Temperature", "°C", 1.0, 0.0),
    31: ("gps_accuracy", "GPS Accuracy", "m", 1.0, 0.0),
    32: ("vspeed", "Vertical Speed", "m/s", 1000.0, 0.0),
    33: ("calories", "Calories", "kcal", 1.0, 0.0),
    73: ("enhanced_speed", "Speed", "m/s", 1000.0, 0.0),
    78: ("enhanced_altitude", "Altitude", "m", 5.0, 500.0),
    81: ("battery", "Battery", "%", 2.0, 0.0),
}


def _decode_field(buf: bytes, offset: int, size: int, base_type: int, endian: str) -> Any:
    bt = base_type & 0x1F
    spec = _BASE_TYPES.get(bt)
    if spec is None or offset + size > len(buf):
        return None
    code, elem_size, invalid = spec
    if code == "s":
        return buf[offset:offset + size].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    if size < elem_size:
        return None
    count = size // elem_size
    if count == 1:
        val = struct.unpack_from(endian + code, buf, offset)[0]
        if invalid is not None and val == invalid:
            return None
        if code in ("f", "d") and not math.isfinite(val):
            return None
        return val
    vals = struct.unpack_from(f"{endian}{count}{code}", buf, offset)
    if invalid is not None and all(v == invalid for v in vals):
        return None
    return vals


def load_fit(path: Path, opts: LoadOptions) -> Telemetry:
    """Load telemetry records from a Garmin / Wahoo .FIT binary file."""
    raw = path.read_bytes()
    if len(raw) < 14:
        raise ValueError(f"{path.name}: file too short to be a valid FIT file")

    header_size = raw[0]
    if header_size < 12 or header_size > len(raw):
        raise ValueError(f"{path.name}: invalid FIT header size {header_size}")
    if raw[8:12] != b".FIT":
        raise ValueError(f"{path.name}: missing '.FIT' signature")

    data_size = struct.unpack_from("<I", raw, 4)[0]
    pos = header_size
    end = min(len(raw), header_size + data_size)

    # Local message number -> (global_mesg_num, endian, fields, dev_fields)
    defs: dict[int, tuple[int, str, list[tuple[int, int, int]], list[tuple[int, int, int]]]] = {}
    last_timestamp = 0
    records: list[dict[str, float]] = []
    laps: list[dict[str, float]] = []
    meta: dict[str, Any] = {"format": "fit"}

    while pos < end:
        hdr = raw[pos]
        pos += 1

        if hdr & 0x80:
            # Compressed timestamp header
            local_num = (hdr >> 5) & 0x03
            time_offset = hdr & 0x1F
            if time_offset >= (last_timestamp & 0x1F):
                last_timestamp = (last_timestamp & ~0x1F) + time_offset
            else:
                last_timestamp = (last_timestamp & ~0x1F) + 0x20 + time_offset
            is_definition = False
            has_compressed_ts = True
        else:
            is_definition = bool(hdr & 0x40)
            has_dev = bool(hdr & 0x20)
            local_num = hdr & 0x0F
            has_compressed_ts = False

        if is_definition:
            if pos + 5 > end:
                break
            arch = raw[pos + 1]
            endian = ">" if arch else "<"
            global_num = struct.unpack_from(endian + "H", raw, pos + 2)[0]
            num_fields = raw[pos + 4]
            pos += 5
            fields: list[tuple[int, int, int]] = []
            for _ in range(num_fields):
                if pos + 3 > end:
                    break
                fields.append((raw[pos], raw[pos + 1], raw[pos + 2]))
                pos += 3
            dev_fields: list[tuple[int, int, int]] = []
            if has_dev and pos < end:
                num_dev = raw[pos]
                pos += 1
                for _ in range(num_dev):
                    if pos + 3 > end:
                        break
                    dev_fields.append((raw[pos], raw[pos + 1], raw[pos + 2]))
                    pos += 3
            defs[local_num] = (global_num, endian, fields, dev_fields)
            continue

        defn = defs.get(local_num)
        if defn is None:
            break
        global_num, endian, fields, dev_fields = defn
        total_size = sum(f[1] for f in fields) + sum(f[1] for f in dev_fields)
        if pos + total_size > end:
            break

        if global_num == 20:  # record
            rec: dict[str, float] = {}
            ts_val: int | None = last_timestamp if has_compressed_ts else None
            fpos = pos
            for fnum, fsize, ftype in fields:
                if fnum == 253 or fnum in _RECORD_FIELDS:
                    val = _decode_field(raw, fpos, fsize, ftype, endian)
                    if val is not None and isinstance(val, (int, float)):
                        if fnum == 253:
                            ts_val = int(val)
                            last_timestamp = ts_val
                        else:
                            key, _, _, scale, offset = _RECORD_FIELDS[fnum]
                            rec[key] = float(val) / scale - offset
                fpos += fsize
            if ts_val is not None:
                rec["_ts"] = float(ts_val + _FIT_EPOCH_OFFSET)
                # Prefer enhanced_speed / enhanced_altitude over legacy 16-bit
                if "enhanced_speed" in rec:
                    rec["speed"] = rec.pop("enhanced_speed")
                if "enhanced_altitude" in rec:
                    rec["altitude"] = rec.pop("enhanced_altitude")
                records.append(rec)
        elif global_num == 19:  # lap
            lap: dict[str, float] = {}
            fpos = pos
            for fnum, fsize, ftype in fields:
                if fnum in (2, 7, 8, 9, 253):
                    val = _decode_field(raw, fpos, fsize, ftype, endian)
                    if isinstance(val, (int, float)):
                        if fnum == 2:
                            lap["start_epoch"] = float(val + _FIT_EPOCH_OFFSET)
                        elif fnum == 253:
                            lap["end_epoch"] = float(val + _FIT_EPOCH_OFFSET)
                        elif fnum == 7:
                            lap["elapsed_s"] = float(val) / 1000.0
                        elif fnum == 9:
                            lap["distance_m"] = float(val) / 100.0
                fpos += fsize
            if lap:
                laps.append(lap)
        elif global_num == 0:  # file_id
            fpos = pos
            for fnum, fsize, ftype in fields:
                val = _decode_field(raw, fpos, fsize, ftype, endian)
                if fnum == 1 and isinstance(val, int):
                    meta["manufacturer_id"] = val
                elif fnum == 2 and isinstance(val, int):
                    meta["product_id"] = val
                fpos += fsize

        pos += total_size

    if not records:
        raise ValueError(f"{path.name}: no FIT record messages found")

    times = np.array([r["_ts"] for r in records], dtype=float)
    order = np.argsort(times, kind="stable")
    times = times[order]
    start_epoch = float(times[0])
    meta["start_epoch"] = start_epoch
    meta["start_time_utc"] = dt.datetime.fromtimestamp(
        start_epoch, tz=dt.timezone.utc
    ).isoformat()
    if laps:
        meta["laps"] = [
            {
                **lap,
                "start_t": lap["start_epoch"] - start_epoch if "start_epoch" in lap else 0.0,
                "end_t": lap["end_epoch"] - start_epoch if "end_epoch" in lap else 0.0,
            }
            for lap in laps
        ]

    rel_time = times - start_epoch
    tel = Telemetry(time=rel_time, meta=meta, source=str(path))

    label_map = {
        v[0]: (v[1], v[2])
        for v in _RECORD_FIELDS.values()
        if not v[0].startswith("enhanced_")
    }
    present_keys = sorted({k for r in records for k in r if k != "_ts"})
    for key in present_keys:
        vals = np.array([r.get(key, math.nan) for r in records], dtype=float)[order]
        if np.isfinite(vals).any():
            lbl, unit = label_map.get(key, (key.replace("_", " ").title(), ""))
            tel.add(Channel(key=key, label=lbl, unit=unit, values=vals, kind="numeric"))
    return tel
