"""Interactive visual widget editor helpers for Telemetry Overlay Studio.

Provides:
- Complete catalog and inspector field schemas for all 24 widget types
- Smart telemetry-aware default widget creation (``default_widget_spec``)
- Pixel-accurate bounding-box measurement on the design canvas (``measure_widget_rects``)
- Safe widget spec patching, font-aware resizing, duplication, and Z-order management
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from PIL import Image

from .config import DESIGN_HEIGHT, DESIGN_WIDTH
from .data import Telemetry
from .render import OverlayRenderer
from .widgets import WIDGET_ALIASES

__all__ = [
    "WIDGET_CATALOG",
    "TEXT_SCALED_WIDGETS",
    "widget_catalog_for_telemetry",
    "default_widget_spec",
    "measure_widget_rects",
    "apply_widget_patch",
    "unique_widget_name",
    "canonical_widget_type",
]

# Text/badge/lamp widgets whose visual size is governed by font/radius rather than box WxH
TEXT_SCALED_WIDGETS = frozenset({"label", "readout", "timer", "badge", "led"})


def canonical_widget_type(raw_type: str) -> str:
    t = str(raw_type).strip().lower()
    return WIDGET_ALIASES.get(t, t)


# ---------------------------------------------------------------------------
# Declarative Widget Catalog + Inspector Schema (all 24 widgets)
# ---------------------------------------------------------------------------

WIDGET_CATALOG: list[dict[str, Any]] = [
    # --- Raketové speciály ---
    {
        "type": "motor_burn",
        "category": "Raketové speciály",
        "label": "Raketový motor",
        "short": "Křivka tahu, tah (N/kgf), peak, impuls, třída NAR/TRA, doba hoření a tryska",
        "icon": "🔥",
        "resize_mode": "box",
        "fields": [
            {"key": "title", "label": "Nadpis", "kind": "text"},
            {"key": "channel", "label": "Kanál tahu", "kind": "channel"},
            {"key": "zoom_burn", "label": "Přiblížit na hoření", "kind": "bool", "default": False},
            {"key": "show_nozzle", "label": "Zobrazit trysku s plamenem", "kind": "bool", "default": True},
            {"key": "show_kgf", "label": "Zobrazit kgf", "kind": "bool", "default": True},
            {"key": "show_events", "label": "Značky IGN / PEAK / BO", "kind": "bool", "default": True},
            {"key": "threshold_fraction", "label": "Práh zážehu (0.01–0.3)", "kind": "range", "min": 0.01, "max": 0.3, "step": 0.01, "default": 0.05},
            {"key": "color", "label": "Barva křivky", "kind": "color"},
        ],
    },
    {
        "type": "rocket_3d",
        "category": "Raketové speciály",
        "label": "3D náklon rakety",
        "short": "Perspektivní 3D model rakety, horizont, svislice UP, oblouk náklonu a 3D plamen",
        "icon": "🚀",
        "resize_mode": "box",
        "fields": [
            {"key": "title", "label": "Nadpis", "kind": "text"},
            {"key": "pitch_channel", "label": "Kanál Pitch", "kind": "opt_channel"},
            {"key": "roll_channel", "label": "Kanál Roll", "kind": "opt_channel"},
            {"key": "yaw_channel", "label": "Kanál Yaw", "kind": "opt_channel"},
            {"key": "thrust_channel", "label": "Kanál tahu (3D plamen)", "kind": "opt_channel"},
            {"key": "camera_elevation", "label": "Elevace kamery (°)", "kind": "range", "min": -10, "max": 75, "step": 1, "default": 22},
            {"key": "camera_azimuth", "label": "Azimut kamery (°)", "kind": "range", "min": -180, "max": 180, "step": 2, "default": 30},
            {"key": "show_grid", "label": "3D horizont", "kind": "bool", "default": True},
            {"key": "show_zenith", "label": "Svislá osa UP", "kind": "bool", "default": True},
            {"key": "show_tilt_arc", "label": "Oblouk náklonu", "kind": "bool", "default": True},
            {"key": "show_angles", "label": "Hodnoty P / R / Y", "kind": "bool", "default": True},
            {"key": "nose_color", "label": "Barva špičky", "kind": "color"},
            {"key": "body_color", "label": "Barva trupu", "kind": "color"},
            {"key": "fin_color", "label": "Barva křidélek", "kind": "color"},
        ],
    },

    # --- Text a hodnoty ---
    {
        "type": "readout",
        "category": "Text a hodnoty",
        "label": "Velké číslo (Readout)",
        "short": "Živá hodnota libovolného kanálu s popiskem, jednotkou a volitelným maximem",
        "icon": "🔢",
        "resize_mode": "text",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "unit", "label": "Jednotka", "kind": "text"},
            {"key": "format", "label": "Formát (např. {:.1f})", "kind": "text", "default": "{:.1f}"},
            {"key": "value_size", "label": "Velikost čísla", "kind": "range", "min": 18, "max": 180, "step": 2, "default": 72},
            {"key": "label_size", "label": "Velikost popisku", "kind": "range", "min": 10, "max": 60, "step": 1, "default": 20},
            {"key": "peak_hold", "label": "Držet maximum (Peak)", "kind": "bool", "default": False},
            {"key": "color", "label": "Barva hodnoty", "kind": "color"},
        ],
    },
    {
        "type": "label",
        "category": "Text a hodnoty",
        "label": "Text / Nadpis",
        "short": "Statický text nebo šablona ({time:.2f}, {speed:.2f}x, {meta.*})",
        "icon": "Aa",
        "resize_mode": "text",
        "fields": [
            {"key": "text", "label": "Text", "kind": "text", "default": "TELEMETRY"},
            {"key": "size", "label": "Velikost písma", "kind": "range", "min": 12, "max": 160, "step": 2, "default": 36},
            {"key": "tracking", "label": "Rozestup písmen", "kind": "range", "min": 0, "max": 20, "step": 1, "default": 2},
            {"key": "uppercase", "label": "VELKÁ PÍSMENA", "kind": "bool", "default": False},
            {"key": "align", "label": "Zarovnání", "kind": "select", "options": ["left", "center", "right"], "default": "left"},
            {"key": "color", "label": "Barva textu", "kind": "color"},
        ],
    },
    {
        "type": "timer",
        "category": "Text a hodnoty",
        "label": "Časovač / Hodiny",
        "short": "Misijní čas (T+0.00) nebo reálný UTC čas s ukazatelem zpomalení",
        "icon": "⏱",
        "resize_mode": "text",
        "fields": [
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "style", "label": "Styl", "kind": "select", "options": ["t+", "t+mmss", "mmss", "hhmmss", "clock", "utc", "plain"], "default": "t+"},
            {"key": "decimals", "label": "Desetinná místa", "kind": "range", "min": 0, "max": 3, "step": 1, "default": 2},
            {"key": "size", "label": "Velikost písma", "kind": "range", "min": 16, "max": 140, "step": 2, "default": 48},
            {"key": "show_speed", "label": "Ukázat rychlost (slow-mo)", "kind": "bool", "default": False},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "badge",
        "category": "Text a hodnoty",
        "label": "Stavový štítek (Badge)",
        "short": "Barevná pilulka pro fázi letu, stupeň nebo stavový textový kanál",
        "icon": "🏷",
        "resize_mode": "text",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "prefix", "label": "Prefix", "kind": "text"},
            {"key": "size", "label": "Velikost písma", "kind": "range", "min": 12, "max": 72, "step": 1, "default": 22},
            {"key": "uppercase", "label": "VELKÁ PÍSMENA", "kind": "bool", "default": True},
            {"key": "color", "label": "Barva štítku", "kind": "color"},
        ],
    },
    {
        "type": "led",
        "category": "Text a hodnoty",
        "label": "Kontrolka (LED)",
        "short": "Svítící indikátor pro pyro, zážeh nebo překročení prahu",
        "icon": "💡",
        "resize_mode": "text",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "threshold", "label": "Práh zapnutí", "kind": "number", "default": 0.5},
            {"key": "on_text", "label": "Text ZAP", "kind": "text"},
            {"key": "off_text", "label": "Text VYP", "kind": "text"},
            {"key": "radius", "label": "Poloměr LED", "kind": "range", "min": 6, "max": 48, "step": 1, "default": 13},
            {"key": "invert", "label": "Invertovat logiku", "kind": "bool", "default": False},
            {"key": "on_color", "label": "Barva ZAP", "kind": "color"},
            {"key": "off_color", "label": "Barva VYP", "kind": "color"},
        ],
    },
    {
        "type": "table",
        "category": "Text a hodnoty",
        "label": "Tabulka kanálů",
        "short": "Více veličin přehledně pod sebou v jednom kompaktním panelu",
        "icon": "📋",
        "resize_mode": "box",
        "fields": [
            {"key": "title", "label": "Nadpis", "kind": "text"},
            {"key": "font_size", "label": "Velikost písma", "kind": "range", "min": 12, "max": 48, "step": 1, "default": 22},
            {"key": "row_gap", "label": "Mezera řádků", "kind": "range", "min": 4, "max": 36, "step": 1, "default": 12},
        ],
    },

    # --- Grafy a budíky ---
    {
        "type": "graph",
        "category": "Grafy a budíky",
        "label": "Průběhový graf",
        "short": "Živý graf s gradientní výplní, značkami událostí a posuvným oknem",
        "icon": "📈",
        "resize_mode": "box",
        "fields": [
            {"key": "title", "label": "Nadpis grafu", "kind": "text"},
            {"key": "series_channel", "label": "Hlavní kanál", "kind": "channel"},
            {"key": "series_color", "label": "Barva křivky", "kind": "color"},
            {"key": "series_fill", "label": "Výplň pod křivkou", "kind": "bool", "default": True},
            {"key": "mode", "label": "Režim", "kind": "select", "options": ["full", "window"], "default": "full"},
            {"key": "window", "label": "Okno v režimu window (s)", "kind": "number", "default": 5.0},
            {"key": "line_width", "label": "Tloušťka čáry", "kind": "range", "min": 1, "max": 10, "step": 0.5, "default": 3.0},
            {"key": "gradient", "label": "Gradientní výplň", "kind": "bool", "default": True},
            {"key": "glow", "label": "Záře křivky", "kind": "bool", "default": True},
            {"key": "show_value", "label": "Živá hodnota u kurzoru", "kind": "bool", "default": True},
            {"key": "legend", "label": "Legenda", "kind": "bool", "default": False},
            {"key": "events", "label": "Značky událostí", "kind": "bool", "default": True},
        ],
    },
    {
        "type": "sparkline",
        "category": "Grafy a budíky",
        "label": "KPI karta (Sparkline)",
        "short": "Kompaktní televizní karta s číslem, šipkou trendu a minigrafem",
        "icon": "⚡",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "format", "label": "Formát", "kind": "text", "default": "{:.1f}"},
            {"key": "window", "label": "Časové okno (s)", "kind": "number", "default": 15.0},
            {"key": "fill", "label": "Výplň minigrafu", "kind": "bool", "default": True},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "gauge",
        "category": "Grafy a budíky",
        "label": "Kruhový budík (Gauge)",
        "short": "Rychloměr / tahoměr / otáčkoměr se stupnicí, peak značkou a shift-lights",
        "icon": "🎛",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "min", "label": "Minimum", "kind": "number"},
            {"key": "max", "label": "Maximum", "kind": "number"},
            {"key": "format", "label": "Formát", "kind": "text", "default": "{:.0f}"},
            {"key": "show_scale", "label": "Číselná stupnice", "kind": "bool", "default": True},
            {"key": "peak_marker", "label": "Peak značka", "kind": "bool", "default": True},
            {"key": "shift_lights", "label": "Závodní Shift-Lights", "kind": "bool", "default": False},
            {"key": "color", "label": "Barva oblouku", "kind": "color"},
        ],
    },
    {
        "type": "bar",
        "category": "Grafy a budíky",
        "label": "Sloupcový ukazatel (Bar)",
        "short": "Horizontální nebo vertikální sloupec (i obousměrný od nuly)",
        "icon": "▮",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "orientation", "label": "Orientace", "kind": "select", "options": ["horizontal", "vertical"], "default": "horizontal"},
            {"key": "min", "label": "Minimum", "kind": "number"},
            {"key": "max", "label": "Maximum", "kind": "number"},
            {"key": "thickness", "label": "Tloušťka sloupce", "kind": "range", "min": 6, "max": 80, "step": 2, "default": 20},
            {"key": "bidirectional", "label": "Obousměrný od nuly", "kind": "bool", "default": False},
            {"key": "show_label", "label": "Zobrazit popisek", "kind": "bool", "default": True},
            {"key": "show_value", "label": "Zobrazit číslo", "kind": "bool", "default": True},
            {"key": "peak_marker", "label": "Peak značka", "kind": "bool", "default": False},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "progress",
        "category": "Grafy a budíky",
        "label": "Časová lišta (Progress)",
        "short": "Průběh klipu se značkami událostí a indikátorem zpomalení",
        "icon": "▬",
        "resize_mode": "box",
        "fields": [
            {"key": "show_events", "label": "Značky událostí", "kind": "bool", "default": True},
            {"key": "speed_badge", "label": "Štítek SLOW-MO", "kind": "bool", "default": True},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },

    # --- Prostorové, GPS, Letectví & Motorsport ---
    {
        "type": "tape",
        "category": "Prostorové a GPS",
        "label": "Letový žebřík (Tape)",
        "short": "Posuvná vertikální stupnice pro výšku, rychlost nebo Mach",
        "icon": "📏",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál", "kind": "channel"},
            {"key": "label", "label": "Popisek", "kind": "text"},
            {"key": "side", "label": "Strana stupnice", "kind": "select", "options": ["right", "left"], "default": "right"},
            {"key": "window", "label": "Rozsah okna", "kind": "number"},
            {"key": "step", "label": "Krok dílků", "kind": "number"},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "attitude",
        "category": "Prostorové a GPS",
        "label": "Umělý horizont (PFD)",
        "short": "Letecký indikátor sklonu (pitch) a náklonu (roll)",
        "icon": "✈",
        "resize_mode": "box",
        "fields": [
            {"key": "pitch_channel", "label": "Kanál Pitch", "kind": "channel"},
            {"key": "roll_channel", "label": "Kanál Roll", "kind": "channel"},
            {"key": "pitch_range", "label": "Rozsah pitch (°)", "kind": "range", "min": 15, "max": 90, "step": 5, "default": 45},
            {"key": "sky_color", "label": "Barva nebe", "kind": "color"},
            {"key": "ground_color", "label": "Barva země", "kind": "color"},
        ],
    },
    {
        "type": "compass",
        "category": "Prostorové a GPS",
        "label": "Kompas / Kurz",
        "short": "Posuvná kurzová páska nebo otočná kompasová růžice",
        "icon": "🧭",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál azimutu", "kind": "channel"},
            {"key": "style", "label": "Styl", "kind": "select", "options": ["tape", "rose"], "default": "tape"},
            {"key": "fov", "label": "Zorné pole (°)", "kind": "range", "min": 60, "max": 240, "step": 10, "default": 120},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "lean",
        "category": "Prostorové a GPS",
        "label": "Úhel náklonu (Lean)",
        "short": "Symetrický ukazatel bočního náklonu s levým a pravým maximem",
        "icon": "∠",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál náklonu", "kind": "channel"},
            {"key": "max_angle", "label": "Max úhel (°)", "kind": "range", "min": 20, "max": 90, "step": 5, "default": 60},
            {"key": "peak_hold", "label": "Držet L/P maxima", "kind": "bool", "default": True},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "gforce",
        "category": "Prostorové a GPS",
        "label": "G-Force kruh",
        "short": "2D přetížení (příčné / podélné G) s mizící stopou",
        "icon": "◎",
        "resize_mode": "box",
        "fields": [
            {"key": "x_channel", "label": "Kanál X (příčné G)", "kind": "channel"},
            {"key": "y_channel", "label": "Kanál Y (podélné G)", "kind": "channel"},
            {"key": "max_g", "label": "Max G", "kind": "range", "min": 0.5, "max": 10, "step": 0.5, "default": 2.0},
            {"key": "trail", "label": "Délka stopy (s)", "kind": "range", "min": 0, "max": 5, "step": 0.2, "default": 1.2},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "map",
        "category": "Prostorové a GPS",
        "label": "GPS Mapa trasy",
        "short": "Animovaná GPS dráha s tepelnou mapou rychlosti/výšky a follow-zoomem",
        "icon": "🗺",
        "resize_mode": "box",
        "requires": "gps",
        "fields": [
            {"key": "title", "label": "Nadpis", "kind": "text"},
            {"key": "mode", "label": "Režim", "kind": "select", "options": ["full", "follow"], "default": "full"},
            {"key": "color_channel", "label": "Kanál heatmapy", "kind": "opt_channel"},
            {"key": "colormap", "label": "Barevná škála", "kind": "select", "options": ["speed", "thermal", "elevation", "ocean", "rdylgn", "cyber"], "default": "speed"},
            {"key": "scale_bar", "label": "Měřítko", "kind": "bool", "default": True},
            {"key": "north_arrow", "label": "Směrovka severu", "kind": "bool", "default": True},
        ],
    },
    {
        "type": "elevation",
        "category": "Prostorové a GPS",
        "label": "Výškový profil",
        "short": "Profil výšky s gradientem, sklonem % a celkovým převýšením",
        "icon": "⛰",
        "resize_mode": "box",
        "fields": [
            {"key": "channel", "label": "Kanál výšky", "kind": "channel"},
            {"key": "grade_channel", "label": "Kanál sklonu", "kind": "opt_channel"},
            {"key": "gain_channel", "label": "Kanál převýšení", "kind": "opt_channel"},
            {"key": "color", "label": "Barva", "kind": "color"},
        ],
    },
    {
        "type": "pedals",
        "category": "Prostorové a GPS",
        "label": "Pedály a volant",
        "short": "Plyn, brzda, spojka, otočný volant a zařazený rychlostní stupeň",
        "icon": "🏎",
        "resize_mode": "box",
        "fields": [
            {"key": "throttle_channel", "label": "Plyn", "kind": "opt_channel"},
            {"key": "brake_channel", "label": "Brzda", "kind": "opt_channel"},
            {"key": "steering_channel", "label": "Volant", "kind": "opt_channel"},
            {"key": "gear_channel", "label": "Stupeň", "kind": "opt_channel"},
        ],
    },
    {
        "type": "sticks",
        "category": "Prostorové a GPS",
        "label": "RC / FPV Kniply",
        "short": "Dvojice RC kniplů (Mode 2) s živou stopou pohybu",
        "icon": "🎮",
        "resize_mode": "box",
        "fields": [
            {"key": "left_x", "label": "Levý X (Yaw)", "kind": "opt_channel"},
            {"key": "left_y", "label": "Levý Y (Throttle)", "kind": "opt_channel"},
            {"key": "right_x", "label": "Pravý X (Roll)", "kind": "opt_channel"},
            {"key": "right_y", "label": "Pravý Y (Pitch)", "kind": "opt_channel"},
        ],
    },
    {
        "type": "lap_timer",
        "category": "Prostorové a GPS",
        "label": "Stopky kola (Lap)",
        "short": "Aktuální kolo, nejlepší čas kola a live delta na okruhu",
        "icon": "🏁",
        "resize_mode": "box",
        "fields": [
            {"key": "lap_channel", "label": "Kanál kola", "kind": "opt_channel"},
            {"key": "time_channel", "label": "Čas kola", "kind": "opt_channel"},
            {"key": "delta_channel", "label": "Delta", "kind": "opt_channel"},
        ],
    },
    {
        "type": "image",
        "category": "Prostorové a GPS",
        "label": "Obrázek / Logo",
        "short": "PNG logo nebo ikona otočná podle libovolného kanálu",
        "icon": "🖼",
        "resize_mode": "box",
        "fields": [
            {"key": "path", "label": "Cesta k obrázku", "kind": "text"},
            {"key": "rotate_channel", "label": "Kanál rotace (volitelné)", "kind": "opt_channel"},
            {"key": "rotate_scale", "label": "Násobič rotace", "kind": "number", "default": 1.0},
            {"key": "rotate_offset", "label": "Offset rotace (°)", "kind": "number", "default": 0.0},
        ],
    },
]


def _pick_channel(telemetry: Telemetry, candidates: list[str], *, allow_text: bool = False) -> str | None:
    """Pick the best existing channel from ``candidates``, falling back to first usable channel."""
    found = telemetry.resolve(candidates)
    if found is not None:
        return found
    nums = [k for k in telemetry.numeric_keys() if k != "time"]
    if nums:
        return nums[0]
    if allow_text and telemetry.channels:
        return next(iter(telemetry.channels))
    return None


def widget_catalog_for_telemetry(telemetry: Telemetry) -> list[dict[str, Any]]:
    """Return catalog metadata annotated with availability for the active dataset."""
    keys = set(telemetry.channels)
    has_gps = telemetry.has_gps
    has_pitch_roll = ("pitch" in keys and "roll" in keys) or all(
        k in keys for k in ("quat_w", "quat_x", "quat_y", "quat_z")
    )
    has_thrust = any(any(w in k for w in ("thrust", "force", "tah")) for k in keys)
    has_numeric = bool([k for k in telemetry.numeric_keys() if k != "time"])

    out: list[dict[str, Any]] = []
    for item in WIDGET_CATALOG:
        entry = copy.deepcopy(item)
        t = item["type"]
        missing: str | None = None
        recommended = False

        if t == "map" and not has_gps:
            missing = "Vyžaduje GPS (lat/lon)"
        elif t == "motor_burn":
            recommended = has_thrust
            if not has_numeric:
                missing = "Vyžaduje číselný kanál"
        elif t == "rocket_3d":
            recommended = has_pitch_roll
            if not has_numeric:
                missing = "Vyžaduje kanály náklonu"
        elif t in ("attitude", "gforce") and len([k for k in telemetry.numeric_keys() if k != "time"]) < 1:
            missing = "Vyžaduje číselné kanály"
        elif t == "pedals" and not (keys & {"throttle", "brake", "steering", "gear"}):
            missing = "Nemá kanály pedálů"
        elif t == "sticks" and not (keys & {"left_x", "left_y", "right_x", "right_y", "throttle", "yaw", "pitch", "roll"}):
            missing = "Nemá RC kanály"
        elif t == "lap_timer" and "lap" not in keys:
            missing = "Bez detekovaných kol"
        elif t not in ("label", "timer", "progress", "image") and not telemetry.channels:
            missing = "Žádné kanály"

        entry["available"] = missing is None
        entry["missing_reason"] = missing
        entry["recommended"] = recommended
        out.append(entry)
    return out


def unique_widget_name(base: str, existing_specs: list[dict[str, Any]]) -> str:
    used = {str(w.get("name", "")) for w in existing_specs if isinstance(w, dict)}
    if base not in used:
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"


def default_widget_spec(
    widget_type: str,
    telemetry: Telemetry,
    existing_specs: list[dict[str, Any]],
    *,
    design_w: int = DESIGN_WIDTH,
    design_h: int = DESIGN_HEIGHT,
    icon_path: Path | None = None,
) -> dict[str, Any]:
    """Create a smart, ready-to-render starter spec for ``widget_type``."""
    wtype = canonical_widget_type(widget_type)
    nums = [k for k in telemetry.numeric_keys() if k != "time"]
    primary = _pick_channel(telemetry, ["thrust", "tah", "force", "speed_kmh", "speed", "altitude", "power"]) or (nums[0] if nums else None)
    secondary = next((k for k in nums if k != primary), primary)

    # Stagger placement around upper-center so consecutive added widgets don't overlap
    n = len(existing_specs)
    ox = (n % 5) * 34
    oy = (n % 4) * 28

    def _px(v: float) -> str:
        return f"{v / max(1.0, float(design_w)) * 100.0:.2f}%"

    def _py(v: float) -> str:
        return f"{v / max(1.0, float(design_h)) * 100.0:.2f}%"

    def _box(w: int, h: int, cx: float = 0.5, cy: float = 0.45) -> dict[str, Any]:
        x = max(24.0, min(design_w - w - 24.0, design_w * cx - w / 2.0 + ox))
        y = max(24.0, min(design_h - h - 24.0, design_h * cy - h / 2.0 + oy))
        return {
            "x": _px(x), "y": _py(y),
            "width": _px(w), "height": _py(h),
            "anchor": "top-left",
        }

    name = unique_widget_name(wtype, existing_specs)

    if wtype == "motor_burn":
        ch = _pick_channel(telemetry, ["thrust", "tah", "force"])
        if ch is None:
            raise ValueError("Log nemá žádný číselný kanál pro motor_burn.")
        return {
            "type": "motor_burn", "name": name, "channel": ch,
            "title": "Rocket Motor", "show_nozzle": True, "show_kgf": True,
            "show_events": True, "zoom_burn": telemetry.duration > 10.0,
            **_box(780, 340, 0.30, 0.72),
        }

    if wtype == "rocket_3d":
        pitch_ch = _pick_channel(telemetry, ["pitch", "tilt", "angle"])
        roll_ch = _pick_channel(telemetry, ["roll", "bank"]) or pitch_ch
        yaw_ch = telemetry.resolve(["yaw", "heading", "bearing"])
        thrust_ch = telemetry.resolve(["thrust", "tah", "force"])
        spec: dict[str, Any] = {
            "type": "rocket_3d", "name": name, "title": "3D Attitude",
            "show_grid": True, "show_zenith": True, "show_tilt_arc": True, "show_angles": True,
            **_box(330, 380, 0.78, 0.30),
        }
        if not all(k in telemetry.channels for k in ("quat_w", "quat_x", "quat_y", "quat_z")):
            if pitch_ch:
                spec["pitch_channel"] = pitch_ch
            if roll_ch:
                spec["roll_channel"] = roll_ch
            if yaw_ch:
                spec["yaw_channel"] = yaw_ch
        if thrust_ch:
            spec["thrust_channel"] = thrust_ch
        return spec

    if wtype == "readout":
        if primary is None:
            raise ValueError("Log nemá žádný kanál pro readout.")
        ch_obj = telemetry[primary]
        return {
            "type": "readout", "name": name, "channel": primary,
            "label": ch_obj.label, "value_size": 72, "label_size": 20,
            "format": "{:.1f}", "panel": False,
            "x": _px(120 + ox), "y": _py(160 + oy), "anchor": "top-left",
        }

    if wtype == "label":
        return {
            "type": "label", "name": name, "text": "TELEMETRY OVERLAY",
            "size": 42, "tracking": 3, "uppercase": True, "panel": False,
            "x": _px(120 + ox), "y": _py(80 + oy), "anchor": "top-left",
        }

    if wtype == "timer":
        return {
            "type": "timer", "name": name, "label": "MISSION TIME",
            "style": "t+", "decimals": 2, "size": 48, "panel": False,
            "x": _px(120 + ox), "y": _py(140 + oy), "anchor": "top-left",
        }

    if wtype == "badge":
        ch = _pick_channel(telemetry, ["phase", "state", "mode", "gear"], allow_text=True)
        if ch is None:
            raise ValueError("Log nemá žádný kanál pro badge.")
        return {
            "type": "badge", "name": name, "channel": ch,
            "prefix": "", "size": 22, "uppercase": True,
            "x": _px(140 + ox), "y": _py(240 + oy), "anchor": "top-left",
        }

    if wtype == "led":
        booleans = [k for k, c in telemetry.channels.items() if c.is_boolean]
        ch = booleans[0] if booleans else primary
        if ch is None:
            raise ValueError("Log nemá žádný kanál pro led.")
        return {
            "type": "led", "name": name, "channel": ch,
            "label": telemetry[ch].label, "radius": 14, "panel": False,
            "x": _px(140 + ox), "y": _py(320 + oy), "anchor": "top-left",
        }

    if wtype == "table":
        pick = nums[:4]
        if not pick:
            raise ValueError("Log nemá žádné číselné kanály pro tabulku.")
        return {
            "type": "table", "name": name, "title": "TELEMETRY",
            "rows": [{"channel": k, "label": telemetry[k].label, "format": "{:.1f}"} for k in pick],
            **_box(360, max(160, 64 + len(pick) * 44), 0.22, 0.35),
        }

    if wtype == "graph":
        if primary is None:
            raise ValueError("Log nemá žádný kanál pro graf.")
        return {
            "type": "graph", "name": name,
            "title": telemetry[primary].label,
            "series": [{"channel": primary, "fill": True}],
            "mode": "full", "show_value": True, "gradient": True, "glow": True, "events": True,
            **_box(760, 320, 0.52, 0.72),
        }

    if wtype == "sparkline":
        if primary is None:
            raise ValueError("Log nemá žádný kanál pro sparkline.")
        return {
            "type": "sparkline", "name": name, "channel": primary,
            "label": telemetry[primary].label, "format": "{:.1f}", "fill": True,
            **_box(280, 145, 0.50, 0.80),
        }

    if wtype == "gauge":
        if primary is None:
            raise ValueError("Log nemá žádný kanál pro budík.")
        lo, hi = telemetry[primary].range()
        return {
            "type": "gauge", "name": name, "channel": primary,
            "label": telemetry[primary].label, "show_scale": True, "peak_marker": True,
            "min": round(min(0.0, lo), 2) if lo < 0 else 0.0,
            "max": round(max(hi * 1.05, 1.0), 2),
            "format": "{:.0f}",
            **_box(270, 270, 0.82, 0.72),
        }

    if wtype == "bar":
        if primary is None:
            raise ValueError("Log nemá žádný kanál pro bar.")
        return {
            "type": "bar", "name": name, "channel": primary,
            "label": telemetry[primary].label, "orientation": "horizontal",
            "show_label": True, "show_value": True, "thickness": 22,
            **_box(380, 115, 0.50, 0.50),
        }

    if wtype == "progress":
        return {
            "type": "progress", "name": name,
            "show_events": True, "speed_badge": True,
            "x": _px(76), "y": _py(design_h - 44),
            "width": _px(design_w - 152), "height": _py(12),
            "anchor": "top-left",
        }

    if wtype == "tape":
        ch = _pick_channel(telemetry, ["altitude", "speed_kmh", "speed", "velocity", "thrust"])
        if ch is None:
            raise ValueError("Log nemá žádný kanál pro letový žebřík.")
        return {
            "type": "tape", "name": name, "channel": ch,
            "label": telemetry[ch].label[:6].upper(), "side": "right",
            **_box(135, 360, 0.90, 0.34),
        }

    if wtype == "attitude":
        p_ch = _pick_channel(telemetry, ["pitch"])
        r_ch = _pick_channel(telemetry, ["roll"]) or secondary
        if p_ch is None or r_ch is None:
            raise ValueError("Log nemá kanály pro umělý horizont.")
        return {
            "type": "attitude", "name": name,
            "pitch_channel": p_ch, "roll_channel": r_ch,
            **_box(260, 260, 0.50, 0.45),
        }

    if wtype == "compass":
        ch = _pick_channel(telemetry, ["heading", "bearing", "yaw"])
        if ch is None:
            raise ValueError("Log nemá kanál pro kompas.")
        return {
            "type": "compass", "name": name, "channel": ch,
            "style": "tape", "fov": 120,
            **_box(460, 78, 0.50, 0.08),
        }

    if wtype == "lean":
        ch = _pick_channel(telemetry, ["lean_angle", "roll", "bank"])
        if ch is None:
            raise ValueError("Log nemá kanál pro úhel náklonu.")
        return {
            "type": "lean", "name": name, "channel": ch,
            "max_angle": 60, "peak_hold": True,
            **_box(260, 170, 0.50, 0.45),
        }

    if wtype == "gforce":
        x_ch = _pick_channel(telemetry, ["g_lat", "ax", "accl_x"])
        y_ch = _pick_channel(telemetry, ["g_lon", "ay", "accl_y"]) or secondary
        if x_ch is None or y_ch is None:
            raise ValueError("Log nemá kanály zrychlení pro G-force.")
        return {
            "type": "gforce", "name": name,
            "x_channel": x_ch, "y_channel": y_ch, "max_g": 2.0, "trail": 1.2,
            **_box(240, 250, 0.78, 0.72),
        }

    if wtype == "map":
        if not telemetry.has_gps:
            raise ValueError("Tento soubor neobsahuje GPS souřadnice (latitude / longitude).")
        spec = {
            "type": "map", "name": name, "title": "ROUTE",
            "mode": "full", "scale_bar": True, "north_arrow": True,
            **_box(320, 320, 0.16, 0.28),
        }
        col_ch = telemetry.resolve(["speed_kmh", "speed", "altitude"])
        if col_ch:
            spec["color_channel"] = col_ch
        return spec

    if wtype == "elevation":
        ch = _pick_channel(telemetry, ["altitude", "elevation", "ele"])
        if ch is None:
            raise ValueError("Log nemá výškový kanál.")
        return {
            "type": "elevation", "name": name, "channel": ch,
            **_box(720, 190, 0.30, 0.84),
        }

    if wtype == "pedals":
        spec = {"type": "pedals", "name": name, **_box(270, 155, 0.16, 0.84)}
        for k, cands in (
            ("throttle_channel", ["throttle"]),
            ("brake_channel", ["brake"]),
            ("steering_channel", ["steering"]),
            ("gear_channel", ["gear"]),
        ):
            found = telemetry.resolve(cands)
            if found:
                spec[k] = found
        if not any(k in spec for k in ("throttle_channel", "brake_channel", "steering_channel")):
            if primary:
                spec["throttle_channel"] = primary
        return spec

    if wtype == "sticks":
        spec = {"type": "sticks", "name": name, **_box(340, 180, 0.50, 0.82)}
        for k, cands in (
            ("left_x", ["left_x", "yaw", "rudder"]),
            ("left_y", ["left_y", "throttle"]),
            ("right_x", ["right_x", "roll", "aileron"]),
            ("right_y", ["right_y", "pitch", "elevator"]),
        ):
            found = telemetry.resolve(cands)
            if found:
                spec[k] = found
        return spec

    if wtype == "lap_timer":
        return {"type": "lap_timer", "name": name, **_box(320, 165, 0.82, 0.18)}

    if wtype == "image":
        if icon_path is None or not icon_path.is_file():
            raise ValueError("Zadejte cestu k PNG obrázku v konfiguraci widgetu.")
        return {
            "type": "image", "name": name, "path": str(icon_path),
            **_box(140, 140, 0.90, 0.12),
        }

    raise ValueError(f"Neznámý typ widgetu: {widget_type!r}")


# ---------------------------------------------------------------------------
# Pixel-accurate bounding box measurement for interactive canvas handles
# ---------------------------------------------------------------------------

def measure_widget_rects(
    renderer: OverlayRenderer,
    raw_specs: list[dict[str, Any]],
    data_t: float,
    design_w: float = DESIGN_WIDTH,
    design_h: float = DESIGN_HEIGHT,
) -> list[dict[str, Any]]:
    """Return UI metadata & design-space bounding boxes for every widget in ``raw_specs``."""
    by_spec_id = {id(w.spec): w for w in renderer.widgets}
    pw, ph = renderer.size
    sx = pw / max(1.0, design_w)
    sy = ph / max(1.0, design_h)

    results: list[dict[str, Any]] = []
    for idx, spec in enumerate(raw_specs):
        if not isinstance(spec, dict):
            continue
        wtype = canonical_widget_type(str(spec.get("type", "widget")))
        name = str(spec.get("name", wtype))
        visible = bool(spec.get("visible", True))
        widget = by_spec_id.get(id(spec))

        rect_dict: dict[str, float] | None = None
        active = widget is not None

        if widget is not None:
            r = widget.rect
            need_bbox = (
                wtype in TEXT_SCALED_WIDGETS
                or r.w <= 4.0
                or r.h <= 4.0
            )
            if need_bbox:
                # Render only this widget onto a blank preview canvas and grab exact pixel bbox
                probe = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
                orig_vis = widget.visible
                orig_op = widget.opacity
                orig_sf, orig_st = widget.show_from, widget.show_to
                try:
                    widget.visible = True
                    widget.opacity = 1.0
                    widget.show_from = None
                    widget.show_to = None
                    widget.draw_on(probe, data_t, frame=0)
                    bbox = probe.getbbox()
                except Exception:  # noqa: BLE001
                    bbox = None
                finally:
                    widget.visible = orig_vis
                    widget.opacity = orig_op
                    widget.show_from = orig_sf
                    widget.show_to = orig_st

                if bbox is not None:
                    pad = 6.0
                    bx0 = max(0.0, bbox[0] - pad) / sx
                    by0 = max(0.0, bbox[1] - pad) / sy
                    bx1 = min(float(pw), bbox[2] + pad) / sx
                    by1 = min(float(ph), bbox[3] + pad) / sy
                    rect_dict = {
                        "x": round(bx0, 1),
                        "y": round(by0, 1),
                        "w": round(max(40.0, bx1 - bx0), 1),
                        "h": round(max(28.0, by1 - by0), 1),
                    }

            if rect_dict is None:
                rx = r.x / sx
                ry = r.y / sy
                rw = max(40.0, r.w / sx) if r.w > 0 else 220.0
                rh = max(28.0, r.h / sy) if r.h > 0 else 70.0
                rect_dict = {
                    "x": round(rx, 1),
                    "y": round(ry, 1),
                    "w": round(rw, 1),
                    "h": round(rh, 1),
                }

        origin_dict: dict[str, float] | None = None
        if widget is not None:
            ox_px = renderer.context.px_x(spec.get("x", 0)) / sx
            oy_px = renderer.context.px_y(spec.get("y", 0)) / sy
            origin_dict = {"x": round(ox_px, 1), "y": round(oy_px, 1)}

        results.append({
            "index": idx,
            "type": wtype,
            "name": name,
            "visible": visible,
            "active": active,
            "resize_mode": "text" if wtype in TEXT_SCALED_WIDGETS else "box",
            "rect": rect_dict,
            "origin": origin_dict,
            "spec": copy.deepcopy(spec),
        })
    return results


# ---------------------------------------------------------------------------
# Widget patching & intuitive font-aware resize helper
# ---------------------------------------------------------------------------

def apply_widget_patch(
    spec: dict[str, Any],
    patch: dict[str, Any] | None = None,
    unset: list[str] | None = None,
    resize_ratio: float | None = None,
    design_w: float = DESIGN_WIDTH,
    design_h: float = DESIGN_HEIGHT,
) -> dict[str, Any]:
    """Return a modified copy of ``spec`` with patch values applied cleanly.

    Numeric ``x`` / ``width`` and ``y`` / ``height`` values (in design-canvas
    pixels) are converted into percentage strings so the Studio preview,
    1080p, and 4K exports all keep identical proportions.
    """
    out = copy.deepcopy(spec)
    wtype = canonical_widget_type(str(out.get("type", "")))

    for key in unset or []:
        out.pop(key, None)

    for key, val in (patch or {}).items():
        # Convenience proxy fields for primary graph series
        if wtype == "graph" and key in ("series_channel", "series_fill", "series_color"):
            series = out.get("series")
            if not isinstance(series, list) or not series:
                ch_fallback = out.pop("channel", None)
                series = [{"channel": ch_fallback}] if ch_fallback else [{}]
            s0 = dict(series[0]) if isinstance(series[0], dict) else {"channel": str(series[0])}
            if key == "series_channel":
                if val:
                    s0["channel"] = str(val)
            elif key == "series_fill":
                s0["fill"] = bool(val)
            elif key == "series_color":
                if val:
                    s0["color"] = str(val)
                else:
                    s0.pop("color", None)
            series[0] = s0
            out["series"] = series
            continue

        # Store geometry as percentages of the design canvas so preview == 1080p == 4K
        if key in ("x", "width") and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = f"{float(val) / max(1.0, float(design_w)) * 100.0:.2f}%"
            continue
        if key in ("y", "height") and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = f"{float(val) / max(1.0, float(design_h)) * 100.0:.2f}%"
            continue

        if val is None or val == "":
            # Remove optional override keys when cleared in the UI
            if key not in ("text", "prefix", "label", "title", "unit", "on_text", "off_text"):
                out.pop(key, None)
            else:
                out[key] = ""
        else:
            out[key] = val

    # Proportional font/radius scaling when dragging corners on text-sized widgets
    if resize_ratio is not None and math.isfinite(resize_ratio) and resize_ratio > 0:
        ratio = max(0.25, min(4.0, float(resize_ratio)))
        if wtype == "readout":
            old_vs = float(out.get("value_size", 72))
            out["value_size"] = int(round(max(16.0, min(220.0, old_vs * ratio))))
            if "label_size" in out:
                out["label_size"] = int(round(max(10.0, min(80.0, float(out["label_size"]) * ratio))))
        elif wtype in ("label", "timer", "badge"):
            default_sz = {"label": 34.0, "timer": 48.0, "badge": 22.0}[wtype]
            old_sz = float(out.get("size", default_sz))
            out["size"] = int(round(max(10.0, min(200.0, old_sz * ratio))))
        elif wtype == "led":
            old_r = float(out.get("radius", 13.0))
            out["radius"] = int(round(max(6.0, min(56.0, old_r * ratio))))
            if "size" in out:
                out["size"] = int(round(max(10.0, min(72.0, float(out["size"]) * ratio))))

    return out
