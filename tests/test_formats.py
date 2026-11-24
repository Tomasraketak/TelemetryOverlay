"""Tests for all telemetry input formats, enrichment, laps, filters, merging and extraction."""

from __future__ import annotations

import json
import math
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest

from telemetry_overlay.cli import main
from telemetry_overlay.data import LoadOptions, load, load_many
from telemetry_overlay.extract import EXPORT_FORMATS, export_telemetry
from telemetry_overlay.formats.enrichment import (
    apply_filters, concat_telemetry, detect_laps, merge_telemetry,
)

from conftest import CSV_SAMPLE, EXAMPLES, write_csv

GPX_SAMPLE = EXAMPLES / "circuit_lap.gpx"
SRT_SAMPLE = EXAMPLES / "drone_flight.srt"


# ---------------------------------------------------------------------------
# GPX, KML/KMZ, TCX, NMEA, IGC, DJI SRT
# ---------------------------------------------------------------------------

def test_gpx_loads_extensions_and_auto_enriches():
    tel = load(GPX_SAMPLE)
    assert tel.meta["format"] == "gpx"
    assert tel.has_gps
    assert tel.time.size == 241
    for key in ("lat", "lon", "altitude", "heart_rate", "cadence", "power",
                "distance", "speed", "speed_kmh", "bearing", "grade",
                "vspeed", "elevation_gain", "g_lon", "g_lat", "g_total", "lean_angle"):
        assert key in tel, f"missing expected channel {key}"
    assert tel["distance"].values[-1] > 2000.0
    assert "start_epoch" in tel.meta


def test_gps_lap_detection_finds_both_laps():
    tel = load(GPX_SAMPLE, LoadOptions(detect_laps=True))
    laps = tel.meta.get("laps")
    assert isinstance(laps, list) and len(laps) >= 1
    assert "lap" in tel and "lap_time" in tel and "best_lap" in tel
    assert tel["lap"].values[-1] >= 2.0


def test_dji_srt_loads_gps_altitude_and_camera_metadata():
    tel = load(SRT_SAMPLE)
    assert tel.meta["format"] == "dji_srt"
    assert tel.has_gps
    assert tel["iso"].values[0] == pytest.approx(100.0)
    assert tel["fnum"].values[0] == pytest.approx(2.8)
    assert tel["focal_length"].values[0] == pytest.approx(24.0)
    assert tel["rel_altitude"].values.max() > 55.0
    assert "speed_kmh" in tel


def test_kml_and_kmz_loading(tmp_path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
      <Document><Placemark><gx:Track>
        <when>2025-05-01T10:00:00Z</when>
        <when>2025-05-01T10:00:01Z</when>
        <when>2025-05-01T10:00:02Z</when>
        <gx:coord>16.5000 49.2000 300</gx:coord>
        <gx:coord>16.5005 49.2002 305</gx:coord>
        <gx:coord>16.5010 49.2004 312</gx:coord>
      </gx:Track></Placemark></Document>
    </kml>"""
    kml_p = tmp_path / "flight.kml"
    kml_p.write_text(kml, encoding="utf-8")
    tel = load(kml_p)
    assert tel.meta["format"] == "kml"
    assert tel.time.size == 3
    assert tel["altitude"].values[-1] == pytest.approx(312.0)

    kmz_p = tmp_path / "flight.kmz"
    with zipfile.ZipFile(kmz_p, "w") as zf:
        zf.writestr("doc.kml", kml)
    tel_z = load(kmz_p)
    assert tel_z.meta["format"] == "kmz"
    assert tel_z.time.size == 3


def test_tcx_loading(tmp_path):
    tcx = """<?xml version="1.0" encoding="UTF-8"?>
    <TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
      <Activities><Activity Sport="Biking"><Lap><Track>
        <Trackpoint>
          <Time>2025-06-01T08:00:00Z</Time>
          <Position><LatitudeDegrees>49.2000</LatitudeDegrees><LongitudeDegrees>16.5000</LongitudeDegrees></Position>
          <AltitudeMeters>250.0</AltitudeMeters><DistanceMeters>0.0</DistanceMeters>
          <HeartRateBpm><Value>140</Value></HeartRateBpm><Cadence>85</Cadence>
          <Extensions><TPX><Watts>260</Watts></TPX></Extensions>
        </Trackpoint>
        <Trackpoint>
          <Time>2025-06-01T08:00:02Z</Time>
          <Position><LatitudeDegrees>49.2003</LatitudeDegrees><LongitudeDegrees>16.5003</LongitudeDegrees></Position>
          <AltitudeMeters>252.5</AltitudeMeters><DistanceMeters>40.0</DistanceMeters>
          <HeartRateBpm><Value>145</Value></HeartRateBpm><Cadence>90</Cadence>
          <Extensions><TPX><Watts>290</Watts></TPX></Extensions>
        </Trackpoint>
      </Track></Lap></Activity></Activities>
    </TrainingCenterDatabase>"""
    p = tmp_path / "ride.tcx"
    p.write_text(tcx, encoding="utf-8")
    tel = load(p)
    assert tel.meta["format"] == "tcx"
    assert tel.meta["sport"] == "Biking"
    assert tel["heart_rate"].values.tolist() == pytest.approx([140.0, 145.0])
    assert tel["power"].values.tolist() == pytest.approx([260.0, 290.0])


def test_nmea_0183_loading_with_marine_wind(tmp_path):
    nmea = "\n".join([
        "$GPRMC,120000.00,A,4912.0000,N,01630.0000,E,10.0,090.0,010625,,,A*00",
        "$GPGGA,120000.00,4912.0000,N,01630.0000,E,1,09,0.9,250.5,M,45.0,M,,*00",
        "$IIMWV,045.0,R,12.0,N,A*00",
        "$GPRMC,120001.00,A,4912.0000,N,01630.0100,E,12.0,092.0,010625,,,A*00",
        "$GPGGA,120001.00,4912.0000,N,01630.0100,E,1,10,0.8,251.0,M,45.0,M,,*00",
        "$IIMWV,048.0,R,14.0,N,A*00",
    ])
    p = tmp_path / "track.nmea"
    p.write_text(nmea, encoding="utf-8")
    tel = load(p)
    assert tel.meta["format"] == "nmea"
    assert tel.time.size == 2
    assert tel["lat"].values[0] == pytest.approx(49.2, abs=1e-4)
    assert tel["satellites"].values[1] == pytest.approx(10.0)
    assert tel["wind_angle"].values[0] == pytest.approx(45.0)


def test_igc_gliding_flight_log(tmp_path):
    igc = "\n".join([
        "AXYZ001",
        "HFDTE010625",
        "HFPLTPILOT:Test Pilot",
        "I013638TAS",
        "B1000004912000N01630000EA0050000520120",
        "B1000024912050N01630100EA0050500526125",
        "B1000044912100N01630200EA0051200534130",
    ])
    p = tmp_path / "glider.igc"
    p.write_text(igc, encoding="utf-8")
    tel = load(p)
    assert tel.meta["format"] == "igc"
    assert tel.time.size == 3
    assert tel["altitude"].values[-1] == pytest.approx(534.0)
    assert tel["tas"].values[-1] == pytest.approx(130.0)


# ---------------------------------------------------------------------------
# Pure-Python Binary Parsers: Garmin FIT & GoPro GPMF (MP4 + BIN)
# ---------------------------------------------------------------------------

def _build_minimal_fit() -> bytes:
    """Build a spec-compliant binary Garmin .FIT file with 3 record messages."""
    # Definition message for local 0 -> global 20 (record), little-endian
    # Fields: timestamp(253, uint32=0x86), lat(0, sint32=0x85), lon(1, sint32=0x85),
    #         altitude(2, uint16=0x84), heart_rate(3, uint8=0x02), power(7, uint16=0x84)
    body = bytearray()
    body += bytes([0x40, 0x00, 0x00]) + struct.pack("<H", 20) + bytes([6])
    body += bytes([253, 4, 0x86, 0, 4, 0x85, 1, 4, 0x85, 2, 2, 0x84, 3, 1, 0x02, 7, 2, 0x84])

    semi = (2 ** 31) / 180.0
    base_ts = 1_100_000_000
    for i in range(3):
        lat_sc = int(round((49.2 + i * 0.0005) * semi))
        lon_sc = int(round((16.5 + i * 0.0005) * semi))
        alt_raw = int(round((300.0 + i * 2.0 + 500.0) * 5.0))
        hr = 145 + i * 3
        pwr = 250 + i * 20
        body += bytes([0x00])
        body += struct.pack("<IiiHBH", base_ts + i * 2, lat_sc, lon_sc, alt_raw, hr, pwr)

    header = struct.pack("<BBHI4sH", 14, 0x20, 2100, len(body), b".FIT", 0)
    return header + bytes(body) + b"\x00\x00"


def test_garmin_fit_binary_parser(tmp_path):
    p = tmp_path / "activity.fit"
    p.write_bytes(_build_minimal_fit())
    tel = load(p)
    assert tel.meta["format"] == "fit"
    assert tel.time.tolist() == pytest.approx([0.0, 2.0, 4.0])
    assert tel["lat"].values[0] == pytest.approx(49.2, abs=1e-4)
    assert tel["altitude"].values[2] == pytest.approx(304.0, abs=0.2)
    assert tel["heart_rate"].values.tolist() == pytest.approx([145.0, 148.0, 151.0])
    assert tel["power"].values.tolist() == pytest.approx([250.0, 270.0, 290.0])


def _klv(fourcc: str, typ: str, struct_size: int, repeat: int, payload: bytes) -> bytes:
    pad = (-len(payload)) % 4
    tb = 0 if typ == "" else ord(typ)
    return fourcc.encode("ascii") + bytes([tb, struct_size]) + struct.pack(">H", repeat) + payload + b"\x00" * pad


def _build_gpmf_chunk() -> bytes:
    # Build STRM with GPS5 (2 samples) and STRM with ACCL + TMPC + CORI
    gps_scal = _klv("SCAL", "l", 4, 5, struct.pack(">5i", 10_000_000, 10_000_000, 1000, 1000, 100))
    gps_fix = _klv("GPSF", "L", 4, 1, struct.pack(">I", 3))
    gps_u = _klv("GPSU", "U", 16, 1, b"250601100000.000")
    gps_pts = struct.pack(
        ">5i5i",
        492000000, 165000000, 300000, 15000, 1520,
        492001000, 165001000, 302000, 16000, 1620,
    )
    gps5 = _klv("GPS5", "l", 20, 2, gps_pts)
    strm_gps = _klv("STRM", "", 1, len(gps_scal + gps_fix + gps_u + gps5), gps_scal + gps_fix + gps_u + gps5)

    accl_scal = _klv("SCAL", "s", 2, 1, struct.pack(">h", 100))
    accl_pts = struct.pack(">3h3h", 200, 50, 981, 300, -50, 981)
    accl = _klv("ACCL", "s", 6, 2, accl_pts)
    strm_accl = _klv("STRM", "", 1, len(accl_scal + accl), accl_scal + accl)

    cori_scal = _klv("SCAL", "s", 2, 1, struct.pack(">h", 32767))
    cori_pts = struct.pack(">4h4h", 32767, 0, 0, 0, 32767, 0, 0, 0)
    cori = _klv("CORI", "s", 8, 2, cori_pts)
    strm_cori = _klv("STRM", "", 1, len(cori_scal + cori), cori_scal + cori)

    tmpc = _klv("TMPC", "f", 4, 1, struct.pack(">f", 38.5))
    strm_tmp = _klv("STRM", "", 1, len(tmpc), tmpc)

    inner = strm_gps + strm_accl + strm_cori + strm_tmp
    return _klv("DEVC", "", 1, len(inner), inner)


def _mp4_box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, btype) + payload


def _build_minimal_gopro_mp4(gpmf_payload: bytes) -> bytes:
    ftyp = _mp4_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom")
    mdat_offset = len(ftyp) + 8
    mdat = _mp4_box(b"mdat", gpmf_payload)

    mdhd = _mp4_box(b"mdhd", struct.pack(">IIIIII", 0, 0, 0, 1000, 1000, 0))
    stsd = _mp4_box(b"stsd", b"\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x10gpmd\x00\x00\x00\x00\x00\x00\x00\x01")
    stts = _mp4_box(b"stts", struct.pack(">IIII", 0, 1, 1, 1000))
    stsc = _mp4_box(b"stsc", struct.pack(">IIIII", 0, 1, 1, 1, 1))
    stsz = _mp4_box(b"stsz", struct.pack(">IIII", 0, 0, 1, len(gpmf_payload)))
    stco = _mp4_box(b"stco", struct.pack(">III", 0, 1, mdat_offset))
    stbl = _mp4_box(b"stbl", stsd + stts + stsc + stsz + stco)
    minf = _mp4_box(b"minf", stbl)
    mdia = _mp4_box(b"mdia", mdhd + minf)
    trak = _mp4_box(b"trak", mdia)
    moov = _mp4_box(b"moov", trak)
    return ftyp + mdat + moov


def test_gopro_gpmf_mp4_and_raw_bin_parser(tmp_path):
    payload = _build_gpmf_chunk()
    raw_p = tmp_path / "sensor.gpmf"
    raw_p.write_bytes(payload)
    tel_raw = load(raw_p)
    assert tel_raw.meta["format"] == "gopro_gpmf"
    assert tel_raw["lat"].values[0] == pytest.approx(49.2, abs=1e-4)
    assert tel_raw["altitude"].values[1] == pytest.approx(302.0, abs=0.1)
    assert tel_raw["speed"].values[0] == pytest.approx(15.0, abs=0.1)
    assert tel_raw["temperature"].values[0] == pytest.approx(38.5, abs=0.1)
    assert "pitch" in tel_raw and "roll" in tel_raw and "g_total" in tel_raw

    mp4_p = tmp_path / "GX010001.MP4"
    mp4_p.write_bytes(_build_minimal_gopro_mp4(payload))
    tel_mp4 = load(mp4_p)
    assert tel_mp4.has_gps
    assert tel_mp4["lat"].values[0] == pytest.approx(49.2, abs=1e-4)


# ---------------------------------------------------------------------------
# JSON, JSONL, GeoJSON, CSV enhancements (ISO timestamps, text channels, preamble)
# ---------------------------------------------------------------------------

def test_json_jsonl_geojson_loading(tmp_path):
    # JSON list of records
    jp = tmp_path / "log.json"
    jp.write_text(json.dumps([
        {"time": 0.0, "speed": 10.0, "phase": "PAD"},
        {"time": 1.0, "speed": 25.0, "phase": "BOOST"},
    ]), encoding="utf-8")
    tel = load(jp)
    assert tel["speed"].values.tolist() == pytest.approx([10.0, 25.0])
    assert tel["phase"].is_text
    assert tel.text_at("phase", 0.9) == "BOOST"

    # JSONL
    jlp = tmp_path / "log.jsonl"
    jlp.write_text('{"time": 0, "v": 1}\n{"time": 1, "v": 3}\n', encoding="utf-8")
    assert load(jlp)["v"].values[-1] == pytest.approx(3.0)

    # GeoJSON
    gjp = tmp_path / "track.geojson"
    gjp.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[16.5, 49.2, 300], [16.501, 49.201, 310]]},
            "properties": {"coordTimes": ["2025-06-01T10:00:00Z", "2025-06-01T10:00:05Z"]},
        }],
    }), encoding="utf-8")
    gtel = load(gjp)
    assert gtel.has_gps and gtel.duration == pytest.approx(5.0)


def test_csv_iso_timestamps_comments_racechrono_and_text_channels(tmp_path):
    p = write_csv(
        tmp_path / "iso.csv",
        "# Vehicle: Rocket-1\n"
        "Timestamp,Altitude [m],Phase\n"
        "2025-06-01T12:00:00.000Z,0,PAD\n"
        "2025-06-01T12:00:01.500Z,120,BOOST\n"
        "2025-06-01T12:00:03.000Z,310,COAST\n",
    )
    tel = load(p)
    assert tel.meta.get("Vehicle") == "Rocket-1"
    assert "start_epoch" in tel.meta
    assert tel.time.tolist() == pytest.approx([0.0, 1.5, 3.0])
    assert tel["phase"].is_text
    assert tel.text_at("phase", 1.5) == "BOOST"


def test_csv_headerless_european_decimal(tmp_path):
    p = write_csv(tmp_path / "euro.csv", "0,0;10,5\n0,5;20,5\n1,0;30,5\n")
    tel = load(p)
    assert tel.time.tolist() == pytest.approx([0.0, 0.5, 1.0])
    assert tel["col2"].values.tolist() == pytest.approx([10.5, 20.5, 30.5])


# ---------------------------------------------------------------------------
# Filters, Merge, Concat & Telemetry Extractor (all 7 export formats)
# ---------------------------------------------------------------------------

def test_signal_filters_despike_smooth_lowpass_resample(tmp_path):
    rows = ["time,val"] + [f"{i * 0.1:.1f},{10.0 if i != 10 else 999.0}" for i in range(30)]
    p = write_csv(tmp_path / "noisy.csv", "\n".join(rows) + "\n")
    tel = load(p)
    apply_filters(tel, [
        {"type": "despike", "window": 5, "sigma": 3.0},
        {"type": "smooth", "window": 3},
        {"type": "lowpass", "cutoff_hz": 2.0},
        {"type": "resample", "hz": 20.0},
    ])
    assert tel["val"].values.max() < 20.0
    assert tel.sample_rate == pytest.approx(20.0, rel=0.05)


def test_merge_and_concat_telemetry(tmp_path):
    a = write_csv(tmp_path / "a.csv", "time,speed\n0,10\n1,20\n2,30\n")
    b = write_csv(tmp_path / "b.csv", "time,hr\n0,130\n1,140\n2,150\n")
    merged = load_many([a, b], mode="merge")
    assert "speed" in merged and "hr" in merged
    assert merged["hr"].values[1] == pytest.approx(140.0)

    c1 = write_csv(tmp_path / "p1.csv", "time,v\n0,1\n1,2\n")
    c2 = write_csv(tmp_path / "p2.csv", "time,v\n0,3\n1,4\n")
    joined = load_many([c1, c2], mode="concat")
    assert joined.time.size == 4
    assert joined["v"].values.tolist() == pytest.approx([1.0, 2.0, 3.0, 4.0])


@pytest.mark.parametrize("fmt", sorted(EXPORT_FORMATS))
def test_extract_all_export_formats_via_cli(tmp_path, fmt):
    out = tmp_path / f"exported.{fmt}"
    rc = main(["extract", str(GPX_SAMPLE), "-o", str(out), "--overwrite"])
    assert rc == 0
    assert out.exists() and out.stat().st_size > 200
    if fmt == "mgjson":
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["version"] == "MGJSON2.0.0"
        assert len(doc["dataDynamicSamples"]) > 0
    elif fmt == "geojson":
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["type"] == "FeatureCollection"
    elif fmt == "gpx":
        reloaded = load(out)
        assert reloaded.has_gps and reloaded.time.size == 241
