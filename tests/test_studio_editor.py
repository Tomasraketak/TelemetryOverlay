"""Tests for the interactive Visual Studio Widget Editor (studio_editor.py & studio.py)."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from telemetry_overlay.studio import create_studio_server
from telemetry_overlay.studio_editor import WIDGET_CATALOG, apply_widget_patch
from telemetry_overlay.widgets import WIDGET_TYPES

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_widget_catalog_covers_all_registered_widgets():
    catalog_types = {item["type"] for item in WIDGET_CATALOG}
    assert catalog_types == set(WIDGET_TYPES)
    for item in WIDGET_CATALOG:
        assert item["label"]
        assert item["category"]
        assert item["resize_mode"] in ("box", "text")


def test_apply_widget_patch_handles_graph_proxy_and_text_scaling():
    graph_spec = {
        "type": "graph",
        "name": "g1",
        "series": [{"channel": "thrust", "fill": True}],
    }
    patched_g = apply_widget_patch(
        graph_spec,
        patch={"series_channel": "altitude", "series_fill": False, "series_color": "#38bdf8"},
    )
    assert patched_g["series"][0]["channel"] == "altitude"
    assert patched_g["series"][0]["fill"] is False
    assert patched_g["series"][0]["color"] == "#38bdf8"

    readout_spec = {"type": "readout", "channel": "thrust", "value_size": 80, "label_size": 20}
    scaled_r = apply_widget_patch(readout_spec, patch={"x": 192, "y": 270}, resize_ratio=1.5)
    assert scaled_r["x"] == "10.00%" and scaled_r["y"] == "25.00%"
    assert scaled_r["value_size"] == 120
    assert scaled_r["label_size"] == 30

    label_spec = {"type": "label", "text": "HI", "size": 40}
    scaled_l = apply_widget_patch(label_spec, resize_ratio=0.5)
    assert scaled_l["size"] == 20


def test_studio_visual_editor_full_crud_and_live_manipulation():
    server = create_studio_server(EXAMPLES / "rocket_flight.csv", host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1) HTML includes visual editor stage, gallery modal, layers, and inspector
        html = urllib.request.urlopen(f"{base}/").read().decode("utf-8")
        for token in ("editorLayer", "galleryModal", "layerList", "inspector", "undoBtn", "snapToggleBtn"):
            assert token in html

        # 2) Initial state returns widgets with exact design-canvas rects & catalog
        state = json.loads(urllib.request.urlopen(f"{base}/api/state?t=2.1").read().decode("utf-8"))
        assert state["design_width"] == 1920
        assert state["design_height"] == 1080
        assert len(state["widget_catalog"]) == len(WIDGET_TYPES)
        assert len(state["widgets"]) >= 4
        for w in state["widgets"]:
            if w["active"]:
                assert w["rect"] is not None
                assert w["rect"]["w"] >= 40 and w["rect"]["h"] >= 28
                assert w["origin"] is not None

        # 3) Clear all widgets and build a custom layout from scratch
        code, cleared = _post_json(f"{base}/api/widgets/clear", {"t": 2.1})
        assert code == 200
        assert cleared["widgets"] == []

        # 4) Add a wide set of widgets via /api/widgets/add and verify each renders cleanly
        to_add = [
            "motor_burn", "rocket_3d", "readout", "label", "timer",
            "badge", "led", "table", "graph", "sparkline", "gauge",
            "bar", "progress", "tape", "attitude", "compass", "lean",
            "gforce", "elevation", "image",
        ]
        for wtype in to_add:
            code, res = _post_json(f"{base}/api/widgets/add", {"type": wtype, "t": 2.1})
            assert code == 200, f"failed to add {wtype}: {res}"
            idx = res["selected_index"]
            added = res["widgets"][idx]
            assert added["type"] == wtype
            assert added["active"] is True
            assert added["rect"]["w"] >= 40 and added["rect"]["h"] >= 28

        # Render composite frame containing all 20 freshly added widgets!
        png_before = urllib.request.urlopen(f"{base}/api/frame?t=2.1").read()
        assert png_before[:8] == b"\x89PNG\r\n\x1a\n"

        # 5) Move & resize a box widget (rocket_3d at index 1) + tweak 3D camera
        code, moved = _post_json(f"{base}/api/widgets/update", {
            "index": 1,
            "patch": {
                "x": 120, "y": 90, "width": 420, "height": 480,
                "anchor": "top-left", "camera_elevation": 42, "camera_azimuth": -45,
                "nose_color": "#38bdf8",
            },
            "t": 2.1,
        })
        assert code == 200
        r3d = moved["widgets"][1]
        assert abs(r3d["rect"]["x"] - 120) <= 2
        assert abs(r3d["rect"]["y"] - 90) <= 2
        assert abs(r3d["rect"]["w"] - 420) <= 2
        assert abs(r3d["rect"]["h"] - 480) <= 2
        assert r3d["spec"]["camera_elevation"] == 42

        # 6) Resize a text widget (label at index 3) via resize_ratio
        old_size = moved["widgets"][3]["spec"]["size"]
        code, resized = _post_json(f"{base}/api/widgets/update", {
            "index": 3,
            "patch": {"x": 500, "y": 100, "text": "CUSTOM ROCKET"},
            "resize_ratio": 1.5,
            "t": 2.1,
        })
        assert code == 200
        assert resized["widgets"][3]["spec"]["size"] == int(round(old_size * 1.5))
        assert resized["widgets"][3]["spec"]["text"] == "CUSTOM ROCKET"

        # 7) Duplicate, reorder, and delete widgets
        count_before = len(resized["widgets"])
        code, duped = _post_json(f"{base}/api/widgets/duplicate", {"index": 0, "t": 2.1})
        assert code == 200
        assert len(duped["widgets"]) == count_before + 1
        assert duped["selected_index"] == 1
        assert duped["widgets"][1]["name"] != duped["widgets"][0]["name"]

        moved_name = duped["widgets"][1]["name"]
        code, reordered = _post_json(f"{base}/api/widgets/reorder", {
            "from_index": 1, "to_index": 4, "t": 2.1,
        })
        assert code == 200
        assert reordered["widgets"][4]["name"] == moved_name

        code, deleted = _post_json(f"{base}/api/widgets/delete", {"index": 4, "t": 2.1})
        assert code == 200
        assert len(deleted["widgets"]) == count_before

        # 8) Upload a custom PNG asset and attach it to the image widget
        img_buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 100, 0, 255)).save(img_buf, "PNG")
        req_asset = urllib.request.Request(
            f"{base}/api/upload_asset?filename=team_logo.png",
            data=img_buf.getvalue(),
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        asset_res = json.loads(urllib.request.urlopen(req_asset).read().decode("utf-8"))
        assert Path(asset_res["path"]).is_file()

        img_idx = next(w["index"] for w in deleted["widgets"] if w["type"] == "image")
        code, with_logo = _post_json(f"{base}/api/widgets/update", {
            "index": img_idx, "patch": {"path": asset_res["path"]}, "t": 2.1,
        })
        assert code == 200
        assert with_logo["widgets"][img_idx]["spec"]["path"] == asset_res["path"]

        # 9) Invalid patch is rejected cleanly without corrupting server state
        code, err = _post_json(f"{base}/api/widgets/update", {
            "index": 0, "patch": {"channel": "nonexistent_channel_xyz"}, "t": 2.1,
        })
        assert code == 400
        assert "error" in err
        after_err = json.loads(urllib.request.urlopen(f"{base}/api/state?t=2.1").read().decode("utf-8"))
        assert len(after_err["widgets"]) == count_before

        # Frame actually changed after edits
        png_after = urllib.request.urlopen(f"{base}/api/frame?t=2.1").read()
        assert png_after[:8] == b"\x89PNG\r\n\x1a\n"
        assert png_after != png_before
    finally:
        server.shutdown()
        server.server_close()


def test_studio_editor_adds_gps_map_on_gpx_sample():
    server = create_studio_server(EXAMPLES / "circuit_lap.gpx", host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        code, cleared = _post_json(f"{base}/api/widgets/clear", {})
        assert code == 200

        code, res = _post_json(f"{base}/api/widgets/add", {"type": "map", "t": 30.0})
        assert code == 200
        assert res["widgets"][0]["type"] == "map"
        assert res["widgets"][0]["active"] is True
        assert res["widgets"][0]["rect"]["w"] >= 200
    finally:
        server.shutdown()
        server.server_close()
