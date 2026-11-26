"""Tests for rocket motor burn widget, 3D rocket attitude widget, upgraded Studio,
icon generator, auto-updater, and Desktop shortcut installer.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from telemetry_overlay.cli import main
from telemetry_overlay.config import Config, load_config, suggest_config
from telemetry_overlay.data import Channel, Telemetry, load
from telemetry_overlay.export import find_ffmpeg
from telemetry_overlay.launcher import (
    create_desktop_launcher,
    generate_app_icon,
    self_update,
)
from telemetry_overlay.render import build_renderer
from telemetry_overlay.studio import create_studio_server
from telemetry_overlay.widgets.rocketry import nar_motor_class

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
PRESETS = REPO / "presets"


def _alpha_max(img: Image.Image) -> int:
    return int(np.array(img.getchannel("A")).max())


# ---------------------------------------------------------------------------
# 1. NAR / TRA Motor Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("impulse,avg_t,expected_prefix", [
    (0.0, 0.0, "--"),
    (1.9, 3.0, "A"),
    (4.5, 6.0, "B"),
    (9.0, 10.0, "C"),
    (18.0, 15.0, "D"),
    (35.0, 25.0, "E"),
    (75.0, 50.0, "F"),
    (120.0, 90.0, "G"),
    (250.0, 180.0, "H"),
    (500.0, 300.0, "I"),
    (1000.0, 600.0, "J"),
    (2000.0, 1200.0, "K"),
    (4000.0, 2000.0, "L"),
    (8000.0, 4000.0, "M"),
    (15000.0, 6000.0, "N"),
    (30000.0, 10000.0, "O"),
])
def test_nar_motor_class(impulse, avg_t, expected_prefix):
    desig, pct = nar_motor_class(impulse, avg_t)
    assert desig.startswith(expected_prefix)
    if impulse > 0:
        assert 1 <= pct <= 100
    else:
        assert pct == 0


# ---------------------------------------------------------------------------
# 2. MotorBurnWidget (motor_burn)
# ---------------------------------------------------------------------------

def test_motor_burn_widget_static_fire_phases():
    tel = load(EXAMPLES / "static_fire_thrust.csv")
    cfg = Config(
        width=960, height=540, fps=30,
        widgets=[{
            "type": "motor_burn",
            "channel": "thrust",
            "x": 20, "y": 20, "width": 760, "height": 320,
            "title": "Test Stand",
        }],
    )
    renderer = build_renderer(cfg, telemetry=tel)
    f_standby = renderer.render_at(0.2)
    f_burning = renderer.render_at(2.3)
    f_burnout = renderer.render_at(5.5)

    assert _alpha_max(f_standby) > 0
    assert _alpha_max(f_burning) > 0
    assert _alpha_max(f_burnout) > 0
    # Frames across phases must visibly change (live curve, flame plume, phase badge)
    assert not np.array_equal(np.array(f_standby), np.array(f_burning))
    assert not np.array_equal(np.array(f_burning), np.array(f_burnout))


def test_motor_burn_widget_xlsx_czech_and_options():
    tel = load(EXAMPLES / "burn_001_data.xlsx")
    cfg = Config(
        width=960, height=540, fps=30,
        widgets=[{
            "type": "rocket_motor",  # alias test
            "channel": ["tah", "thrust"],
            "x": 20, "y": 20, "width": 700, "height": 300,
            "zoom_burn": True,
            "show_nozzle": False,
            "show_kgf": False,
            "show_events": False,
        }],
    )
    renderer = build_renderer(cfg, telemetry=tel)
    img = renderer.render_at(tel.t0 + tel.duration * 0.4)
    assert _alpha_max(img) > 0


# ---------------------------------------------------------------------------
# 3. Rocket3DWidget (rocket_3d) — Euler & Quaternion
# ---------------------------------------------------------------------------

def test_rocket_3d_widget_euler_and_flame():
    tel = load(EXAMPLES / "rocket_flight.csv")
    cfg = Config(
        width=960, height=540, fps=30,
        widgets=[{
            "type": "rocket_3d",
            "x": 20, "y": 20, "width": 320, "height": 360,
            "title": "3D Attitude",
            "thrust_channel": "thrust",
        }],
    )
    renderer = build_renderer(cfg, telemetry=tel)
    f_pad = renderer.render_at(0.1)
    f_boost = renderer.render_at(1.8)
    f_coast = renderer.render_at(11.0)

    assert _alpha_max(f_pad) > 0
    assert _alpha_max(f_boost) > 0
    assert _alpha_max(f_coast) > 0
    assert not np.array_equal(np.array(f_pad), np.array(f_boost))
    assert not np.array_equal(np.array(f_boost), np.array(f_coast))


def test_rocket_3d_widget_quaternion_mode():
    t = np.linspace(0.0, 2.0, 41)
    # Smoothly pitch from 0 deg to 45 deg around X axis: q = (cos(a/2), sin(a/2), 0, 0)
    ang = np.deg2rad(np.linspace(0.0, 45.0, 41))
    tel = Telemetry(
        time=t,
        channels={
            "quat_w": Channel("quat_w", "qw", "", np.cos(ang / 2.0)),
            "quat_x": Channel("quat_x", "qx", "", np.sin(ang / 2.0)),
            "quat_y": Channel("quat_y", "qy", "", np.zeros_like(ang)),
            "quat_z": Channel("quat_z", "qz", "", np.zeros_like(ang)),
        },
        source="quat_test.csv",
    )
    cfg = Config(
        width=640, height=400, fps=30,
        widgets=[{
            "type": "attitude_3d",  # alias test
            "x": 20, "y": 20, "width": 300, "height": 340,
            "show_grid": True, "show_zenith": True, "show_tilt_arc": True,
        }],
    )
    renderer = build_renderer(cfg, telemetry=tel)
    f0 = renderer.render_at(0.0)
    f1 = renderer.render_at(2.0)
    assert _alpha_max(f0) > 0 and _alpha_max(f1) > 0
    assert not np.array_equal(np.array(f0), np.array(f1))


# ---------------------------------------------------------------------------
# 4. Presets & Smart Domain Suggestions
# ---------------------------------------------------------------------------

def test_rocketry_presets_and_suggest_config():
    static_tel = load(EXAMPLES / "static_fire_thrust.csv")
    flight_tel = load(EXAMPLES / "rocket_flight.csv")

    # Bundled presets render cleanly
    sf_cfg = load_config(PRESETS / "static_fire_pro.yaml", {"resolution": "720p"})
    sf_img = build_renderer(sf_cfg, telemetry=static_tel).render_at(2.3)
    assert _alpha_max(sf_img) > 0

    rf_cfg = load_config(PRESETS / "rocket_flight.yaml", {"resolution": "720p"})
    rf_img = build_renderer(rf_cfg, telemetry=flight_tel).render_at(2.1)
    assert _alpha_max(rf_img) > 0

    # Smart suggest_config picks specialized rocketry widgets
    sug_flight = suggest_config(flight_tel)
    w_types_flight = {w["type"] for w in sug_flight["widgets"]}
    assert "motor_burn" in w_types_flight
    assert "rocket_3d" in w_types_flight

    sug_static = suggest_config(static_tel)
    w_types_static = {w["type"] for w in sug_static["widgets"]}
    assert "motor_burn" in w_types_static

    # Standalone IMU (pitch/roll without GPS or thrust) picks rocket_3d
    t = np.linspace(0.0, 5.0, 51)
    imu_tel = Telemetry(
        time=t,
        channels={
            "pitch": Channel("pitch", "Pitch", "deg", 15.0 * np.sin(t)),
            "roll": Channel("roll", "Roll", "deg", 10.0 * np.cos(t)),
            "yaw": Channel("yaw", "Yaw", "deg", t * 20.0),
        },
        source="imu.csv",
    )
    sug_imu = suggest_config(imu_tel)
    assert any(w["type"] == "rocket_3d" for w in sug_imu["widgets"])


# ---------------------------------------------------------------------------
# 5. Upgraded Web Studio (zero-arg startup, sample switcher, upload, render)
# ---------------------------------------------------------------------------

def test_studio_zero_arg_upload_samples_and_render():
    server = create_studio_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # Favicon & initial state
        fav = urllib.request.urlopen(f"{base}/favicon.svg").read().decode("utf-8")
        assert "<svg" in fav

        state = json.loads(urllib.request.urlopen(f"{base}/api/state").read().decode("utf-8"))
        assert state["source"] == "rocket_flight.csv"
        assert "rocket_flight.csv" in state["samples_list"]
        assert "static_fire_pro" in state["presets"]

        # Samples list endpoint
        samples = json.loads(urllib.request.urlopen(f"{base}/api/samples").read().decode("utf-8"))
        assert "static_fire_thrust.csv" in samples["samples"]

        # Switch sample via POST /api/sample
        req_sample = urllib.request.Request(
            f"{base}/api/sample",
            data=json.dumps({"name": "static_fire_thrust.csv"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sw = json.loads(urllib.request.urlopen(req_sample).read().decode("utf-8"))
        assert sw["source"] == "static_fire_thrust.csv"

        # Upload custom CSV via POST /api/upload
        csv_bytes = (
            "Time [s],Thrust [N],Pitch [deg],Roll [deg]\n"
            "0.0,0,0,0\n0.2,120,3,1\n0.4,180,6,2\n0.6,90,9,3\n0.8,0,12,4\n"
        ).encode("utf-8")
        req_up = urllib.request.Request(
            f"{base}/api/upload?filename=custom_motor.csv",
            data=csv_bytes,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        up = json.loads(urllib.request.urlopen(req_up).read().decode("utf-8"))
        assert up["source"] == "custom_motor.csv"
        assert up["samples"] == 5

        # Full-res PNG download
        png_full = urllib.request.urlopen(f"{base}/api/frame?t=0.4&full=1").read()
        assert png_full[:8] == b"\x89PNG\r\n\x1a\n"

        # Video render download (tiny fast GIF if ffmpeg available)
        if find_ffmpeg() is not None:
            req_ren = urllib.request.Request(
                f"{base}/api/render",
                data=json.dumps({"codec": "gif", "resolution": "320x180", "fps": 5}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            gif_bytes = urllib.request.urlopen(req_ren).read()
            assert gif_bytes[:6] in (b"GIF89a", b"GIF87a")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 6. App Icon Generator, Desktop Launcher & CLI Commands
# ---------------------------------------------------------------------------

def test_icon_generator_and_desktop_launchers(tmp_path):
    icons = generate_app_icon(tmp_path / "icons")
    assert icons["png"].is_file() and icons["ico"].is_file()
    with Image.open(icons["png"]) as im:
        assert im.size == (256, 256)
        assert im.mode == "RGBA"
    assert icons["ico"].stat().st_size > 1000

    # Linux .desktop launcher
    repo_linux = tmp_path / "repo_linux"
    desk_linux = tmp_path / "desk_linux"
    repo_linux.mkdir()
    res_l = create_desktop_launcher(
        repo_dir=repo_linux, desktop_dir=desk_linux, platform_override="linux",
    )
    assert res_l["launcher"].is_file()
    assert res_l["shortcut"].suffix == ".desktop"
    txt_l = res_l["launcher"].read_text(encoding="utf-8")
    assert "telemetry_overlay update" in txt_l
    assert "telemetry_overlay studio --open" in txt_l
    desk_txt = res_l["shortcut"].read_text(encoding="utf-8")
    assert "Telemetry Overlay" in desk_txt
    assert str(res_l["icon_png"]) in desk_txt

    # Windows .cmd launcher + shortcut fallback
    repo_win = tmp_path / "repo_win"
    desk_win = tmp_path / "desk_win"
    repo_win.mkdir()
    res_w = create_desktop_launcher(
        repo_dir=repo_win, desktop_dir=desk_win, platform_override="win32",
    )
    assert res_w["launcher"].name == "launch_telemetry_overlay.cmd"
    assert res_w["shortcut"].is_file()
    cmd_bytes = res_w["launcher"].read_bytes()
    assert b"\r\n" in cmd_bytes
    assert b"telemetry_overlay update" in cmd_bytes
    assert b"telemetry_overlay studio --open %*" in cmd_bytes

    # macOS .command launcher
    repo_mac = tmp_path / "repo_mac"
    desk_mac = tmp_path / "desk_mac"
    repo_mac.mkdir()
    res_m = create_desktop_launcher(
        repo_dir=repo_mac, desktop_dir=desk_mac, platform_override="darwin",
    )
    assert res_m["shortcut"].suffix == ".command"

    # Self-update safely skips non-source directories without network
    upd = self_update(repo_dir=tmp_path / "not_a_repo", quiet=True)
    assert upd["status"] == "skipped"


def test_cli_discovery_and_install_desktop(tmp_path, capsys):
    assert main(["widgets"]) == 0
    out_w = capsys.readouterr().out
    assert "motor_burn" in out_w
    assert "rocket_3d" in out_w

    assert main(["presets"]) == 0
    out_p = capsys.readouterr().out
    assert "static_fire_pro" in out_p

    fake_repo = tmp_path / "app"
    fake_desk = tmp_path / "Desktop"
    fake_repo.mkdir()
    assert main([
        "install-desktop",
        "--repo-dir", str(fake_repo),
        "--desktop-dir", str(fake_desk),
    ]) == 0
    out_d = capsys.readouterr().out
    assert "Desktop launcher created" in out_d
    assert any(fake_desk.iterdir())


def test_install_cmd_is_powershell_free_and_cmd_safe():
    """Guard install.cmd against PowerShell dependency and cmd.exe parenthesis bugs."""
    raw = (REPO / "install.cmd").read_bytes()
    assert b"\r\n" in raw, "install.cmd must use CRLF line endings for cmd.exe"
    text = raw.decode("utf-8")
    assert "powershell" not in text.lower(), "install.cmd must not invoke PowerShell"

    in_block = False
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.upper().startswith("REM "):
            continue
        if not in_block and s.endswith("("):
            in_block = True
            continue
        if in_block:
            if s == ")":
                in_block = False
                continue
            in_quote = False
            for ch in s:
                if ch == '"':
                    in_quote = not in_quote
                elif not in_quote and ch in "()":
                    pytest.fail(
                        f"install.cmd:{lineno} has unquoted {ch!r} inside a (...) block: {s}"
                    )
    assert not in_block, "Unclosed '(' block in install.cmd"

