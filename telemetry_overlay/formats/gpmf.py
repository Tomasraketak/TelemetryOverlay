"""Pure-Python GoPro GPMF binary telemetry parser (directly from .MP4 / .LRV / .BIN).

Reads the ISO-BMFF (MP4) box tree to locate the ``gpmd`` metadata track
without needing ffmpeg, extracts every timed GPMF payload chunk, and decodes
nested KLV streams:
- ``GPS5`` (HERO 5–11): lat, lon, altitude, speed_2d, speed_3d
- ``GPS9`` (HERO 11–13 / MAX2): lat, lon, altitude, speed_2d, speed_3d, days, secs, dop, fix
- ``ACCL``: 3-axis accelerometer (m/s^2)
- ``GYRO``: 3-axis gyroscope (rad/s -> deg/s)
- ``CORI``: camera orientation quaternion (w, x, y, z -> pitch, roll, yaw)
- ``GRAV``: gravity vector
- ``TMPC``: sensor temperature (°C)
- ``ISOE`` / ``SHUT``: camera ISO & shutter speed
"""

from __future__ import annotations

import datetime as dt
import math
import struct
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from ..data import Channel, LoadOptions, Telemetry

__all__ = ["load_gpmf", "extract_gpmf_chunks_from_mp4", "parse_gpmf_payload"]

# GPMF scalar type byte -> (struct_fmt_char, byte_size)
_GPMF_TYPES: dict[int, tuple[str, int]] = {
    ord("b"): ("b", 1),
    ord("B"): ("B", 1),
    ord("s"): ("h", 2),
    ord("S"): ("H", 2),
    ord("l"): ("i", 4),
    ord("L"): ("I", 4),
    ord("j"): ("q", 8),
    ord("J"): ("Q", 8),
    ord("f"): ("f", 4),
    ord("d"): ("d", 8),
    ord("c"): ("c", 1),
    ord("U"): ("c", 1),
}

_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}


# ---------------------------------------------------------------------------
# Minimal ISO-BMFF (MP4) box reader for the 'gpmd' track
# ---------------------------------------------------------------------------

def _iter_boxes(fh: BinaryIO, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            break
        size, box_type = struct.unpack(">I4s", header)
        hdr_len = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
            hdr_len = 16
        elif size == 0:
            size = end - pos
        if size < hdr_len or pos + size > end:
            break
        yield box_type, pos + hdr_len, pos + size
        pos += size


def _parse_trak_gpmd(fh: BinaryIO, start: int, end: int) -> list[tuple[float, float, bytes]] | None:
    """If this MP4 trak is a GoPro 'gpmd' track, return [(t_start, duration, payload)]."""
    timescale = 1000
    is_gpmd = False
    stts: list[tuple[int, int]] = []
    stsz: list[int] = []
    stsc: list[tuple[int, int, int]] = []
    chunk_offsets: list[int] = []

    def walk(s: int, e: int) -> None:
        nonlocal timescale, is_gpmd
        for btype, bs, be in _iter_boxes(fh, s, e):
            if btype in _CONTAINER_BOXES:
                walk(bs, be)
            elif btype == b"mdhd":
                fh.seek(bs)
                data = fh.read(min(32, be - bs))
                if len(data) >= 16:
                    ver = data[0]
                    if ver == 1 and len(data) >= 24:
                        timescale = struct.unpack_from(">I", data, 20)[0] or 1000
                    elif ver == 0:
                        timescale = struct.unpack_from(">I", data, 12)[0] or 1000
            elif btype == b"stsd":
                fh.seek(bs)
                data = fh.read(min(128, be - bs))
                if b"gpmd" in data:
                    is_gpmd = True
            elif btype == b"stts":
                fh.seek(bs)
                data = fh.read(be - bs)
                if len(data) >= 8:
                    count = struct.unpack_from(">I", data, 4)[0]
                    for i in range(count):
                        off = 8 + i * 8
                        if off + 8 <= len(data):
                            stts.append(struct.unpack_from(">II", data, off))
            elif btype == b"stsz":
                fh.seek(bs)
                data = fh.read(be - bs)
                if len(data) >= 12:
                    uniform, count = struct.unpack_from(">II", data, 4)
                    if uniform != 0:
                        stsz.extend([uniform] * count)
                    else:
                        for i in range(count):
                            off = 12 + i * 4
                            if off + 4 <= len(data):
                                stsz.append(struct.unpack_from(">I", data, off)[0])
            elif btype == b"stsc":
                fh.seek(bs)
                data = fh.read(be - bs)
                if len(data) >= 8:
                    count = struct.unpack_from(">I", data, 4)[0]
                    for i in range(count):
                        off = 8 + i * 12
                        if off + 12 <= len(data):
                            stsc.append(struct.unpack_from(">III", data, off))
            elif btype == b"stco":
                fh.seek(bs)
                data = fh.read(be - bs)
                if len(data) >= 8:
                    count = struct.unpack_from(">I", data, 4)[0]
                    for i in range(count):
                        off = 8 + i * 4
                        if off + 4 <= len(data):
                            chunk_offsets.append(struct.unpack_from(">I", data, off)[0])
            elif btype == b"co64":
                fh.seek(bs)
                data = fh.read(be - bs)
                if len(data) >= 8:
                    count = struct.unpack_from(">I", data, 4)[0]
                    for i in range(count):
                        off = 8 + i * 8
                        if off + 8 <= len(data):
                            chunk_offsets.append(struct.unpack_from(">Q", data, off)[0])

    walk(start, end)
    if not is_gpmd or not stsz or not chunk_offsets:
        return None

    # Expand per-sample durations from stts
    durations: list[float] = []
    for cnt, delta in stts:
        dur = delta / float(timescale)
        durations.extend([dur] * cnt)
    while len(durations) < len(stsz):
        durations.append(durations[-1] if durations else 1.0)

    # Map each sample to its byte offset using stsc + chunk_offsets
    sample_offsets: list[int] = []
    num_chunks = len(chunk_offsets)
    sample_idx = 0
    for c_idx in range(num_chunks):
        chunk_num_1 = c_idx + 1
        spc = 1
        for first_chunk, samples_per_chunk, _ in stsc:
            if chunk_num_1 >= first_chunk:
                spc = samples_per_chunk
            else:
                break
        off = chunk_offsets[c_idx]
        for _ in range(spc):
            if sample_idx >= len(stsz):
                break
            sample_offsets.append(off)
            off += stsz[sample_idx]
            sample_idx += 1

    chunks: list[tuple[float, float, bytes]] = []
    t_cur = 0.0
    for off, sz, dur in zip(sample_offsets, stsz, durations):
        fh.seek(off)
        chunks.append((t_cur, dur, fh.read(sz)))
        t_cur += dur
    return chunks


def extract_gpmf_chunks_from_mp4(path: Path) -> list[tuple[float, float, bytes]]:
    """Locate and read all timed GPMF payload chunks from a GoPro MP4/MOV/LRV."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        for btype, bs, be in _iter_boxes(fh, 0, size):
            if btype == b"moov":
                for sub_type, ss, se in _iter_boxes(fh, bs, be):
                    if sub_type == b"trak":
                        found = _parse_trak_gpmd(fh, ss, se)
                        if found:
                            return found
    return []


# ---------------------------------------------------------------------------
# GPMF KLV binary decoder
# ---------------------------------------------------------------------------

def _decode_klv_values(type_byte: int, struct_size: int, repeat: int, payload: bytes) -> Any:
    if type_byte == ord("c"):
        return payload[:struct_size * repeat].decode("latin-1", errors="replace").rstrip("\x00")
    if type_byte == ord("U"):
        return payload[:struct_size * repeat].decode("ascii", errors="replace").rstrip("\x00")
    if type_byte == ord("?"):
        # Complex structure defined by TYPE — skip unless handled separately
        return payload
    spec = _GPMF_TYPES.get(type_byte)
    if spec is None:
        return None
    fmt_char, elem_sz = spec
    cols = struct_size // elem_sz
    total = cols * repeat
    need = total * elem_sz
    if cols <= 0 or need > len(payload):
        return None
    vals = struct.unpack_from(f">{total}{fmt_char}", payload, 0)
    arr = np.asarray(vals, dtype=float)
    if cols > 1:
        return arr.reshape((repeat, cols))
    return arr


def _decode_complex_gps9(payload: bytes, repeat: int, type_def: str) -> np.ndarray | None:
    """Decode GPS9 heterogeneous struct using its GPMF TYPE descriptor."""
    fmt_parts: list[str] = []
    total_sz = 0
    for ch in type_def:
        spec = _GPMF_TYPES.get(ord(ch))
        if spec is None:
            return None
        fmt_parts.append(spec[0])
        total_sz += spec[1]
    if total_sz <= 0 or total_sz * repeat > len(payload):
        return None
    fmt = ">" + "".join(fmt_parts)
    rows = []
    for i in range(repeat):
        rows.append(struct.unpack_from(fmt, payload, i * total_sz))
    return np.asarray(rows, dtype=float)


def parse_gpmf_payload(payload: bytes) -> list[dict[str, Any]]:
    """Parse one GPMF payload chunk into a list of STRM dicts."""
    streams: list[dict[str, Any]] = []

    def walk(buf: bytes, start: int, end: int, cur_strm: dict[str, Any] | None) -> None:
        pos = start
        while pos + 8 <= end:
            fourcc = buf[pos:pos + 4].decode("latin-1")
            type_byte = buf[pos + 4]
            struct_size = buf[pos + 5]
            repeat = struct.unpack_from(">H", buf, pos + 6)[0]
            data_len = struct_size * repeat
            padded = (data_len + 3) & ~3
            dstart = pos + 8
            dend = dstart + data_len
            if dend > end:
                break

            if type_byte == 0:
                if fourcc == "STRM":
                    strm: dict[str, Any] = {}
                    walk(buf, dstart, dend, strm)
                    streams.append(strm)
                else:
                    walk(buf, dstart, dend, cur_strm)
            elif cur_strm is not None:
                chunk = buf[dstart:dend]
                if fourcc == "TYPE":
                    cur_strm["TYPE"] = chunk.decode("ascii", errors="replace").rstrip("\x00")
                elif fourcc == "SCAL":
                    val = _decode_klv_values(type_byte, struct_size, repeat, chunk)
                    if val is not None:
                        cur_strm["SCAL"] = val
                elif fourcc == "SIUN":
                    cur_strm["SIUN"] = chunk.decode("latin-1", errors="replace").rstrip("\x00")
                elif fourcc in ("GPSF", "GPSP", "GPSU", "STNM", "ORIN"):
                    cur_strm[fourcc] = _decode_klv_values(type_byte, struct_size, repeat, chunk)
                elif fourcc in ("GPS5", "GPS9", "ACCL", "GYRO", "CORI", "GRAV",
                                "TMPC", "ISOE", "SHUT", "WBAL", "MAGN"):
                    if type_byte == ord("?") and "TYPE" in cur_strm:
                        arr = _decode_complex_gps9(chunk, repeat, cur_strm["TYPE"])
                    else:
                        arr = _decode_klv_values(type_byte, struct_size, repeat, chunk)
                    if isinstance(arr, np.ndarray) and arr.size:
                        cur_strm["key"] = fourcc
                        cur_strm["data"] = arr
            pos = dstart + padded

    walk(payload, 0, len(payload), None)
    return streams


def _apply_scale(data: np.ndarray, scal: Any) -> np.ndarray:
    if scal is None:
        return data
    s = np.asarray(scal, dtype=float).ravel()
    s = np.where(s == 0, 1.0, s)
    if data.ndim == 2:
        if s.size == data.shape[1]:
            return data / s[None, :]
        if s.size == 1:
            return data / s[0]
    elif s.size >= 1:
        return data / s[0]
    return data


def _quat_to_euler_deg(q: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert (N, 4) quaternion [w, x, y, z] into (pitch, roll, yaw) in degrees."""
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    sinr = 2.0 * (w * x + y * z)
    cosr = 1.0 - 2.0 * (x * x + y * y)
    roll = np.degrees(np.arctan2(sinr, cosr))
    sinp = np.clip(2.0 * (w * y - z * x), -1.0, 1.0)
    pitch = np.degrees(np.arcsin(sinp))
    siny = 2.0 * (w * z + x * y)
    cosy = 1.0 - 2.0 * (y * y + z * z)
    yaw = (np.degrees(np.arctan2(siny, cosy)) + 360.0) % 360.0
    return pitch, roll, yaw


def load_gpmf(path: Path, opts: LoadOptions) -> Telemetry:
    """Load GoPro GPMF telemetry directly from an .MP4/.LRV/.MOV or raw .GPMF/.BIN."""
    suffix = path.suffix.lower()
    if suffix in {".gpmf", ".bin"}:
        chunks = [(0.0, 1.0, path.read_bytes())]
    else:
        chunks = extract_gpmf_chunks_from_mp4(path)
        if not chunks:
            raise ValueError(
                f"{path.name}: no GoPro GPMF ('gpmd') telemetry track found in video"
            )

    # Collect per-stream timed samples: key -> (times_list, array_list)
    streams_t: dict[str, list[np.ndarray]] = {}
    streams_v: dict[str, list[np.ndarray]] = {}
    meta: dict[str, Any] = {"format": "gopro_gpmf"}
    gpsu_epoch: float | None = None

    for t_start, dur, payload in chunks:
        parsed = parse_gpmf_payload(payload)
        for strm in parsed:
            key = strm.get("key")
            arr = strm.get("data")
            if not key or not isinstance(arr, np.ndarray) or arr.size == 0:
                continue

            if gpsu_epoch is None and isinstance(strm.get("GPSU"), str):
                raw_u = strm["GPSU"]
                # Format: YYMMDDHHMMSS.sss
                if len(raw_u) >= 12:
                    try:
                        parsed_dt = dt.datetime.strptime(raw_u[:12], "%y%m%d%H%M%S").replace(
                            tzinfo=dt.timezone.utc
                        )
                        frac = float(raw_u[12:]) if len(raw_u) > 12 else 0.0
                        gpsu_epoch = parsed_dt.timestamp() + frac - t_start
                    except ValueError:
                        pass

            # Skip GPS5 samples with no fix (GPSF == 0)
            if key == "GPS5":
                gpsf = strm.get("GPSF")
                if isinstance(gpsf, np.ndarray) and gpsf.size and gpsf.ravel()[0] == 0:
                    continue

            scaled = _apply_scale(arr, strm.get("SCAL"))
            n = scaled.shape[0]
            if n == 0:
                continue
            step = dur / n if dur > 0 else 1.0 / max(1, n)
            ts = t_start + np.arange(n, dtype=float) * step
            streams_t.setdefault(key, []).append(ts)
            streams_v.setdefault(key, []).append(scaled)

    if not streams_t:
        raise ValueError(f"{path.name}: GPMF track contained no decodable sensor streams")

    if gpsu_epoch is not None:
        meta["start_epoch"] = gpsu_epoch
        meta["start_time_utc"] = dt.datetime.fromtimestamp(
            gpsu_epoch, tz=dt.timezone.utc
        ).isoformat()

    # Pick master timebase: highest rate between GPS5/GPS9 or ACCL (capped reasonably)
    combined: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for key in streams_t:
        ts = np.concatenate(streams_t[key])
        vs = np.concatenate(streams_v[key], axis=0)
        order = np.argsort(ts, kind="stable")
        combined[key] = (ts[order], vs[order])

    # Choose base timeline: prefer GPS if present & dense, else highest-count stream
    # (cap to <= 60 Hz uniform grid if ACCL/GYRO is 200 Hz so overlays stay snappy)
    t_min = min(float(ts[0]) for ts, _ in combined.values())
    t_max = max(float(ts[-1]) for ts, _ in combined.values())
    best_key = max(combined, key=lambda k: combined[k][0].size)
    raw_base_t = combined[best_key][0]
    if raw_base_t.size > 2 and t_max > t_min:
        approx_hz = raw_base_t.size / (t_max - t_min)
        if approx_hz > 65.0:
            base_t = np.arange(t_min, t_max + 1e-6, 1.0 / 60.0)
        else:
            base_t = raw_base_t
    else:
        base_t = raw_base_t

    tel = Telemetry(time=base_t, meta=meta, source=str(path))

    def _add_resampled(key: str, label: str, unit: str, src_t: np.ndarray, src_v: np.ndarray) -> None:
        if src_t.size == 0 or not np.isfinite(src_v).any():
            return
        if src_t.size == base_t.size and np.allclose(src_t, base_t):
            vals = src_v
        else:
            vals = np.interp(base_t, src_t, src_v)
        tel.add(Channel(key=key, label=label, unit=unit, values=vals, kind="numeric"))

    # GPS5 or GPS9
    gps_key = "GPS9" if "GPS9" in combined else ("GPS5" if "GPS5" in combined else None)
    if gps_key:
        gt, gv = combined[gps_key]
        if gv.ndim == 2 and gv.shape[1] >= 5:
            _add_resampled("lat", "Latitude", "°", gt, gv[:, 0])
            _add_resampled("lon", "Longitude", "°", gt, gv[:, 1])
            _add_resampled("altitude", "Altitude", "m", gt, gv[:, 2])
            _add_resampled("speed", "Speed", "m/s", gt, gv[:, 3])
            _add_resampled("speed_3d", "Speed 3D", "m/s", gt, gv[:, 4])
            if gps_key == "GPS9" and gv.shape[1] >= 9:
                _add_resampled("hdop", "DOP", "", gt, gv[:, 7])
                _add_resampled("gps_fix", "GPS Fix", "", gt, gv[:, 8])

    if "ACCL" in combined:
        at, av = combined["ACCL"]
        if av.ndim == 2 and av.shape[1] >= 3:
            _add_resampled("accel_x", "Accel X", "m/s²", at, av[:, 0])
            _add_resampled("accel_y", "Accel Y", "m/s²", at, av[:, 1])
            _add_resampled("accel_z", "Accel Z", "m/s²", at, av[:, 2])
            _add_resampled("g_lon", "Longitudinal G", "G", at, av[:, 0] / 9.80665)
            _add_resampled("g_lat", "Lateral G", "G", at, av[:, 1] / 9.80665)
            total_g = np.sqrt(av[:, 0] ** 2 + av[:, 1] ** 2 + av[:, 2] ** 2) / 9.80665
            _add_resampled("g_total", "Total G", "G", at, total_g)

    if "GYRO" in combined:
        gt, gv = combined["GYRO"]
        if gv.ndim == 2 and gv.shape[1] >= 3:
            _add_resampled("gyro_x", "Gyro X", "°/s", gt, np.degrees(gv[:, 0]))
            _add_resampled("gyro_y", "Gyro Y", "°/s", gt, np.degrees(gv[:, 1]))
            _add_resampled("gyro_z", "Gyro Z", "°/s", gt, np.degrees(gv[:, 2]))

    if "CORI" in combined:
        ct, cv = combined["CORI"]
        if cv.ndim == 2 and cv.shape[1] >= 4:
            pitch, roll, yaw = _quat_to_euler_deg(cv)
            _add_resampled("pitch", "Pitch", "°", ct, pitch)
            _add_resampled("roll", "Roll", "°", ct, roll)
            _add_resampled("yaw", "Yaw", "°", ct, yaw)

    for raw_k, ch_k, lbl, u in (
        ("TMPC", "temperature", "Temperature", "°C"),
        ("ISOE", "iso", "ISO", ""),
        ("SHUT", "shutter", "Shutter", "s"),
        ("WBAL", "color_temp", "Color Temp", "K"),
    ):
        if raw_k in combined:
            st, sv = combined[raw_k]
            _add_resampled(ch_k, lbl, u, st, sv.ravel())

    return tel
