"""Tests for alpha compositing fixes, all 22 widgets, new presets, AST sandbox,
direct video compositing, batch CLI, and local Web Studio."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from telemetry_overlay.cli import main
from telemetry_overlay.config import Config, load_config, suggest_config
from telemetry_overlay.data import Channel, Telemetry, load
from telemetry_overlay.export import export_composite_video, find_ffmpeg
from telemetry_overlay.expressions import evaluate
from telemetry_overlay.render import build_renderer
from telemetry_overlay.studio import create_studio_server
from telemetry_overlay.theme import THEMES

from conftest import CSV_SAMPLE, EXAMPLES, PRESETS, write_csv

GPX_SAMPLE = EXAMPLES / "circuit_lap.gpx"
SRT_SAMPLE = EXAMPLES / "drone_flight.srt"


def _rich_telemetry() -> Telemetry:
    """Synthetic telemetry covering GPS, attitude, pedals, RC sticks, laps & text."""
    t = np.linspace(0.0, 20.0, 101)
    tel = Telemetry(time=t, meta={"start_epoch": 1750000000.0, "motor": "H59"}, source="mem")
    tel.add(Channel("lat", "Latitude", "°", 49.2 + 0.003 * np.sin(t / 3.0)))
    tel.add(Channel("lon", "Longitude", "°", 16.5 + 0.004 * np.cos(t / 3.0)))
    tel.add(Channel("altitude", "Altitude", "m", 300.0 + 150.0 * np.sin(np.pi * t / 20.0)))
    tel.add(Channel("speed_kmh", "Speed", "km/h", 80.0 + 60.0 * np.sin(t / 2.0)))
    tel.add(Channel("vspeed", "VSI", "m/s", 5.0 * np.cos(np.pi * t / 20.0)))
    tel.add(Channel("bearing", "Heading", "°", (t * 18.0) % 360.0))
    tel.add(Channel("pitch", "Pitch", "°", 12.0 * np.sin(t / 2.5)))
    tel.add(Channel("roll", "Roll", "°", 25.0 * np.cos(t / 2.0)))
    tel.add(Channel("lean_angle", "Lean", "°", 35.0 * np.sin(t / 2.0)))
    tel.add(Channel("g_lat", "Lat G", "G", 0.9 * np.sin(t / 2.0)))
    tel.add(Channel("g_lon", "Lon G", "G", 0.6 * np.cos(t / 2.0)))
    tel.add(Channel("throttle", "Throttle", "%", 50.0 + 50.0 * np.sin(t)))
    tel.add(Channel("brake", "Brake", "%", np.maximum(0.0, -50.0 * np.sin(t))))
    tel.add(Channel("steering", "Steering", "°", 90.0 * np.cos(t / 2.0)))
    tel.add(Channel("gear", "Gear", "", np.clip(np.round(1 + t / 4.0), 1, 6)))
    tel.add(Channel("rc_yaw", "Yaw", "", np.sin(t)))
    tel.add(Channel("rc_throttle", "Thr", "", np.cos(t)))
    tel.add(Channel("rc_roll", "Roll", "", np.sin(t * 1.3)))
    tel.add(Channel("rc_pitch", "Pitch", "", np.cos(t * 1.3)))
    tel.add(Channel("lap", "Lap", "", np.where(t < 10.0, 1.0, 2.0)))
    tel.add(Channel("lap_time", "Lap Time", "s", t % 10.0))
    tel.add(Channel("last_lap", "Last Lap", "s", np.where(t < 10.0, np.nan, 10.0)))
    tel.add(Channel("best_lap", "Best Lap", "s", np.where(t < 10.0, np.nan, 10.0)))
    tel.add(Channel("lap_delta", "Delta", "s", np.where(t < 10.0, np.nan, -0.25)))
    phases = np.array(["BOOST" if x < 6 else ("COAST" if x < 12 else "DESCENT") for x in t], dtype=object)
    tel.add(Channel("phase", "Phase", "", np.full_like(t, np.nan), kind="text", text_values=phases))
    tel.add(Channel("valve_open", "Valve Open", "", np.where(t > 5, 1.0, 0.0), kind="boolean"))
    return tel


def test_panel_alpha_compositing_never_punches_holes():
    """Regression test: translucent panels over opaque background must stay alpha=255."""
    tel = load(CSV_SAMPLE)
    cfg = Config(
        width=400, height=300, fps=30, background="#121620",
        theme=THEMES["night"], events=False,
        widgets=[
            {"type": "gauge", "channel": "thrust", "x": 10, "y": 10, "width": 160, "height": 160},
            {"type": "bar", "channel": "thrust", "x": 190, "y": 20, "width": 180},
            {"type": "table", "rows": ["thrust"], "x": 190, "y": 120, "width": 180},
            {"type": "led", "channel": "pyro_active", "x": 20, "y": 210},
        ],
    )
    img = build_renderer(cfg, telemetry=tel).render_at(3.0)
    alpha = np.array(img.getchannel("A"))
    assert alpha.min() == 255
    # Verify panel region got darkened instead of lightened/punched
    px = img.getpixel((200, 130))
    assert px[0] <= 18 and px[1] <= 22 and px[2] <= 32 and px[3] == 255


@pytest.mark.parametrize("spec", [
    {"type": "map", "x": 10, "y": 10, "width": 200, "height": 200},
    {"type": "map", "mode": "follow", "x": 10, "y": 10, "width": 200, "height": 200},
    {"type": "gforce", "x": 10, "y": 10, "width": 180, "height": 190},
    {"type": "attitude", "x": 10, "y": 10, "width": 180, "height": 180},
    {"type": "compass", "style": "tape", "x": 10, "y": 10, "width": 300, "height": 64},
    {"type": "compass", "style": "rose", "x": 10, "y": 10, "width": 160, "height": 160},
    {"type": "tape", "channel": "altitude", "x": 10, "y": 10, "width": 100, "height": 240},
    {"type": "lean", "x": 10, "y": 10, "width": 200, "height": 140},
    {"type": "elevation", "x": 10, "y": 10, "width": 300, "height": 120},
    {"type": "sparkline", "channel": "speed_kmh", "x": 10, "y": 10, "width": 200, "height": 90},
    {"type": "pedals", "x": 10, "y": 10, "width": 240, "height": 130},
    {"type": "sticks", "x": 10, "y": 10, "width": 260, "height": 130},
    {"type": "lap_timer", "x": 10, "y": 10, "width": 240, "height": 130},
    {"type": "progress", "x": 10, "y": 20, "width": 400, "height": 30},
    {"type": "badge", "channel": "phase", "x": 10, "y": 10,
     "colors": {"boost": "#ef4444", "coast": "#f59e0b"}},
    {"type": "badge", "channel": "gear", "label": "G", "x": 10, "y": 10},
    {"type": "bar", "channel": "vspeed", "orientation": "vertical", "bidirectional": True,
     "min": -10, "max": 10, "ticks": 4, "x": 10, "y": 10, "width": 80, "height": 200},
    {"type": "gauge", "channel": "speed_kmh", "shift_lights": True, "peak_marker": True,
     "show_scale": True, "x": 10, "y": 10, "width": 200, "height": 200},
    {"type": "readout", "channel": "phase", "x": 10, "y": 10},
    {"type": "readout", "expression": "speed_kmh / 3.6", "unit": "m/s", "x": 10, "y": 10},
    {"type": "table", "rows": [{"channel": "phase"}, {"expression": "altitude * 3.28084", "unit": "ft"}],
     "x": 10, "y": 10},
    {"type": "timer", "style": "clock", "show_speed": True, "x": 10, "y": 10},
    {"type": "label", "text": "Motor {meta.motor}", "x": 10, "y": 10},
])
def test_every_new_and_upgraded_widget_renders(spec):
    tel = _rich_telemetry()
    cfg = Config(width=640, height=360, fps=30, widgets=[spec], theme=THEMES["night"], events=False)
    img = build_renderer(cfg, telemetry=tel).render_at(11.0)
    assert np.array(img.getchannel("A")).max() > 0


def test_ast_sandbox_allows_safe_names_with_open_or_import_substrings():
    tel = _rich_telemetry()
    res = evaluate("valve_open * 100", tel)
    assert res[-1] == pytest.approx(100.0)
    # New scientific & conversion functions
    assert evaluate("to_ft(altitude)", tel).max() == pytest.approx(tel["altitude"].values.max() * 3.280839895)
    assert evaluate("ema(speed_kmh, 0.3)", tel).shape == tel.time.shape
    assert evaluate("despike(speed_kmh, 5, 3.0)", tel).shape == tel.time.shape
    assert evaluate("lowpass(speed_kmh, 1.5)", tel).shape == tel.time.shape
    assert evaluate("norm(g_lat, g_lon)", tel).shape == tel.time.shape
    # Still blocks attribute access & unsafe syntax
    with pytest.raises(ValueError):
        evaluate("valve_open.__class__", tel)
    with pytest.raises(ValueError):
        evaluate("[x for x in speed_kmh]", tel)


def test_image_widget_relative_path_anchor_and_rotation(tmp_path):
    logo = tmp_path / "arrow.png"
    im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    im.putpixel((20, 5), (255, 0, 0, 255))
    im.save(logo)
    cfg_p = tmp_path / "overlay.json"
    cfg_p.write_text(json.dumps({
        "output": {"resolution": "320x180", "fps": 30},
        "widgets": [{
            "type": "image", "path": "arrow.png", "x": "90%", "y": "90%",
            "anchor": "bottom-right", "rotate_channel": "bearing",
        }],
    }), encoding="utf-8")
    cfg = load_config(cfg_p)
    img = build_renderer(cfg, telemetry=_rich_telemetry()).render_at(5.0)
    assert np.array(img.getchannel("A")).max() > 0


@pytest.mark.parametrize("preset", [
    "rocket_thrust", "rocket_flight", "motorsport", "cycling",
    "aviation", "drone_fpv", "action_cam", "hud", "broadcast", "minimal",
])
def test_all_ten_bundled_presets_render_cleanly(preset):
    cfg = load_config(PRESETS / f"{preset}.yaml")
    cfg.width, cfg.height = 640, 360
    tel = load(CSV_SAMPLE) if preset in ("rocket_thrust", "hud", "broadcast", "minimal") else _rich_telemetry()
    renderer = build_renderer(cfg, telemetry=tel)
    assert renderer.frame_count > 0
    assert np.array(renderer.render_frame(renderer.frame_count // 2).getchannel("A")).max() > 0


@pytest.mark.parametrize("theme_name", sorted(THEMES))
def test_all_ten_themes_render(theme_name):
    cfg = Config(
        width=320, height=180, fps=30, theme=THEMES[theme_name], events=False,
        widgets=[{"type": "readout", "channel": "thrust", "x": 10, "y": 10}],
    )
    img = build_renderer(cfg, telemetry=load(CSV_SAMPLE)).render_at(2.3)
    assert np.array(img.getchannel("A")).max() > 0


def test_smart_suggest_config_detects_domains():
    gpx_cfg = suggest_config(load(GPX_SAMPLE))
    assert "Cycling" in gpx_cfg["name"]
    assert any(w["type"] == "map" for w in gpx_cfg["widgets"])

    srt_cfg = suggest_config(load(SRT_SAMPLE))
    assert any(w["type"] == "map" for w in srt_cfg["widgets"])


def test_discovery_and_batch_cli(tmp_path, capsys):
    assert main(["formats"]) == 0
    assert "gopro_mp4" in capsys.readouterr().out
    assert main(["widgets"]) == 0
    assert "attitude" in capsys.readouterr().out

    out_dir = tmp_path / "batch_out"
    assert main(["batch", str(GPX_SAMPLE), str(SRT_SAMPLE),
                 "--extract-to", "csv", "-o", str(out_dir)]) == 0
    assert (out_dir / "circuit_lap.csv").exists()
    assert (out_dir / "drone_flight.csv").exists()


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not available")
def test_direct_video_compositing_and_new_codecs(tmp_path):
    ff = find_ffmpeg()
    src_video = tmp_path / "footage.mp4"
    # Generate a tiny 0.4s synthetic test footage MP4
    res = subprocess.run([
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=0x1e293b:s=320x180:r=15:d=0.4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src_video),
    ], capture_output=True)
    assert res.returncode == 0

    out_mp4 = tmp_path / "composited.mp4"
    rc = main([
        "render", str(CSV_SAMPLE), "-c", "minimal",
        "--video", str(src_video), "-o", str(out_mp4),
        "-r", "320x180", "--fps", "15", "--start", "2.0", "--end", "2.3",
        "--quiet", "--overwrite",
    ])
    assert rc == 0
    assert out_mp4.exists() and out_mp4.stat().st_size > 1000

    # Also test animated GIF & chroma green backdrop
    out_gif = tmp_path / "preview.gif"
    rc2 = main([
        "render", str(CSV_SAMPLE), "-c", "minimal", "-o", str(out_gif),
        "-r", "240x135", "--fps", "10", "--start", "2.0", "--end", "2.2",
        "--chroma", "green", "-j", "1", "--quiet", "--overwrite",
    ])
    assert rc2 == 0 and out_gif.exists() and out_gif.stat().st_size > 500


def test_local_web_studio_api():
    server = create_studio_server(GPX_SAMPLE, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(f"{base}/").read().decode("utf-8")
        assert "Telemetry Overlay Studio" in html

        state = json.loads(urllib.request.urlopen(f"{base}/api/state").read().decode("utf-8"))
        assert state["samples"] == 241
        assert "motorsport" in state["presets"]

        png = urllib.request.urlopen(f"{base}/api/frame?t=30.0").read()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

        req = urllib.request.Request(
            f"{base}/api/config",
            data=json.dumps({"preset": "cycling"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        updated = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
        assert updated["theme"] == "cycling"

        gpx_dl = urllib.request.urlopen(f"{base}/api/export?format=geojson").read().decode("utf-8")
        assert json.loads(gpx_dl)["type"] == "FeatureCollection"
    finally:
        server.shutdown()
        server.server_close()
