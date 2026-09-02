"""Rendering: transparency, widget behaviour and config validation."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from telemetry_overlay.config import load_config, parse_resolution, suggest_config
from telemetry_overlay.data import load
from telemetry_overlay.render import build_renderer
from telemetry_overlay.theme import Rect, anchor_offset, parse_color, resolve_length

from conftest import CSV_SAMPLE, PRESETS


def _config(widgets, **time):
    from telemetry_overlay.config import Config
    from telemetry_overlay.theme import THEMES
    return Config(width=640, height=360, fps=30, widgets=widgets,
                  theme=THEMES["night"], events=False, **time)


def _render(widgets, at=2.304, **kw):
    telemetry = load(CSV_SAMPLE)
    renderer = build_renderer(_config(widgets, **kw), telemetry=telemetry)
    return renderer.render_at(at)


def alpha_of(image: Image.Image) -> np.ndarray:
    return np.array(image.getchannel("A"))


# -- transparency ---------------------------------------------------------

def test_empty_overlay_is_fully_transparent():
    image = _render([])
    assert image.mode == "RGBA"
    assert alpha_of(image).max() == 0


def test_widgets_paint_only_where_they_are():
    image = _render([{"type": "label", "text": "HELLO", "x": 20, "y": 20,
                      "size": 30}])
    alpha = alpha_of(image)
    assert alpha.max() == 255           # the text is opaque
    assert (alpha == 0).mean() > 0.8    # most of the frame stays clear


def test_background_option_fills_the_frame():
    from telemetry_overlay.config import Config
    from telemetry_overlay.theme import THEMES
    config = Config(width=320, height=180, fps=30, widgets=[],
                    theme=THEMES["night"], events=False, background="#000000")
    renderer = build_renderer(config, telemetry=load(CSV_SAMPLE))
    assert alpha_of(renderer.render_at(2.0)).min() == 255


# -- widgets --------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    {"type": "label", "text": "T", "x": 10, "y": 10},
    {"type": "readout", "channel": "thrust", "x": 10, "y": 10},
    {"type": "timer", "x": 10, "y": 10},
    {"type": "graph", "channel": "thrust", "x": 10, "y": 10,
     "width": 300, "height": 150},
    {"type": "bar", "channel": "thrust", "x": 10, "y": 10, "width": 200},
    {"type": "gauge", "channel": "thrust", "x": 10, "y": 10,
     "width": 150, "height": 150},
    {"type": "led", "channel": "pyro_active", "x": 10, "y": 10},
    {"type": "table", "rows": ["thrust"], "x": 10, "y": 10},
])
def test_every_widget_type_draws_something(spec):
    assert alpha_of(_render([spec])).max() > 0


def test_readout_shows_the_value_at_that_moment():
    """The peak frame and an idle frame must not look the same."""
    spec = [{"type": "readout", "channel": "thrust", "x": 10, "y": 10,
             "value_size": 40}]
    peak = np.array(_render(spec, at=2.304))
    idle = np.array(_render(spec, at=0.5))
    assert not np.array_equal(peak, idle)


def test_graph_reveals_progressively():
    """More of the curve is drawn later in the burn than at the start."""
    spec = [{"type": "graph", "channel": "thrust", "x": 5, "y": 5,
             "width": 600, "height": 300, "panel": False, "ghost": False,
             "grid": False, "axis_labels": False, "dot": False,
             "playhead": False}]
    early = alpha_of(_render(spec, at=0.0)).astype(bool).sum()
    late = alpha_of(_render(spec, at=5.0)).astype(bool).sum()
    assert late > early * 2


def test_led_reflects_the_boolean_state():
    spec = [{"type": "led", "channel": "pyro_active", "x": 10, "y": 10,
             "label": "PYRO"}]
    on = np.array(_render(spec, at=0.5))     # pyro high
    off = np.array(_render(spec, at=3.0))    # pyro low
    assert not np.array_equal(on, off)


def test_opacity_scales_the_whole_widget():
    base = [{"type": "label", "text": "HELLO", "x": 20, "y": 20, "size": 40}]
    faded = [{**base[0], "opacity": 0.5}]
    assert alpha_of(_render(faded)).max() < alpha_of(_render(base)).max()


def test_show_window_hides_a_widget_outside_its_range():
    spec = [{"type": "label", "text": "LATE", "x": 20, "y": 20, "size": 40,
             "show_from": 4.0}]
    assert alpha_of(_render(spec, at=1.0)).max() == 0
    assert alpha_of(_render(spec, at=5.0)).max() > 0


def test_widgets_render_at_any_resolution():
    from telemetry_overlay.config import Config
    from telemetry_overlay.theme import THEMES
    telemetry = load(CSV_SAMPLE)
    widgets = [{"type": "readout", "channel": "thrust", "x": "5%", "y": "10%"}]
    for width, height in ((640, 360), (1920, 1080), (3840, 2160)):
        config = Config(width=width, height=height, fps=30, widgets=widgets,
                        theme=THEMES["night"], events=False)
        image = build_renderer(config, telemetry=telemetry).render_at(2.3)
        assert image.size == (width, height)
        assert alpha_of(image).max() > 0


# -- error reporting ------------------------------------------------------

def test_unknown_channel_names_the_alternatives():
    with pytest.raises(ValueError, match="unknown channel"):
        _render([{"type": "readout", "channel": "nope", "x": 0, "y": 0}])


def test_unknown_widget_type_lists_the_valid_ones():
    with pytest.raises(ValueError, match="unknown widget type"):
        _render([{"type": "hologram", "x": 0, "y": 0}])


def test_widget_without_a_type_is_rejected():
    with pytest.raises(ValueError, match="missing a 'type'"):
        _render([{"x": 0, "y": 0}])


def test_bad_anchor_is_rejected():
    with pytest.raises(ValueError, match="anchor"):
        _render([{"type": "label", "text": "x", "anchor": "sideways"}])


# -- config ---------------------------------------------------------------

@pytest.mark.parametrize("preset", ["rocket_thrust", "minimal", "hud",
                                    "broadcast"])
def test_every_bundled_preset_renders(preset):
    config = load_config(PRESETS / f"{preset}.yaml")
    config.width, config.height = 640, 360
    renderer = build_renderer(config, telemetry=load(CSV_SAMPLE))
    assert renderer.frame_count > 0
    assert alpha_of(renderer.render_frame(renderer.frame_count // 2)).max() > 0


def test_suggested_config_renders_without_editing():
    telemetry = load(CSV_SAMPLE)
    raw = suggest_config(telemetry, resolution="720p")
    import json
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_config(path)
    renderer = build_renderer(config, telemetry=telemetry)
    assert alpha_of(renderer.render_frame(renderer.frame_count // 2)).max() > 0


def test_unknown_config_section_is_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"widgets": [], "wigets": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config section"):
        load_config(path)


def test_cli_overrides_beat_the_config_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"output": {"resolution": "1080p", "fps": 30}}',
                    encoding="utf-8")
    config = load_config(path, {"resolution": "720p", "fps": 60})
    assert (config.width, config.height) == (1280, 720)
    assert config.fps == 60


@pytest.mark.parametrize("value,expected", [
    ("4k", (3840, 2160)), ("1080p", (1920, 1080)),
    ("1920x1080", (1920, 1080)), ("800X600", (800, 600)), ([64, 32], (64, 32)),
])
def test_resolution_forms(value, expected):
    assert parse_resolution(value) == expected


def test_bad_resolution_is_rejected():
    with pytest.raises(ValueError, match="unrecognised resolution"):
        parse_resolution("enormous")


# -- helpers --------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("#fff", (255, 255, 255, 255)),
    ("#ff9f0a", (255, 159, 10, 255)),
    ("#ff9f0a80", (255, 159, 10, 128)),
    ("white @ 50%", (255, 255, 255, 128)),
    ("red", (255, 59, 48, 255)),
    ([1, 2, 3], (1, 2, 3, 255)),
])
def test_colour_forms(value, expected):
    assert parse_color(value) == expected


def test_bad_colour_is_rejected():
    with pytest.raises(ValueError, match="unrecognised colour"):
        parse_color("chartreusey")


@pytest.mark.parametrize("value,expected", [
    (120, 120.0), ("8%", 153.6), ("64px", 64.0), (0.05, 96.0),
])
def test_length_forms(value, expected):
    assert resolve_length(value, 1920) == pytest.approx(expected)


def test_anchor_positions():
    assert anchor_offset("top-left", 100, 50) == (0.0, 0.0)
    assert anchor_offset("bottom-right", 100, 50) == (-100, -50)
    assert anchor_offset("middle-center", 100, 50) == (-50.0, -25.0)


def test_frames_are_deterministic():
    """The same frame index must render identically every time."""
    spec = [{"type": "graph", "channel": "thrust", "x": 5, "y": 5,
             "width": 400, "height": 200},
            {"type": "readout", "channel": "thrust", "x": 420, "y": 20}]
    first = np.array(_render(spec, at=2.5))
    second = np.array(_render(spec, at=2.5))
    assert np.array_equal(first, second)


# -- channel aliases and optional widgets ---------------------------------

def test_channel_alias_picks_the_first_present():
    """One config must cover loggers that spell a channel differently."""
    spec = [{"type": "readout", "channel": ["nope", "thrust", "also_nope"],
             "x": 10, "y": 10}]
    assert alpha_of(_render(spec)).max() > 0


def test_alias_that_matches_nothing_is_still_an_error():
    with pytest.raises(ValueError, match="unknown channel"):
        _render([{"type": "readout", "channel": ["nope", "nope2"],
                  "x": 0, "y": 0}])


def test_optional_widget_is_dropped_when_its_channel_is_missing():
    telemetry = load(CSV_SAMPLE)
    widgets = [
        {"type": "readout", "channel": "thrust", "x": 10, "y": 10},
        {"type": "led", "channel": "chamber_pressure", "optional": True,
         "x": 10, "y": 60},
    ]
    renderer = build_renderer(_config(widgets), telemetry=telemetry)
    assert len(renderer.widgets) == 1


def test_optional_does_not_hide_other_mistakes():
    """'optional' excuses a missing channel, not a bad widget type."""
    with pytest.raises(ValueError, match="unknown widget type"):
        _render([{"type": "nonsense", "channel": "thrust", "optional": True}])


def test_rocket_preset_runs_on_both_sample_files():
    """The bundled preset must not be tied to one logger's column names."""
    from conftest import XLSX_SAMPLE
    config = load_config(PRESETS / "rocket_thrust.yaml")
    config.width, config.height = 480, 270
    for sample in (CSV_SAMPLE, XLSX_SAMPLE):
        renderer = build_renderer(config, telemetry=load(sample))
        assert alpha_of(renderer.render_frame(renderer.frame_count // 2)).max() > 0
