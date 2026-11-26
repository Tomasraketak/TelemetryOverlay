"""Zero-dependency local interactive Visual Studio for Telemetry Overlay.

Run with:
    telemetry-overlay studio                         # opens with sample rocket flight
    telemetry-overlay studio --open                  # also opens your web browser
    telemetry-overlay studio examples/rocket_flight.csv

Serves a responsive dark-mode browser UI using Python's stdlib ``http.server``:
- Full interactive visual canvas editor: click to select, drag to move with smart
  alignment snap guides, drag 8 corner/edge handles to resize (including automatic
  font scaling on text/readout/timer widgets), keyboard nudging, and Undo/Redo
- Categorized "+ Přidat widget" gallery for all 24 widget types with smart
  auto-binding to loaded telemetry channels
- Per-widget Visual Inspector (channels, titles, colors, opacity, panel style,
  sliders for 3D camera angles, font sizes, graph options, and layer Z-order)
- Zero-argument startup with bundled samples + drag-and-drop / Open File upload
- Live YAML config editor synchronized two-way with the visual canvas
- One-click full-resolution PNG frame, animated overlay video, and data export
"""

from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from . import __version__
from .config import DESIGN_HEIGHT, DESIGN_WIDTH, Config, load_config, parse_resolution, suggest_config
from .data import Channel, Telemetry, load as load_data
from .export import VIDEO_CODECS, codec_for_path, export_video, find_ffmpeg
from .extract import EXPORT_FORMATS, export_telemetry
from .launcher import generate_app_icon
from .render import OverlayRenderer
from .studio_editor import (
    apply_widget_patch,
    default_widget_spec,
    measure_widget_rects,
    unique_widget_name,
    widget_catalog_for_telemetry,
)
from .theme import THEMES

__all__ = ["run_studio", "create_studio_server"]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PRESET_DIR = _REPO_ROOT / "presets"
_EXAMPLES_DIR = _REPO_ROOT / "examples"

_SAMPLE_EXTENSIONS = {
    ".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".json", ".geojson",
    ".gpx", ".kml", ".kmz", ".tcx", ".fit", ".nmea", ".nma", ".igc",
    ".srt", ".mp4", ".mov",
}

_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0d1320"/>
<path d="M10 50 Q24 46 32 22 Q40 44 54 48" fill="none" stroke="#ff9f0a" stroke-width="3.5" stroke-linecap="round"/>
<path d="M32 8 C37 16 39 26 39 36 L25 36 C25 26 27 16 32 8 Z" fill="#f1f5f9"/>
<path d="M32 8 C35 13 37 18 37.5 22 L26.5 22 C27 18 29 13 32 8 Z" fill="#ff9f0a"/>
<polygon points="25,30 17,40 25,38" fill="#ef4444"/>
<polygon points="39,30 47,40 39,38" fill="#ef4444"/>
<polygon points="28,38 36,38 32,54" fill="#ff6b1a"/>
</svg>"""


_STUDIO_HTML = r"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Telemetry Overlay Studio</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>
  :root {
    --bg: #0b0f17; --panel: #121826; --panel2: #1a2235; --panel3: #222c42;
    --border: rgba(255,255,255,0.09); --text: #f1f5f9; --dim: #94a3b8;
    --accent: #ff9f0a; --cyan: #38bdf8; --good: #22c55e; --bad: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column;
    overflow: hidden; user-select: none;
  }
  input, textarea, select { user-select: text; }
  header {
    display: flex; align-items: center; gap: 8px; padding: 8px 14px;
    background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap;
    z-index: 20;
  }
  header h1 {
    margin: 0; font-size: 14px; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--accent); display: flex; align-items: center; gap: 7px; white-space: nowrap;
  }
  .ver {
    font-size: 10px; color: var(--dim); background: rgba(255,255,255,0.06);
    padding: 2px 6px; border-radius: 999px; font-weight: 600;
  }
  .src { font-size: 12px; color: var(--dim); font-family: ui-monospace, monospace; }
  .spacer { flex: 1; min-width: 6px; }
  select, button, input[type="text"], input[type="number"] {
    background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 5px 9px; font-size: 12.5px;
  }
  button { cursor: pointer; display: inline-flex; align-items: center; gap: 5px; }
  button.primary { background: var(--accent); color: #090d16; font-weight: 700; border: none; }
  button.good { background: var(--good); color: #052e16; font-weight: 700; border: none; }
  button.danger { background: rgba(239,68,68,0.16); color: #fca5a5; border-color: rgba(239,68,68,0.35); }
  button.ghost { background: transparent; border-color: transparent; padding: 4px 7px; color: var(--dim); }
  button.ghost:hover { background: rgba(255,255,255,0.07); color: var(--text); }
  button.toggle-on { background: rgba(255,159,10,0.18); border-color: rgba(255,159,10,0.55); color: #ffd089; }
  button:hover:not(:disabled) { filter: brightness(1.12); }
  button:disabled { opacity: 0.42; cursor: not-allowed; }
  main { flex: 1; display: grid; grid-template-columns: 1fr 410px; min-height: 0; }
  .stage-wrap { display: flex; flex-direction: column; padding: 10px 12px; gap: 9px; min-height: 0; }

  /* Stage quick toolbar */
  .stage-bar {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    background: var(--panel); border: 1px solid var(--border); border-radius: 9px;
    padding: 6px 10px;
  }
  .stage-bar .sep { width: 1px; height: 18px; background: var(--border); margin: 0 2px; }
  .stage-hint { font-size: 11.5px; color: var(--dim); margin-left: auto; }

  /* Interactive Canvas Stage */
  .canvas-box {
    flex: 1; display: flex; align-items: center; justify-content: center; min-height: 0;
    border-radius: 10px; border: 1px solid var(--border); overflow: hidden; position: relative;
    background-image:
      linear-gradient(45deg, #1a2130 25%, transparent 25%),
      linear-gradient(-45deg, #1a2130 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #1a2130 75%),
      linear-gradient(-45deg, transparent 75%, #1a2130 75%);
    background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
    background-color: #111622;
  }
  .canvas-box.dark-bg { background-image: none; background-color: #0d121d; }
  .canvas-box.drag-over { outline: 2px dashed var(--accent); outline-offset: -6px; }
  .drop-overlay {
    position: absolute; inset: 0; background: rgba(11,15,23,0.88); display: none;
    align-items: center; justify-content: center; flex-direction: column; gap: 8px;
    font-size: 18px; font-weight: 700; color: var(--accent); pointer-events: none; z-index: 40;
  }
  .canvas-box.drag-over .drop-overlay { display: flex; }

  .stage-frame {
    position: relative; line-height: 0; box-shadow: 0 12px 40px rgba(0,0,0,0.55);
  }
  #preview { width: 100%; height: 100%; display: block; pointer-events: none; }
  #editorLayer {
    position: absolute; inset: 0; z-index: 10; overflow: visible;
  }
  #editorLayer.hidden-boxes .w-box:not(.selected) { border-color: transparent; background: transparent; }
  #editorLayer.edit-off { pointer-events: none; opacity: 0; }

  /* Subtle optional grid */
  .grid-overlay {
    position: absolute; inset: 0; pointer-events: none; display: none;
    background-image:
      linear-gradient(to right, rgba(255,255,255,0.06) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(255,255,255,0.06) 1px, transparent 1px);
    background-size: 10% 10%;
  }
  .grid-overlay.show { display: block; }
  .safe-margin {
    position: absolute; left: 4%; top: 4%; right: 4%; bottom: 4%;
    border: 1px dashed rgba(56,189,248,0.22); pointer-events: none;
  }

  /* Interactive widget hitboxes */
  .w-box {
    position: absolute; cursor: move;
    border: 1px dashed rgba(255,255,255,0.24);
    border-radius: 4px;
    transition: border-color 0.08s, background 0.08s;
  }
  .w-box:hover {
    border: 1px solid rgba(56,189,248,0.85);
    background: rgba(56,189,248,0.06);
  }
  .w-box.selected {
    border: 2px solid var(--accent);
    background: rgba(255,159,10,0.06);
    box-shadow: 0 0 0 1px rgba(0,0,0,0.7), 0 0 18px rgba(255,159,10,0.22);
    z-index: 15;
  }
  .w-box.is-hidden {
    border-style: dotted; border-color: rgba(148,163,184,0.35);
    background: repeating-linear-gradient(-45deg, rgba(148,163,184,0.06), rgba(148,163,184,0.06) 6px, transparent 6px, transparent 12px);
  }
  .w-tag {
    position: absolute; left: -2px; top: -22px;
    background: var(--accent); color: #090d16; font-size: 10.5px; font-weight: 700;
    padding: 2px 7px; border-radius: 4px 4px 0 0; white-space: nowrap;
    pointer-events: none; display: none; letter-spacing: 0.02em;
  }
  .w-box.selected .w-tag, .w-box:hover .w-tag { display: block; }
  .w-box:not(.selected):hover .w-tag { background: var(--cyan); }

  /* 8 resize handles */
  .rh {
    position: absolute; width: 10px; height: 10px; background: #fff;
    border: 2px solid var(--accent); border-radius: 2px; display: none; z-index: 20;
  }
  .w-box.selected .rh { display: block; }
  .rh.nw { left: -6px; top: -6px; cursor: nwse-resize; }
  .rh.n  { left: calc(50% - 5px); top: -6px; cursor: ns-resize; }
  .rh.ne { right: -6px; top: -6px; cursor: nesw-resize; }
  .rh.e  { right: -6px; top: calc(50% - 5px); cursor: ew-resize; }
  .rh.se { right: -6px; bottom: -6px; cursor: nwse-resize; }
  .rh.s  { left: calc(50% - 5px); bottom: -6px; cursor: ns-resize; }
  .rh.sw { left: -6px; bottom: -6px; cursor: nesw-resize; }
  .rh.w  { left: -6px; top: calc(50% - 5px); cursor: ew-resize; }

  /* Snap alignment guides & live drag tooltip */
  .snap-line {
    position: absolute; pointer-events: none; z-index: 25;
  }
  .snap-line.v { top: 0; bottom: 0; width: 1px; background: var(--cyan); box-shadow: 0 0 6px var(--cyan); }
  .snap-line.h { left: 0; right: 0; height: 1px; background: var(--cyan); box-shadow: 0 0 6px var(--cyan); }
  #dragHud {
    position: absolute; left: 10px; bottom: 10px; z-index: 30; display: none;
    background: rgba(9,13,22,0.92); border: 1px solid var(--accent); color: #fff;
    font-family: ui-monospace, monospace; font-size: 11.5px; padding: 4px 9px; border-radius: 6px;
    pointer-events: none;
  }

  /* Timeline scrubber */
  .scrub {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 8px 12px; display: flex; flex-direction: column; gap: 6px;
  }
  .scrub-row { display: flex; align-items: center; gap: 10px; }
  input[type="range"] { accent-color: var(--accent); cursor: pointer; }
  .scrub-row input[type="range"] { flex: 1; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .events { display: flex; gap: 6px; flex-wrap: wrap; }
  .ev-btn {
    font-size: 11px; padding: 3px 8px; border-radius: 999px;
    background: rgba(255,159,10,0.14); border: 1px solid rgba(255,159,10,0.4); color: #ffd089;
  }

  /* Right Sidebar */
  aside {
    background: var(--panel); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
  }
  .tabs { display: flex; border-bottom: 1px solid var(--border); }
  .tabs button {
    flex: 1; border: none; border-radius: 0; padding: 9px 4px;
    background: transparent; color: var(--dim); font-weight: 600; font-size: 11.5px; justify-content: center;
  }
  .tabs button.active { color: var(--accent); border-bottom: 2px solid var(--accent); }
  .tab-pane { flex: 1; display: none; flex-direction: column; padding: 10px; gap: 10px; min-height: 0; overflow: auto; }
  .tab-pane.active { display: flex; }

  /* Layers list */
  .section-head {
    display: flex; align-items: center; justify-content: space-between; gap: 6px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--dim); font-weight: 700;
  }
  .layer-list {
    display: flex; flex-direction: column; gap: 4px; max-height: 195px; overflow-y: auto;
    padding-right: 2px;
  }
  .layer-row {
    display: flex; align-items: center; gap: 6px; padding: 5px 7px; border-radius: 7px;
    background: var(--panel2); border: 1px solid transparent; cursor: pointer; font-size: 12px;
  }
  .layer-row:hover { border-color: rgba(255,255,255,0.16); }
  .layer-row.selected { border-color: var(--accent); background: rgba(255,159,10,0.12); }
  .layer-row.inactive { opacity: 0.55; }
  .layer-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .layer-type { font-size: 10px; color: var(--dim); font-family: ui-monospace, monospace; background: rgba(0,0,0,0.28); padding: 1px 5px; border-radius: 4px; }
  .layer-btns { display: flex; gap: 2px; }
  .layer-btns button { padding: 2px 5px; font-size: 11px; border-radius: 5px; background: rgba(255,255,255,0.05); border: none; color: var(--dim); }
  .layer-btns button:hover { color: #fff; background: rgba(255,255,255,0.14); }

  /* Inspector Form */
  .inspector {
    display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--border); padding-top: 10px;
  }
  .insp-card {
    background: var(--panel2); border: 1px solid var(--border); border-radius: 9px;
    padding: 10px; display: flex; flex-direction: column; gap: 8px;
  }
  .insp-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent); font-weight: 700;
    display: flex; align-items: center; justify-content: space-between;
  }
  .prop-grid { display: grid; grid-template-columns: 128px 1fr; gap: 7px 8px; align-items: center; font-size: 12px; }
  .prop-grid label { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .prop-grid input[type="text"], .prop-grid input[type="number"], .prop-grid select { width: 100%; }
  .xywh-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
  .xywh-cell { display: flex; flex-direction: column; gap: 2px; }
  .xywh-cell span { font-size: 10px; color: var(--dim); text-transform: uppercase; }
  .xywh-cell input { width: 100%; padding: 4px 6px; font-family: ui-monospace, monospace; font-size: 12px; }
  .range-wrap { display: flex; align-items: center; gap: 8px; }
  .range-wrap input[type="range"] { flex: 1; }
  .range-val { font-family: ui-monospace, monospace; font-size: 11.5px; color: var(--text); min-width: 40px; text-align: right; }
  .color-wrap { display: flex; align-items: center; gap: 6px; }
  input[type="color"] {
    width: 34px; height: 28px; padding: 2px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); cursor: pointer;
  }
  .check-row { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 12.5px; color: var(--text); }
  .check-row input[type="checkbox"] { accent-color: var(--accent); width: 15px; height: 15px; cursor: pointer; }
  .empty-insp {
    padding: 22px 14px; text-align: center; color: var(--dim); font-size: 12.5px;
    background: rgba(255,255,255,0.02); border: 1px dashed var(--border); border-radius: 9px;
    display: flex; flex-direction: column; gap: 10px; align-items: center;
  }

  /* Add Widget Modal */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(5,8,15,0.78); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; z-index: 100; padding: 18px;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: min(860px, 96vw); max-height: 86vh; background: var(--panel);
    border: 1px solid rgba(255,255,255,0.14); border-radius: 14px;
    display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 70px rgba(0,0,0,0.7);
  }
  .modal-head {
    display: flex; align-items: center; gap: 12px; padding: 14px 18px;
    border-bottom: 1px solid var(--border);
  }
  .modal-head h2 { margin: 0; font-size: 16px; color: var(--accent); }
  .modal-head input { flex: 1; max-width: 320px; }
  .modal-body { padding: 14px 18px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
  .cat-title { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--dim); font-weight: 700; margin-bottom: 8px; }
  .gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(235px, 1fr)); gap: 9px; }
  .w-card {
    background: var(--panel2); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 12px; cursor: pointer; display: flex; flex-direction: column; gap: 4px;
    transition: border-color 0.12s, transform 0.08s; text-align: left;
  }
  .w-card:hover:not(.disabled) { border-color: var(--accent); transform: translateY(-1px); background: var(--panel3); }
  .w-card.disabled { opacity: 0.45; cursor: not-allowed; }
  .w-card-top { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 13px; color: var(--text); }
  .w-card-ico { font-size: 17px; width: 24px; text-align: center; }
  .w-card-desc { font-size: 11.5px; color: var(--dim); line-height: 1.35; }
  .pill-rec { margin-left: auto; font-size: 10px; background: rgba(34,197,94,0.18); color: #86efac; padding: 2px 6px; border-radius: 999px; }
  .pill-miss { margin-left: auto; font-size: 10px; background: rgba(239,68,68,0.18); color: #fca5a5; padding: 2px 6px; border-radius: 999px; }

  textarea {
    flex: 1; width: 100%; resize: none; background: #0a0e17; color: #e2e8f0;
    border: 1px solid var(--border); border-radius: 8px; padding: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45;
  }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--dim); font-weight: 600; }
  .err {
    color: #fca5a5; background: rgba(239,68,68,0.14); border: 1px solid rgba(239,68,68,0.38);
    border-radius: 8px; padding: 7px 10px; font-size: 12px; display: none;
  }
  .ok-msg {
    color: #86efac; background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35);
    border-radius: 8px; padding: 7px 10px; font-size: 12px; display: none;
  }
  .row-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .card {
    background: var(--panel2); border: 1px solid var(--border); border-radius: 9px;
    padding: 12px; display: flex; flex-direction: column; gap: 10px;
  }
  .card h3 { margin: 0; font-size: 13px; color: var(--text); }
  .form-grid { display: grid; grid-template-columns: 110px 1fr; gap: 8px; align-items: center; font-size: 12.5px; }
  .form-grid label { color: var(--dim); }
  @media (max-width: 1020px) {
    main { grid-template-columns: 1fr; grid-template-rows: 54vh 1fr; }
    aside { border-left: none; border-top: 1px solid var(--border); }
    .stage-hint { display: none; }
  }
</style>
</head>
<body>
<header>
  <h1>Telemetry Overlay Studio <span class="ver" id="verTag"></span></h1>
  <button id="addWidgetTopBtn" class="primary">+ Přidat widget</button>
  <button id="openFileBtn">Otevřít soubor...</button>
  <input id="fileInput" type="file" style="display:none"
         accept=".csv,.tsv,.txt,.xlsx,.xlsm,.gpx,.kml,.kmz,.tcx,.fit,.nmea,.igc,.srt,.json,.geojson,.mp4,.mov" />
  <input id="assetInput" type="file" style="display:none" accept=".png,.jpg,.jpeg,.webp" />
  <label class="src">Vzorek: <select id="sampleSel"></select></label>
  <span class="src" id="sourceLabel"></span>
  <span class="spacer"></span>
  <label class="src">Šablona: <select id="presetSel"></select></label>
  <label class="src">Motiv: <select id="themeSel"></select></label>
  <button id="bgToggle">Pozadí: Šachovnice</button>
</header>

<main>
  <section class="stage-wrap">
    <div class="stage-bar">
      <button id="undoBtn" title="Zpět (Ctrl+Z)" disabled>↶ Zpět</button>
      <button id="redoBtn" title="Znovu (Ctrl+Y)" disabled>↷ Znovu</button>
      <span class="sep"></span>
      <button id="editToggleBtn" class="toggle-on" title="Zobrazit/skrýt rámečky úprav">✎ Úpravy</button>
      <button id="snapToggleBtn" class="toggle-on" title="Přichytávat k okrajům a středu (Alt vypne)">🧲 Přichytávat</button>
      <button id="gridToggleBtn" title="Zobrazit pomocnou mřížku">⊞ Mřížka</button>
      <span class="sep"></span>
      <button id="dupSelBtn" disabled title="Duplikovat vybraný (Ctrl+D)">⧉ Duplikovat</button>
      <button id="delSelBtn" class="danger" disabled title="Smazat vybraný (Delete)">✕ Smazat</button>
      <span class="stage-hint">Táhni myší = přesun · Rohy = velikost · Šipky = jemný posun</span>
    </div>

    <div class="canvas-box" id="canvasBox">
      <div class="drop-overlay">Přetáhni sem telemetrický soubor<span style="font-size:13px;color:#cbd5e1;font-weight:400">CSV · XLSX · GoPro MP4 · DJI SRT · FIT · GPX · KML · IGC · NMEA · JSON</span></div>
      <div class="stage-frame" id="stageFrame">
        <img id="preview" alt="Overlay Preview" />
        <div id="editorLayer">
          <div class="grid-overlay" id="gridOverlay"><div class="safe-margin"></div></div>
          <div id="widgetBoxes"></div>
          <div id="snapGuides"></div>
        </div>
        <div id="dragHud"></div>
      </div>
    </div>

    <div class="scrub">
      <div class="scrub-row">
        <button id="playBtn" class="primary">Play</button>
        <input id="slider" type="range" min="0" max="1000" value="350" />
        <span class="mono" id="timeLabel">0.00 s</span>
      </div>
      <div class="events" id="eventsBar"></div>
    </div>
  </section>

  <aside>
    <div class="tabs">
      <button class="active" data-tab="editorTab">Editor</button>
      <button data-tab="yamlTab">YAML</button>
      <button data-tab="channelsTab">Kanály</button>
      <button data-tab="renderTab">Render</button>
      <button data-tab="exportTab">Export</button>
    </div>

    <div class="tab-pane active" id="editorTab">
      <div class="err" id="editorErr"></div>
      <div class="section-head">
        <span>Vrstvy / Widgety (<span id="widgetCount">0</span>)</span>
        <div class="row-actions">
          <button id="addWidgetSideBtn" class="primary" style="padding:3px 8px;font-size:11.5px">+ Přidat</button>
          <button id="clearAllBtn" class="danger" style="padding:3px 8px;font-size:11.5px" title="Smazat všechny widgety a začít od nuly">Vymazat vše</button>
        </div>
      </div>
      <div class="layer-list" id="layerList"></div>
      <div class="inspector" id="inspector"></div>
    </div>

    <div class="tab-pane" id="yamlTab">
      <div class="err" id="errBox"></div>
      <textarea id="yamlEdit" spellcheck="false"></textarea>
      <div class="row-actions">
        <button id="applyBtn" class="primary">Použít YAML</button>
        <button id="downloadYaml">Stáhnout YAML</button>
      </div>
    </div>

    <div class="tab-pane" id="channelsTab">
      <table id="chanTable">
        <thead><tr><th>Klíč</th><th>Jednotka</th><th>Min</th><th>Max</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="tab-pane" id="renderTab">
      <div class="card">
        <h3>Snímek (aktuální čas)</h3>
        <p class="src" style="margin:0">Stáhne průhledné PNG v plném rozlišení na aktuálním čase.</p>
        <div class="row-actions">
          <button id="savePngBtn" class="primary">Uložit Full-Res PNG</button>
        </div>
      </div>
      <div class="card">
        <h3>Animované overlay video</h3>
        <div class="form-grid">
          <label for="resSel">Rozlišení</label>
          <select id="resSel">
            <option value="720p">720p (1280×720)</option>
            <option value="1080p" selected>1080p (1920×1080)</option>
            <option value="1440p">1440p (2560×1440)</option>
            <option value="4k">4K (3840×2160)</option>
            <option value="vertical1080">Vertikální 1080×1920</option>
          </select>
          <label for="fpsSel">FPS</label>
          <select id="fpsSel">
            <option value="24">24 fps</option>
            <option value="30" selected>30 fps</option>
            <option value="60">60 fps</option>
          </select>
          <label for="codecSel">Formát</label>
          <select id="codecSel">
            <option value="qtrle">.mov — QuickTime RLE (alfa, rychlé a malé)</option>
            <option value="prores4444">.mov — ProRes 4444 (alfa, Resolve/Premiere)</option>
            <option value="vp9">.webm — VP9 (alfa, web/OBS)</option>
            <option value="gif">.gif — Animovaný GIF (alfa)</option>
            <option value="webp">.webp — Animované WebP (alfa)</option>
            <option value="h264">.mp4 — H.264 (bez alfy)</option>
          </select>
        </div>
        <div class="err" id="renderErr"></div>
        <div class="ok-msg" id="renderOk"></div>
        <div class="row-actions">
          <button id="renderVideoBtn" class="good">Renderovat a stáhnout video</button>
        </div>
      </div>
    </div>

    <div class="tab-pane" id="exportTab">
      <p class="src">Offline export načtených a dopočítaných kanálů:</p>
      <div class="row-actions" id="exportBtns"></div>
    </div>
  </aside>
</main>

<!-- Widget Gallery Modal -->
<div class="modal-bg" id="galleryModal">
  <div class="modal">
    <div class="modal-head">
      <h2>+ Přidat widget</h2>
      <input type="text" id="gallerySearch" placeholder="Hledat widget (např. motor, 3D, graf, budík, mapa)..." />
      <span class="spacer"></span>
      <button id="closeGalleryBtn">Zavřít (Esc)</button>
    </div>
    <div class="modal-body" id="galleryBody"></div>
  </div>
</div>

<script>
let meta = null, selectedIdx = null;
let playing = false, timer = null, darkBg = false, inflight = false;
let editMode = true, snapEnabled = true, gridEnabled = false;
let undoStack = [], redoStack = [];
let dragState = null;
let livePatchTimer = null, pendingPatch = null;
let pendingAssetTarget = null;

const $ = id => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toHexColor(raw, fallback = '#ff9f0a') {
  if (!raw || typeof raw !== 'string') return fallback;
  const m = raw.trim().match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})/);
  if (!m) return fallback;
  if (m[1].length === 3) {
    return '#' + m[1].split('').map(c => c + c).join('');
  }
  return '#' + m[1];
}

function showEditorError(msg) {
  const box = $('editorErr');
  if (!msg) { box.style.display = 'none'; return; }
  box.textContent = msg;
  box.style.display = 'block';
  setTimeout(() => { if (box.textContent === msg) box.style.display = 'none'; }, 5000);
}

function pushUndo() {
  if (!meta) return;
  undoStack.push({ yaml: meta.yaml, selectedIdx });
  if (undoStack.length > 40) undoStack.shift();
  redoStack = [];
  updateUndoButtons();
}

function updateUndoButtons() {
  $('undoBtn').disabled = undoStack.length === 0;
  $('redoBtn').disabled = redoStack.length === 0;
  const hasSel = selectedIdx !== null && meta && meta.widgets && meta.widgets[selectedIdx];
  $('dupSelBtn').disabled = !hasSel;
  $('delSelBtn').disabled = !hasSel;
}

function currentTime() {
  if (!meta) return 0;
  const frac = Number($('slider').value) / 1000;
  return meta.t0 + frac * (meta.t1 - meta.t0);
}

// Keep stage-frame sized to exact preview aspect ratio inside canvas-box
function fitStageFrame() {
  const box = $('canvasBox');
  const frame = $('stageFrame');
  if (!meta || !box || !frame) return;
  const bw = Math.max(100, box.clientWidth - 20);
  const bh = Math.max(80, box.clientHeight - 20);
  const ratio = (meta.design_width || 1920) / Math.max(1, meta.design_height || 1080);
  let w = bw, h = bw / ratio;
  if (h > bh) { h = bh; w = bh * ratio; }
  frame.style.width = Math.round(w) + 'px';
  frame.style.height = Math.round(h) + 'px';
}
new ResizeObserver(fitStageFrame).observe($('canvasBox'));
window.addEventListener('resize', fitStageFrame);

function applyMetaToUI(data, opts = {}) {
  meta = data;
  if (opts.selectedIdx !== undefined) {
    selectedIdx = opts.selectedIdx;
  } else if (selectedIdx !== null && (!meta.widgets || selectedIdx >= meta.widgets.length)) {
    selectedIdx = meta.widgets && meta.widgets.length ? meta.widgets.length - 1 : null;
  }
  $('verTag').textContent = 'v' + (meta.version || '');
  $('sourceLabel').textContent = `${meta.source} (${meta.samples} pts @ ${meta.sample_rate.toFixed(1)} Hz)`;

  const sSel = $('sampleSel');
  if (meta.samples_list) {
    sSel.innerHTML = '<option value="">-- vzorky --</option>' +
      meta.samples_list.map(s => `<option value="${esc(s)}" ${s===meta.source?'selected':''}>${esc(s)}</option>`).join('');
  }
  const pSel = $('presetSel');
  pSel.innerHTML = '<option value="__custom__">(Vlastní / Auto)</option>' +
    meta.presets.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');

  const tSel = $('themeSel');
  tSel.innerHTML = meta.themes.map(t => `<option value="${esc(t)}" ${t===meta.theme?'selected':''}>${esc(t)}</option>`).join('');

  $('yamlEdit').value = meta.yaml;
  renderChannels();
  renderEvents();
  $('exportBtns').innerHTML = meta.export_formats.map(f =>
    `<a href="/api/export?format=${encodeURIComponent(f)}" download><button>${esc(f.toUpperCase())}</button></a>`
  ).join('');

  if (opts.resetSlider) {
    const peak = (meta.events || []).find(e => /peak|burn|ign/i.test(e.label));
    if (peak && meta.t1 > meta.t0) {
      const frac = Math.max(0, Math.min(1, (peak.time - meta.t0) / (meta.t1 - meta.t0)));
      $('slider').value = Math.round(frac * 1000);
    } else {
      $('slider').value = 350;
    }
  }

  fitStageFrame();
  renderWidgetBoxes();
  renderLayers();
  if (!opts.skipInspector) renderInspector();
  updateUndoButtons();
  updateFrame();
}

async function init() {
  const res = await fetch('/api/state');
  const data = await res.json();
  applyMetaToUI(data, { resetSlider: true, selectedIdx: null });
}

function renderChannels() {
  const tbody = $('chanTable').querySelector('tbody');
  tbody.innerHTML = meta.channels.map(c =>
    `<tr><td class="mono">${esc(c.key)}</td><td>${esc(c.unit)}</td><td class="mono">${esc(c.min)}</td><td class="mono">${esc(c.max)}</td></tr>`
  ).join('');
}

function renderEvents() {
  const bar = $('eventsBar');
  if (!meta.events || !meta.events.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = meta.events.map(e => {
    const frac = Math.max(0, Math.min(1, (e.time - meta.t0) / Math.max(1e-6, meta.t1 - meta.t0)));
    return `<button class="ev-btn" onclick="jumpTo(${frac})">${esc(e.label)} @ ${e.time.toFixed(2)}s</button>`;
  }).join('');
}

window.jumpTo = (frac) => {
  $('slider').value = Math.round(frac * 1000);
  updateFrame();
};

async function updateFrame() {
  if (!meta || inflight) return;
  inflight = true;
  const t = currentTime();
  $('timeLabel').textContent = `${t.toFixed(2)} s / ${meta.t1.toFixed(2)} s`;
  try {
    const resp = await fetch(`/api/frame?t=${t.toFixed(4)}&_=${Date.now()}`);
    if (resp.ok) {
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const img = $('preview');
      const old = img.dataset.url;
      img.src = url;
      img.dataset.url = url;
      if (old) URL.revokeObjectURL(old);
    }
  } finally { inflight = false; }
}

// =========================================================================
// Interactive Canvas Hitboxes, Drag-to-Move, 8-Handle Resize & Smart Snapping
// =========================================================================

function renderWidgetBoxes() {
  const container = $('widgetBoxes');
  if (!meta || !meta.widgets) { container.innerHTML = ''; return; }
  const dw = meta.design_width || 1920;
  const dh = meta.design_height || 1080;

  container.innerHTML = meta.widgets.map(w => {
    if (!w.rect) return '';
    const isSel = w.index === selectedIdx;
    const cls = ['w-box', isSel ? 'selected' : '', w.visible ? '' : 'is-hidden'].filter(Boolean).join(' ');
    const style = `left:${(w.rect.x / dw * 100).toFixed(3)}%;top:${(w.rect.y / dh * 100).toFixed(3)}%;width:${(w.rect.w / dw * 100).toFixed(3)}%;height:${(w.rect.h / dh * 100).toFixed(3)}%;`;
    const handles = ['nw','n','ne','e','se','s','sw','w'].map(d => `<div class="rh ${d}" data-dir="${d}"></div>`).join('');
    return `<div class="${cls}" data-idx="${w.index}" style="${style}">
      <div class="w-tag">${esc(w.name)} (${esc(w.type)})</div>
      ${handles}
    </div>`;
  }).join('');
}

function selectWidget(idx, focusEditor = false) {
  selectedIdx = (idx === null || idx === undefined) ? null : Number(idx);
  renderWidgetBoxes();
  renderLayers();
  renderInspector();
  updateUndoButtons();
  if (focusEditor) switchTab('editorTab');
}

function switchTab(tabId) {
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === tabId));
}

function computeSnap(box, skipIdx, dw, dh) {
  const thresh = 14;
  const vTargets = [Math.round(dw * 0.04), dw / 2, Math.round(dw * 0.96)];
  const hTargets = [Math.round(dh * 0.04), dh / 2, Math.round(dh * 0.96)];
  for (const w of (meta.widgets || [])) {
    if (w.index === skipIdx || !w.rect || !w.visible) continue;
    vTargets.push(w.rect.x, w.rect.x + w.rect.w / 2, w.rect.x + w.rect.w);
    hTargets.push(w.rect.y, w.rect.y + w.rect.h / 2, w.rect.y + w.rect.h);
  }
  let x = box.x, y = box.y;
  const guides = [];

  // Snap X (left, center, right)
  let bestDx = thresh + 1, snapX = null;
  for (const tx of vTargets) {
    for (const [edge, offset] of [[x, 0], [x + box.w / 2, box.w / 2], [x + box.w, box.w]]) {
      const d = Math.abs(edge - tx);
      if (d < bestDx) { bestDx = d; snapX = { newX: tx - offset, line: tx }; }
    }
  }
  if (snapX) { x = snapX.newX; guides.push({ axis: 'v', pos: snapX.line / dw * 100 }); }

  // Snap Y (top, middle, bottom)
  let bestDy = thresh + 1, snapY = null;
  for (const ty of hTargets) {
    for (const [edge, offset] of [[y, 0], [y + box.h / 2, box.h / 2], [y + box.h, box.h]]) {
      const d = Math.abs(edge - ty);
      if (d < bestDy) { bestDy = d; snapY = { newY: ty - offset, line: ty }; }
    }
  }
  if (snapY) { y = snapY.newY; guides.push({ axis: 'h', pos: snapY.line / dh * 100 }); }

  return { x, y, guides };
}

function renderSnapGuides(guides) {
  $('snapGuides').innerHTML = guides.map(g =>
    g.axis === 'v'
      ? `<div class="snap-line v" style="left:${g.pos.toFixed(3)}%"></div>`
      : `<div class="snap-line h" style="top:${g.pos.toFixed(3)}%"></div>`
  ).join('');
}

$('editorLayer').addEventListener('pointerdown', (e) => {
  if (!editMode || !meta) return;
  const handle = e.target.closest('.rh');
  const boxEl = e.target.closest('.w-box');
  if (!boxEl) {
    selectWidget(null);
    return;
  }
  e.preventDefault();
  const idx = Number(boxEl.dataset.idx);
  const w = meta.widgets[idx];
  if (!w || !w.rect) return;
  if (selectedIdx !== idx) selectWidget(idx);

  const stageRect = $('stageFrame').getBoundingClientRect();
  dragState = {
    idx,
    mode: handle ? 'resize' : 'move',
    dir: handle ? handle.dataset.dir : null,
    startClientX: e.clientX,
    startClientY: e.clientY,
    stageW: stageRect.width,
    stageH: stageRect.height,
    orig: { ...w.rect },
    cur: { ...w.rect },
    origin: w.origin ? { ...w.origin } : { x: w.rect.x, y: w.rect.y },
    resizeMode: w.resize_mode || 'box',
    moved: false,
    boxEl: $('widgetBoxes').querySelector(`.w-box[data-idx="${idx}"]`),
  };
  window.addEventListener('pointermove', onStagePointerMove);
  window.addEventListener('pointerup', onStagePointerUp);
});

function onStagePointerMove(e) {
  if (!dragState || !meta) return;
  const dw = meta.design_width || 1920;
  const dh = meta.design_height || 1080;
  const dx = (e.clientX - dragState.startClientX) / dragState.stageW * dw;
  const dy = (e.clientY - dragState.startClientY) / dragState.stageH * dh;
  if (Math.abs(dx) > 2 || Math.abs(dy) > 2) dragState.moved = true;
  if (!dragState.moved) return;

  const o = dragState.orig;
  let nx = o.x, ny = o.y, nw = o.w, nh = o.h;

  if (dragState.mode === 'move') {
    nx = o.x + dx;
    ny = o.y + dy;
    if (snapEnabled && !e.altKey) {
      const sn = computeSnap({ x: nx, y: ny, w: nw, h: nh }, dragState.idx, dw, dh);
      nx = sn.x; ny = sn.y;
      renderSnapGuides(sn.guides);
    } else {
      renderSnapGuides([]);
    }
  } else {
    const dir = dragState.dir;
    const minW = 48, minH = 26;
    if (dir.includes('e')) nw = Math.max(minW, o.w + dx);
    if (dir.includes('s')) nh = Math.max(minH, o.h + dy);
    if (dir.includes('w')) {
      const candW = Math.max(minW, o.w - dx);
      nx = o.x + (o.w - candW);
      nw = candW;
    }
    if (dir.includes('n')) {
      const candH = Math.max(minH, o.h - dy);
      ny = o.y + (o.h - candH);
      nh = candH;
    }
    // Keep aspect ratio when holding Shift on a corner handle (or for text widgets)
    const isCorner = dir.length === 2;
    if ((e.shiftKey && isCorner) || dragState.resizeMode === 'text') {
      const aspect = o.w / Math.max(1, o.h);
      if (dir === 'e' || dir === 'w') {
        nh = Math.max(minH, nw / aspect);
      } else {
        nw = Math.max(minW, nh * aspect);
        if (dir.includes('w')) nx = o.x + (o.w - nw);
      }
    }
    renderSnapGuides([]);
  }

  dragState.cur = { x: nx, y: ny, w: nw, h: nh };
  const el = dragState.boxEl;
  if (el) {
    el.style.left = (nx / dw * 100).toFixed(3) + '%';
    el.style.top = (ny / dh * 100).toFixed(3) + '%';
    el.style.width = (nw / dw * 100).toFixed(3) + '%';
    el.style.height = (nh / dh * 100).toFixed(3) + '%';
  }
  const hud = $('dragHud');
  hud.style.display = 'block';
  hud.textContent = dragState.mode === 'move'
    ? `X: ${Math.round(nx)} px   Y: ${Math.round(ny)} px`
    : `${Math.round(nw)} × ${Math.round(nh)} px   (X: ${Math.round(nx)}, Y: ${Math.round(ny)})`;
}

async function onStagePointerUp() {
  window.removeEventListener('pointermove', onStagePointerMove);
  window.removeEventListener('pointerup', onStagePointerUp);
  renderSnapGuides([]);
  $('dragHud').style.display = 'none';
  if (!dragState) return;
  const st = dragState;
  dragState = null;
  if (!st.moved) return;

  const dx = st.cur.x - st.orig.x;
  const dy = st.cur.y - st.orig.y;

  if (st.resizeMode === 'text') {
    const patch = {
      x: Math.round(st.origin.x + dx),
      y: Math.round(st.origin.y + dy),
    };
    const ratio = st.mode === 'resize' ? (st.cur.h / Math.max(1, st.orig.h)) : null;
    await updateWidgetOnServer(st.idx, patch, { resize_ratio: ratio, pushHistory: true });
  } else {
    const patch = {
      x: Math.round(st.cur.x),
      y: Math.round(st.cur.y),
      width: Math.round(st.cur.w),
      height: Math.round(st.cur.h),
      anchor: 'top-left',
    };
    await updateWidgetOnServer(st.idx, patch, { pushHistory: true });
  }
}

// =========================================================================
// Backend Widget Mutations
// =========================================================================

async function widgetApi(endpoint, payload, { pushHistory = true, keepSelected = undefined, skipInspector = false } = {}) {
  showEditorError('');
  if (pushHistory) pushUndo();
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payload, t: currentTime() }),
  });
  const data = await res.json();
  if (!res.ok) {
    if (pushHistory && undoStack.length) undoStack.pop();
    updateUndoButtons();
    showEditorError(data.error || 'Chyba při úpravě widgetu');
    renderWidgetBoxes();
    return null;
  }
  const sel = keepSelected !== undefined
    ? keepSelected
    : (data.selected_index !== undefined ? data.selected_index : selectedIdx);
  applyMetaToUI(data, { selectedIdx: sel, skipInspector });
  return data;
}

async function updateWidgetOnServer(index, patch, { unset = [], resize_ratio = null, pushHistory = true, skipInspector = false } = {}) {
  const body = { index, patch, unset };
  if (resize_ratio !== null) body.resize_ratio = resize_ratio;
  return widgetApi('/api/widgets/update', body, { pushHistory, keepSelected: index, skipInspector });
}

function scheduleLivePatch(index, patch, unset = []) {
  if (!pendingPatch || pendingPatch.index !== index) {
    pushUndo();
    pendingPatch = { index, patch: { ...patch }, unset: [...unset] };
  } else {
    Object.assign(pendingPatch.patch, patch);
    pendingPatch.unset = Array.from(new Set([...pendingPatch.unset, ...unset]));
  }
  if (livePatchTimer) return;
  livePatchTimer = setTimeout(async () => {
    const job = pendingPatch;
    pendingPatch = null;
    livePatchTimer = null;
    if (job) {
      await updateWidgetOnServer(job.index, job.patch, { unset: job.unset, pushHistory: false, skipInspector: true });
    }
  }, 95);
}

// =========================================================================
// Layers List & Visual Inspector
// =========================================================================

function renderLayers() {
  const list = $('layerList');
  const widgets = (meta && meta.widgets) || [];
  $('widgetCount').textContent = widgets.length;
  if (!widgets.length) {
    list.innerHTML = '<div class="src" style="padding:8px;text-align:center">Žádné widgety — klikni na "+ Přidat"</div>';
    return;
  }
  // Display top-most layer first (reverse Z-order) like Photoshop/Figma
  const rows = [...widgets].reverse();
  list.innerHTML = rows.map(w => {
    const sel = w.index === selectedIdx ? 'selected' : '';
    const inact = (!w.visible || !w.active) ? 'inactive' : '';
    const eye = w.visible ? '👁' : '🚫';
    const warn = !w.active ? ' title="Volitelný widget není aktivní (chybí kanál v datech)"' : '';
    return `<div class="layer-row ${sel} ${inact}" data-idx="${w.index}"${warn}>
      <span class="layer-name">${esc(w.name)}</span>
      <span class="layer-type">${esc(w.type)}</span>
      <div class="layer-btns">
        <button data-act="vis" title="Zobrazit/skrýt">${eye}</button>
        <button data-act="up" title="Posunout nahoru (nad ostatní)">▲</button>
        <button data-act="down" title="Posunout dolů (pod ostatní)">▼</button>
        <button data-act="dup" title="Duplikovat">⧉</button>
        <button data-act="del" title="Smazat">✕</button>
      </div>
    </div>`;
  }).join('');
}

$('layerList').addEventListener('click', async (e) => {
  const row = e.target.closest('.layer-row');
  if (!row) return;
  const idx = Number(row.dataset.idx);
  const btn = e.target.closest('button[data-act]');
  if (!btn) {
    selectWidget(idx);
    return;
  }
  e.stopPropagation();
  const act = btn.dataset.act;
  const w = meta.widgets[idx];
  if (!w) return;
  if (act === 'vis') {
    await updateWidgetOnServer(idx, { visible: !w.visible });
  } else if (act === 'up' && idx < meta.widgets.length - 1) {
    await widgetApi('/api/widgets/reorder', { from_index: idx, to_index: idx + 1 });
  } else if (act === 'down' && idx > 0) {
    await widgetApi('/api/widgets/reorder', { from_index: idx, to_index: idx - 1 });
  } else if (act === 'dup') {
    await widgetApi('/api/widgets/duplicate', { index: idx });
  } else if (act === 'del') {
    await widgetApi('/api/widgets/delete', { index: idx }, { keepSelected: null });
  }
});

function getSpecFieldValue(spec, wtype, key, defVal) {
  if (wtype === 'graph') {
    const s0 = Array.isArray(spec.series) && spec.series.length
      ? (typeof spec.series[0] === 'object' ? spec.series[0] : { channel: spec.series[0] })
      : { channel: spec.channel };
    if (key === 'series_channel') return Array.isArray(s0.channel) ? s0.channel[0] : (s0.channel || '');
    if (key === 'series_fill') return s0.fill !== undefined ? Boolean(s0.fill) : Boolean(defVal);
    if (key === 'series_color') return s0.color || '';
  }
  let val = spec[key];
  if (Array.isArray(val)) val = val[0];
  return val !== undefined ? val : (defVal !== undefined ? defVal : '');
}

function renderFieldControl(f, spec, wtype) {
  const val = getSpecFieldValue(spec, wtype, f.key, f.default);
  const k = esc(f.key);

  if (f.kind === 'channel' || f.kind === 'opt_channel') {
    const opts = (f.kind === 'opt_channel' ? '<option value="">(žádný)</option>' : '') +
      (meta.channels || []).map(c => {
        const sel = String(val) === c.key ? 'selected' : '';
        const unit = c.unit ? ` [${c.unit}]` : '';
        return `<option value="${esc(c.key)}" ${sel}>${esc(c.key)}${esc(unit)}</option>`;
      }).join('');
    // Preserve custom alias if not exact match
    const hasExact = (meta.channels || []).some(c => c.key === String(val));
    const extra = (!hasExact && val) ? `<option value="${esc(val)}" selected>${esc(val)}</option>` : '';
    return `<label>${esc(f.label)}</label><select data-field="${k}" data-kind="${f.kind}">${extra}${opts}</select>`;
  }

  if (f.kind === 'bool') {
    return `<label>${esc(f.label)}</label><label class="check-row"><input type="checkbox" data-field="${k}" data-kind="bool" ${val ? 'checked' : ''} /><span>${val ? 'Zapnuto' : 'Vypnuto'}</span></label>`;
  }

  if (f.kind === 'select') {
    const opts = (f.options || []).map(o => `<option value="${esc(o)}" ${String(val) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('');
    return `<label>${esc(f.label)}</label><select data-field="${k}" data-kind="select">${opts}</select>`;
  }

  if (f.kind === 'range') {
    const num = Number(val !== '' ? val : (f.default ?? f.min));
    return `<label>${esc(f.label)}</label>
      <div class="range-wrap">
        <input type="range" data-field="${k}" data-kind="range" min="${f.min}" max="${f.max}" step="${f.step || 1}" value="${num}" />
        <span class="range-val">${num}</span>
      </div>`;
  }

  if (f.kind === 'color') {
    const hasVal = Boolean(val);
    const hex = toHexColor(val, '#ff9f0a');
    return `<label>${esc(f.label)}</label>
      <div class="color-wrap">
        <input type="color" data-field="${k}" data-kind="color" value="${hex}" />
        <span class="mono" style="color:var(--dim);font-size:11px">${hasVal ? esc(val) : 'motiv'}</span>
        ${hasVal ? `<button type="button" class="ghost" data-clear="${k}" style="margin-left:auto;font-size:11px">Reset</button>` : ''}
      </div>`;
  }

  if (f.kind === 'number') {
    return `<label>${esc(f.label)}</label><input type="number" step="any" data-field="${k}" data-kind="number" value="${val === '' ? '' : esc(val)}" placeholder="auto" />`;
  }

  // text (plus upload button for image.path)
  if (wtype === 'image' && f.key === 'path') {
    return `<label>${esc(f.label)}</label>
      <div class="row-actions" style="flex-wrap:nowrap">
        <input type="text" data-field="${k}" data-kind="text" value="${esc(val)}" style="flex:1;min-width:0" />
        <button type="button" id="uploadAssetBtn" style="padding:4px 7px;font-size:11px;white-space:nowrap">Nahrát...</button>
      </div>`;
  }
  return `<label>${esc(f.label)}</label><input type="text" data-field="${k}" data-kind="text" value="${esc(val)}" />`;
}

function renderInspector() {
  const wrap = $('inspector');
  if (selectedIdx === null || !meta || !meta.widgets || !meta.widgets[selectedIdx]) {
    wrap.innerHTML = `<div class="empty-insp">
      <div><strong>Klikni na libovolný widget na plátně</strong> nebo v seznamu vrstev nahoře pro jeho úpravu.</div>
      <div class="src">Můžeš ho chytit myší a přesunout, tahat za rohy pro zvětšení/zmenšení, nebo přidat nový widget.</div>
      <button class="primary" onclick="openGallery()">+ Přidat nový widget</button>
    </div>`;
    return;
  }

  const w = meta.widgets[selectedIdx];
  const spec = w.spec || {};
  const cat = (meta.widget_catalog || []).find(c => c.type === w.type);
  const r = w.rect || { x: 0, y: 0, w: 200, h: 100 };
  const isBox = w.resize_mode !== 'text';
  const opacityPct = Math.round((spec.opacity !== undefined ? Number(spec.opacity) : 1.0) * 100);
  const panelOn = spec.panel !== false;
  const cornerRad = spec.corner_radius !== undefined ? Number(spec.corner_radius) : 12;

  const specificHtml = cat && cat.fields && cat.fields.length
    ? `<div class="insp-card">
         <div class="insp-title"><span>${esc(cat.icon || '')} ${esc(cat.label)}</span><span class="mono" style="color:var(--dim)">${esc(w.type)}</span></div>
         <div class="prop-grid">${cat.fields.map(f => renderFieldControl(f, spec, w.type)).join('')}</div>
       </div>`
    : '';

  wrap.innerHTML = `
    <div class="insp-card">
      <div class="insp-title">
        <span>Pozice a velikost (1920×1080)</span>
        <div class="row-actions">
          <button type="button" class="ghost" id="centerHBtn" title="Zarovnat vodorovně na střed" style="font-size:11px">↔ Střed</button>
          <button type="button" class="ghost" id="centerVBtn" title="Zarovnat svisle na střed" style="font-size:11px">↕ Střed</button>
        </div>
      </div>
      <div class="xywh-grid">
        <div class="xywh-cell"><span>X</span><input type="number" id="geoX" value="${Math.round(r.x)}" /></div>
        <div class="xywh-cell"><span>Y</span><input type="number" id="geoY" value="${Math.round(r.y)}" /></div>
        <div class="xywh-cell"><span>Šířka</span><input type="number" id="geoW" value="${Math.round(r.w)}" ${isBox ? '' : 'disabled title="U textového widgetu měň velikost písma nebo tahej za roh"'} /></div>
        <div class="xywh-cell"><span>Výška</span><input type="number" id="geoH" value="${Math.round(r.h)}" ${isBox ? '' : 'disabled title="U textového widgetu měň velikost písma nebo tahej za roh"'} /></div>
      </div>
      <div class="prop-grid">
        <label>Název vrstvy</label>
        <input type="text" data-field="name" data-kind="text" value="${esc(spec.name || w.name)}" />
      </div>
    </div>

    ${specificHtml}

    <div class="insp-card">
      <div class="insp-title"><span>Vzhled a pozadí</span></div>
      <div class="prop-grid">
        <label>Průhlednost</label>
        <div class="range-wrap">
          <input type="range" id="opacityRange" min="10" max="100" step="5" value="${opacityPct}" />
          <span class="range-val">${opacityPct}%</span>
        </div>
        <label>Pozadí karty (Panel)</label>
        <label class="check-row"><input type="checkbox" data-field="panel" data-kind="bool" ${panelOn ? 'checked' : ''} /><span>${panelOn ? 'Zobrazeno' : 'Průhledné'}</span></label>
        <label>Zaoblení rohů</label>
        <div class="range-wrap">
          <input type="range" data-field="corner_radius" data-kind="range" min="0" max="40" step="2" value="${cornerRad}" />
          <span class="range-val">${cornerRad}</span>
        </div>
        <label>Barva panelu</label>
        <div class="color-wrap">
          <input type="color" data-field="panel_color" data-kind="color_panel" value="${toHexColor(spec.panel_color, '#0c111d')}" />
          <span class="mono" style="color:var(--dim);font-size:11px">${spec.panel_color ? esc(spec.panel_color) : 'motiv'}</span>
          ${spec.panel_color ? '<button type="button" class="ghost" data-clear="panel_color" style="margin-left:auto;font-size:11px">Reset</button>' : ''}
        </div>
        <label>Zobrazit od (s)</label>
        <input type="number" step="0.1" data-field="show_from" data-kind="number" value="${spec.show_from ?? ''}" placeholder="celý klip" />
        <label>Zobrazit do (s)</label>
        <input type="number" step="0.1" data-field="show_to" data-kind="number" value="${spec.show_to ?? ''}" placeholder="celý klip" />
      </div>
    </div>
  `;

  bindInspectorEvents(w);
}

function bindInspectorEvents(w) {
  const idx = w.index;
  const dw = meta.design_width || 1920;
  const dh = meta.design_height || 1080;

  // Geometry inputs
  const commitGeo = () => {
    const nx = Number($('geoX').value);
    const ny = Number($('geoY').value);
    if (w.resize_mode === 'text') {
      const dx = nx - (w.rect ? w.rect.x : 0);
      const dy = ny - (w.rect ? w.rect.y : 0);
      const orig = w.origin || { x: nx, y: ny };
      updateWidgetOnServer(idx, { x: Math.round(orig.x + dx), y: Math.round(orig.y + dy) });
    } else {
      const nw = Math.max(40, Number($('geoW').value));
      const nh = Math.max(20, Number($('geoH').value));
      updateWidgetOnServer(idx, { x: Math.round(nx), y: Math.round(ny), width: Math.round(nw), height: Math.round(nh), anchor: 'top-left' });
    }
  };
  ['geoX', 'geoY', 'geoW', 'geoH'].forEach(id => {
    const el = $(id);
    if (el && !el.disabled) el.addEventListener('change', commitGeo);
  });

  $('centerHBtn')?.addEventListener('click', () => {
    if (!w.rect) return;
    const newX = Math.round((dw - w.rect.w) / 2);
    if (w.resize_mode === 'text' && w.origin) {
      updateWidgetOnServer(idx, { x: Math.round(w.origin.x + (newX - w.rect.x)) });
    } else {
      updateWidgetOnServer(idx, { x: newX, width: Math.round(w.rect.w), height: Math.round(w.rect.h), anchor: 'top-left' });
    }
  });
  $('centerVBtn')?.addEventListener('click', () => {
    if (!w.rect) return;
    const newY = Math.round((dh - w.rect.h) / 2);
    if (w.resize_mode === 'text' && w.origin) {
      updateWidgetOnServer(idx, { y: Math.round(w.origin.y + (newY - w.rect.y)) });
    } else {
      updateWidgetOnServer(idx, { y: newY, width: Math.round(w.rect.w), height: Math.round(w.rect.h), anchor: 'top-left' });
    }
  });

  // Opacity slider
  const opEl = $('opacityRange');
  if (opEl) {
    opEl.addEventListener('input', () => {
      opEl.nextElementSibling.textContent = opEl.value + '%';
      scheduleLivePatch(idx, { opacity: Number(opEl.value) / 100 });
    });
    opEl.addEventListener('change', () => updateWidgetOnServer(idx, { opacity: Number(opEl.value) / 100 }, { pushHistory: false }));
  }

  // Reset color buttons
  $('inspector').querySelectorAll('button[data-clear]').forEach(btn => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.clear;
      if (w.type === 'graph' && k === 'series_color') {
        updateWidgetOnServer(idx, { series_color: '' });
      } else {
        updateWidgetOnServer(idx, {}, { unset: [k] });
      }
    });
  });

  // Upload asset button for image widget
  $('uploadAssetBtn')?.addEventListener('click', () => {
    pendingAssetTarget = idx;
    $('assetInput').click();
  });

  // Generic declarative field handlers
  $('inspector').querySelectorAll('[data-field]').forEach(el => {
    const field = el.dataset.field;
    const kind = el.dataset.kind;
    if (kind === 'range') {
      el.addEventListener('input', () => {
        el.nextElementSibling.textContent = el.value;
        scheduleLivePatch(idx, { [field]: Number(el.value) });
      });
      el.addEventListener('change', () => updateWidgetOnServer(idx, { [field]: Number(el.value) }, { pushHistory: false }));
    } else if (kind === 'color' || kind === 'color_panel') {
      const fmtVal = v => kind === 'color_panel' ? (v + ' @ 82%') : v;
      el.addEventListener('input', () => scheduleLivePatch(idx, { [field]: fmtVal(el.value) }));
      el.addEventListener('change', () => updateWidgetOnServer(idx, { [field]: fmtVal(el.value) }, { pushHistory: false }));
    } else if (kind === 'bool') {
      el.addEventListener('change', () => updateWidgetOnServer(idx, { [field]: el.checked }));
    } else if (kind === 'number') {
      el.addEventListener('change', () => {
        const raw = el.value.trim();
        if (raw === '') updateWidgetOnServer(idx, {}, { unset: [field] });
        else updateWidgetOnServer(idx, { [field]: Number(raw) });
      });
    } else {
      el.addEventListener('change', () => {
        const val = el.value;
        if (kind === 'opt_channel' && val === '') updateWidgetOnServer(idx, {}, { unset: [field] });
        else updateWidgetOnServer(idx, { [field]: val });
      });
    }
  });
}

// =========================================================================
// Widget Gallery Modal ("+ Přidat widget")
// =========================================================================

function renderGallery(query = '') {
  const body = $('galleryBody');
  const q = query.trim().toLowerCase();
  const catList = (meta && meta.widget_catalog) || [];
  const groups = new Map();
  for (const item of catList) {
    if (q) {
      const hay = `${item.label} ${item.type} ${item.short} ${item.category}`.toLowerCase();
      if (!hay.includes(q)) continue;
    }
    if (!groups.has(item.category)) groups.set(item.category, []);
    groups.get(item.category).push(item);
  }
  if (!groups.size) {
    body.innerHTML = '<div class="src" style="padding:24px;text-align:center">Nic nenalezeno.</div>';
    return;
  }
  let html = '';
  for (const [catName, items] of groups.entries()) {
    html += `<div><div class="cat-title">${esc(catName)}</div><div class="gallery-grid">`;
    for (const item of items) {
      const dis = item.available ? '' : 'disabled';
      const badge = !item.available
        ? `<span class="pill-miss">${esc(item.missing_reason || 'Nedostupné')}</span>`
        : (item.recommended ? '<span class="pill-rec">Doporučeno</span>' : '');
      html += `<button class="w-card ${dis}" data-add="${esc(item.type)}" ${item.available ? '' : 'disabled'}>
        <div class="w-card-top"><span class="w-card-ico">${esc(item.icon)}</span><span>${esc(item.label)}</span>${badge}</div>
        <div class="w-card-desc">${esc(item.short)}</div>
      </button>`;
    }
    html += '</div></div>';
  }
  body.innerHTML = html;
}

window.openGallery = () => {
  $('gallerySearch').value = '';
  renderGallery('');
  $('galleryModal').classList.add('open');
  setTimeout(() => $('gallerySearch').focus(), 30);
};

function closeGallery() {
  $('galleryModal').classList.remove('open');
}

$('addWidgetTopBtn').addEventListener('click', openGallery);
$('addWidgetSideBtn').addEventListener('click', openGallery);
$('closeGalleryBtn').addEventListener('click', closeGallery);
$('galleryModal').addEventListener('click', (e) => { if (e.target === $('galleryModal')) closeGallery(); });
$('gallerySearch').addEventListener('input', (e) => renderGallery(e.target.value));
$('galleryBody').addEventListener('click', async (e) => {
  const card = e.target.closest('.w-card[data-add]');
  if (!card || card.disabled) return;
  const wtype = card.dataset.add;
  closeGallery();
  switchTab('editorTab');
  await widgetApi('/api/widgets/add', { type: wtype });
});

// =========================================================================
// Toolbar, Undo/Redo, Keyboard Shortcuts, Playback & File Controls
// =========================================================================

$('undoBtn').addEventListener('click', async () => {
  if (!undoStack.length || !meta) return;
  const prev = undoStack.pop();
  redoStack.push({ yaml: meta.yaml, selectedIdx });
  await applyConfig({ yaml: prev.yaml }, { pushHistory: false, keepSelected: prev.selectedIdx });
});

$('redoBtn').addEventListener('click', async () => {
  if (!redoStack.length || !meta) return;
  const next = redoStack.pop();
  undoStack.push({ yaml: meta.yaml, selectedIdx });
  await applyConfig({ yaml: next.yaml }, { pushHistory: false, keepSelected: next.selectedIdx });
});

$('editToggleBtn').addEventListener('click', () => {
  editMode = !editMode;
  $('editToggleBtn').classList.toggle('toggle-on', editMode);
  $('editorLayer').classList.toggle('edit-off', !editMode);
});

$('snapToggleBtn').addEventListener('click', () => {
  snapEnabled = !snapEnabled;
  $('snapToggleBtn').classList.toggle('toggle-on', snapEnabled);
});

$('gridToggleBtn').addEventListener('click', () => {
  gridEnabled = !gridEnabled;
  $('gridToggleBtn').classList.toggle('toggle-on', gridEnabled);
  $('gridOverlay').classList.toggle('show', gridEnabled);
});

$('dupSelBtn').addEventListener('click', () => {
  if (selectedIdx !== null) widgetApi('/api/widgets/duplicate', { index: selectedIdx });
});
$('delSelBtn').addEventListener('click', () => {
  if (selectedIdx !== null) widgetApi('/api/widgets/delete', { index: selectedIdx }, { keepSelected: null });
});
$('clearAllBtn').addEventListener('click', () => {
  if (meta && meta.widgets && meta.widgets.length) {
    widgetApi('/api/widgets/clear', {}, { keepSelected: null });
  }
});

// Global keyboard shortcuts (when not typing in a field)
window.addEventListener('keydown', (e) => {
  const tag = (document.activeElement && document.activeElement.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
  if (e.key === 'Escape') {
    if ($('galleryModal').classList.contains('open')) { closeGallery(); return; }
    if (!typing) selectWidget(null);
    return;
  }
  if (typing) return;

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    if (e.shiftKey) $('redoBtn').click(); else $('undoBtn').click();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
    e.preventDefault(); $('redoBtn').click(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'd' && selectedIdx !== null) {
    e.preventDefault(); $('dupSelBtn').click(); return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selectedIdx !== null) {
    e.preventDefault(); $('delSelBtn').click(); return;
  }
  if (e.key === ' ' && !$('galleryModal').classList.contains('open')) {
    e.preventDefault(); $('playBtn').click(); return;
  }
  if (selectedIdx !== null && meta && meta.widgets[selectedIdx] && ['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)) {
    e.preventDefault();
    const step = e.shiftKey ? 20 : 4;
    const dx = e.key === 'ArrowLeft' ? -step : (e.key === 'ArrowRight' ? step : 0);
    const dy = e.key === 'ArrowUp' ? -step : (e.key === 'ArrowDown' ? step : 0);
    const w = meta.widgets[selectedIdx];
    if (!w.rect) return;
    if (w.resize_mode === 'text' && w.origin) {
      updateWidgetOnServer(selectedIdx, { x: Math.round(w.origin.x + dx), y: Math.round(w.origin.y + dy) });
    } else {
      updateWidgetOnServer(selectedIdx, {
        x: Math.round(w.rect.x + dx), y: Math.round(w.rect.y + dy),
        width: Math.round(w.rect.w), height: Math.round(w.rect.h), anchor: 'top-left',
      });
    }
  }
});

$('slider').addEventListener('input', updateFrame);
$('playBtn').addEventListener('click', () => {
  playing = !playing;
  $('playBtn').textContent = playing ? 'Pause' : 'Play';
  if (playing) {
    timer = setInterval(() => {
      let v = Number($('slider').value) + 8;
      if (v > 1000) v = 0;
      $('slider').value = v;
      updateFrame();
    }, 120);
  } else { clearInterval(timer); }
});

$('bgToggle').addEventListener('click', () => {
  darkBg = !darkBg;
  $('canvasBox').classList.toggle('dark-bg', darkBg);
  $('bgToggle').textContent = darkBg ? 'Pozadí: Tmavé' : 'Pozadí: Šachovnice';
});

async function uploadFile(file) {
  if (!file) return;
  showEditorError('');
  $('sourceLabel').textContent = `Načítám ${file.name} ...`;
  try {
    const res = await fetch('/api/upload?filename=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await res.json();
    if (!res.ok) {
      showEditorError(data.error || 'Soubor se nepodařilo načíst');
      return;
    }
    undoStack = []; redoStack = [];
    applyMetaToUI(data, { resetSlider: true, selectedIdx: null });
  } catch (err) {
    showEditorError(String(err));
  }
}

$('openFileBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', (e) => {
  if (e.target.files && e.target.files[0]) uploadFile(e.target.files[0]);
  e.target.value = '';
});

$('assetInput').addEventListener('change', async (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = '';
  if (!f || pendingAssetTarget === null) return;
  const res = await fetch('/api/upload_asset?filename=' + encodeURIComponent(f.name), {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: f,
  });
  const data = await res.json();
  if (res.ok && data.path) {
    await updateWidgetOnServer(pendingAssetTarget, { path: data.path });
  } else {
    showEditorError(data.error || 'Nepodařilo se nahrát obrázek');
  }
  pendingAssetTarget = null;
});

const box = $('canvasBox');
const isFileDrag = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
['dragenter', 'dragover'].forEach(ev => box.addEventListener(ev, (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault(); box.classList.add('drag-over');
}));
['dragleave', 'drop'].forEach(ev => box.addEventListener(ev, (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault(); box.classList.remove('drag-over');
}));
box.addEventListener('drop', (e) => {
  if (!isFileDrag(e)) return;
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

$('sampleSel').addEventListener('change', async (e) => {
  const name = e.target.value;
  if (!name) return;
  const res = await fetch('/api/sample', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name }),
  });
  const data = await res.json();
  if (res.ok) {
    undoStack = []; redoStack = [];
    applyMetaToUI(data, { resetSlider: true, selectedIdx: null });
  }
});

async function applyConfig(payload, { pushHistory = true, keepSelected = undefined } = {}) {
  $('errBox').style.display = 'none';
  if (pushHistory) pushUndo();
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ...payload, t: currentTime() }),
  });
  const data = await res.json();
  if (!res.ok) {
    if (pushHistory && undoStack.length) undoStack.pop();
    updateUndoButtons();
    $('errBox').textContent = data.error || 'Chyba konfigurace';
    $('errBox').style.display = 'block';
    showEditorError(data.error || 'Chyba konfigurace');
    return;
  }
  applyMetaToUI(data, { selectedIdx: keepSelected !== undefined ? keepSelected : selectedIdx });
}

$('applyBtn').addEventListener('click', () => applyConfig({ yaml: $('yamlEdit').value }));
$('presetSel').addEventListener('change', (e) => {
  if (e.target.value !== '__custom__') applyConfig({ preset: e.target.value }, { keepSelected: null });
});
$('themeSel').addEventListener('change', (e) => applyConfig({ theme: e.target.value }));
$('downloadYaml').addEventListener('click', () => {
  const blob = new Blob([$('yamlEdit').value], { type: 'text/yaml' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'overlay.yaml'; a.click();
});

$('savePngBtn').addEventListener('click', () => {
  const t = currentTime();
  const a = document.createElement('a');
  a.href = `/api/frame?t=${t.toFixed(4)}&full=1`;
  a.download = `overlay_${t.toFixed(2)}s.png`;
  a.click();
});

$('renderVideoBtn').addEventListener('click', async () => {
  const btn = $('renderVideoBtn');
  $('renderErr').style.display = 'none';
  $('renderOk').style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Renderuji video... (čekejte prosím)';
  try {
    const res = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        resolution: $('resSel').value,
        fps: Number($('fpsSel').value),
        codec: $('codecSel').value,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      $('renderErr').textContent = data.error || 'Render selhal';
      $('renderErr').style.display = 'block';
      return;
    }
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const filename = m ? m[1] : 'telemetry_overlay.mov';
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    $('renderOk').textContent = `Vyrenderováno ${filename} (${(blob.size / 1e6).toFixed(2)} MB)`;
    $('renderOk').style.display = 'block';
  } catch (err) {
    $('renderErr').textContent = String(err);
    $('renderErr').style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Renderovat a stáhnout video';
  }
});

document.querySelectorAll('.tabs button').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

init();
</script>
</body>
</html>
"""


def _list_samples() -> list[str]:
    if not _EXAMPLES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in _EXAMPLES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _SAMPLE_EXTENSIONS
    )


def _default_sample_path() -> Path | None:
    if not _EXAMPLES_DIR.is_dir():
        return None
    for preferred in ("rocket_flight.csv", "static_fire_thrust.csv", "circuit_lap.gpx"):
        p = _EXAMPLES_DIR / preferred
        if p.is_file():
            return p
    samples = _list_samples()
    return (_EXAMPLES_DIR / samples[0]) if samples else None


def _synthetic_demo_telemetry() -> Telemetry:
    """Fallback in-memory rocket demo telemetry when no example files are bundled."""
    t = np.linspace(0.0, 12.0, 241)
    thrust = np.where((t >= 0.5) & (t <= 3.0), 220.0 * np.sin(np.pi * (t - 0.5) / 2.5) ** 0.7, 0.0)
    alt = np.maximum(0.0, 480.0 * np.sin(np.clip(t / 12.0, 0.0, 1.0) * np.pi * 0.85))
    pitch = 4.0 + 18.0 * np.clip((t - 1.0) / 8.0, 0.0, 1.0)
    roll = 8.0 * np.sin(t * 1.1)
    yaw = (t * 25.0) % 360.0
    return Telemetry(
        time=t,
        channels={
            "thrust": Channel("thrust", "Thrust", "N", thrust),
            "altitude": Channel("altitude", "Altitude", "m", alt),
            "pitch": Channel("pitch", "Pitch", "deg", pitch),
            "roll": Channel("roll", "Roll", "deg", roll),
            "yaw": Channel("yaw", "Yaw", "deg", yaw),
        },
        source="demo_rocket_flight.csv",
    )


def _clone_telemetry(tel: Telemetry) -> Telemetry:
    """Shallow-copy Telemetry with fresh numpy arrays so filters never mutate raw data."""
    new_channels = {}
    for k, ch in tel.channels.items():
        new_channels[k] = Channel(
            key=ch.key,
            label=ch.label,
            unit=ch.unit,
            values=ch.values.copy(),
            kind=ch.kind,
            text_values=ch.text_values.copy() if ch.text_values is not None else None,
        )
    return Telemetry(
        time=tel.time.copy(),
        channels=new_channels,
        meta=copy.deepcopy(tel.meta),
        source=tel.source,
        start_epoch=tel.start_epoch,
    )


def _load_config_from_yaml_text(yaml_text: str) -> Config:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
        tf.write(yaml_text)
        tmp_p = Path(tf.name)
    try:
        return load_config(tmp_p)
    finally:
        tmp_p.unlink(missing_ok=True)


def _build_initial_config(telemetry: Telemetry, config_path: str | Path | None = None) -> tuple[str, Config]:
    if config_path is not None:
        cfg_p = Path(config_path)
        yaml_text = cfg_p.read_text(encoding="utf-8")
        return yaml_text, load_config(cfg_p)
    suggested = suggest_config(telemetry)
    yaml_text = yaml.safe_dump(suggested, sort_keys=False, allow_unicode=True)
    return yaml_text, _load_config_from_yaml_text(yaml_text)


class _StudioState:
    def __init__(
        self,
        telemetry: Telemetry,
        config_yaml: str,
        config: Config,
        source_path: Path | None = None,
    ):
        self.lock = threading.Lock()
        self.raw_telemetry = telemetry
        self.telemetry = telemetry
        self.source_path = source_path
        self.yaml_text = config_yaml
        self.config = config
        self.upload_dir = tempfile.TemporaryDirectory(prefix="telemetry_studio_")
        self._default_icon_path: Path | None = None
        self.renderer = self._make_renderer(config)

    def close(self) -> None:
        self.upload_dir.cleanup()

    def ensure_default_icon(self) -> Path:
        if self._default_icon_path is None or not self._default_icon_path.is_file():
            icons = generate_app_icon(Path(self.upload_dir.name) / "assets")
            self._default_icon_path = icons["png"]
        return self._default_icon_path

    def design_size(self, cfg: Config | None = None) -> tuple[int, int]:
        c = cfg or self.config
        dw = DESIGN_WIDTH
        dh = max(100, int(round(DESIGN_WIDTH * c.height / max(1, c.width))))
        return dw, dh

    def preview_size(self, cfg: Config | None = None) -> tuple[int, int]:
        # Render internally at design resolution (1920x1080 for 16:9) so widgets
        # with pixel widths in bundled presets match final 1080p output 1:1.
        return self.design_size(cfg)

    def _make_renderer(self, cfg: Config) -> OverlayRenderer:
        pw, ph = self.preview_size(cfg)
        preview_cfg = Config(**{**cfg.__dict__, "width": pw, "height": ph})
        tel = _clone_telemetry(self.raw_telemetry) if (cfg.filters or cfg.derived) else self.raw_telemetry
        self.telemetry = tel
        return OverlayRenderer(preview_cfg, tel)

    def commit_yaml_doc(self, doc: dict[str, Any]) -> None:
        new_yaml = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        new_cfg = _load_config_from_yaml_text(new_yaml)
        new_renderer = self._make_renderer(new_cfg)
        self.yaml_text = new_yaml
        self.config = new_cfg
        self.renderer = new_renderer

    def parsed_doc(self) -> dict[str, Any]:
        doc = yaml.safe_load(self.yaml_text)
        if not isinstance(doc, dict):
            doc = {}
        doc.setdefault("widgets", [])
        if not isinstance(doc["widgets"], list):
            doc["widgets"] = []
        return doc

    def load_new_telemetry(
        self,
        telemetry: Telemetry,
        source_path: Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        yaml_text, cfg = _build_initial_config(telemetry, config_path)
        self.raw_telemetry = telemetry
        self.telemetry = telemetry
        self.source_path = source_path
        self.yaml_text = yaml_text
        self.config = cfg
        self.renderer = self._make_renderer(cfg)

    def state_dict(self, data_t: float | None = None) -> dict[str, Any]:
        presets = sorted(p.stem for p in _PRESET_DIR.glob("*.y*ml")) if _PRESET_DIR.is_dir() else []
        channels = []
        for ch in self.telemetry.channels.values():
            st = ch.describe()
            if ch.kind == "text":
                lo, hi = ", ".join(st.get("unique", [])[:2]), ""
            else:
                lo = f"{st['min']:.4g}" if st else "-"
                hi = f"{st['max']:.4g}" if st else "-"
            channels.append({"key": ch.key, "label": ch.label, "unit": ch.unit, "min": lo, "max": hi})

        dw, dh = self.design_size()
        t_probe = (
            float(data_t)
            if data_t is not None
            else (self.renderer.timeline.data_start + self.renderer.timeline.data_end) * 0.5
        )
        widgets_meta = measure_widget_rects(
            self.renderer, self.config.widgets, t_probe, design_w=dw, design_h=dh,
        )

        return {
            "version": __version__,
            "source": Path(self.telemetry.source).name,
            "samples": int(self.telemetry.time.size),
            "sample_rate": float(self.telemetry.sample_rate),
            "t0": float(self.renderer.timeline.data_start),
            "t1": float(self.renderer.timeline.data_end),
            "design_width": dw,
            "design_height": dh,
            "theme": self.config.theme.name,
            "presets": presets,
            "samples_list": _list_samples(),
            "themes": sorted(THEMES),
            "export_formats": sorted(EXPORT_FORMATS),
            "has_ffmpeg": find_ffmpeg() is not None,
            "channels": channels,
            "widgets": widgets_meta,
            "widget_catalog": widget_catalog_for_telemetry(self.telemetry),
            "events": [
                {"time": float(e["time"]), "label": str(e.get("label", ""))}
                for e in self.renderer.events
            ],
            "yaml": self.yaml_text,
        }


def create_studio_server(
    data_path: str | Path | None = None,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create (without blocking) the Studio HTTP server for interactive visual editing.

    If ``data_path`` is omitted, loads a bundled rocket flight example (or a
    synthetic in-memory demo) so the app opens immediately ready to use.
    """
    resolved_src: Path | None = None
    if data_path is not None:
        resolved_src = Path(data_path)
        telemetry = load_data(resolved_src)
    else:
        sample = _default_sample_path()
        if sample is not None:
            resolved_src = sample
            telemetry = load_data(sample)
        else:
            telemetry = _synthetic_demo_telemetry()

    yaml_text, cfg = _build_initial_config(telemetry, config_path)
    state = _StudioState(telemetry, yaml_text, cfg, source_path=resolved_src)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send_json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream_upload(self, dest: Path, length: int) -> None:
            remaining = length
            with dest.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path in ("/", "/index.html"):
                body = _STUDIO_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in ("/favicon.svg", "/favicon.ico"):
                body = _FAVICON_SVG.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                t_val = float(qs["t"][0]) if "t" in qs else None
                with state.lock:
                    self._send_json(200, state.state_dict(data_t=t_val))
                return

            if parsed.path == "/api/samples":
                self._send_json(200, {"samples": _list_samples()})
                return

            if parsed.path == "/api/frame":
                t = float(qs.get("t", ["0"])[0])
                full = qs.get("full", ["0"])[0] in ("1", "true", "yes")
                with state.lock:
                    if full and state.renderer.size != (state.config.width, state.config.height):
                        tel = _clone_telemetry(state.raw_telemetry) if (state.config.filters or state.config.derived) else state.raw_telemetry
                        full_renderer = OverlayRenderer(state.config, tel)
                        img = full_renderer.render_at(t, frame=0)
                    else:
                        img = state.renderer.render_at(t, frame=0)
                if not full and img.width > 1280:
                    from PIL import Image as _PILImage
                    nh = max(1, int(round(img.height * 1280 / img.width)))
                    img = img.resize((1280, nh), _PILImage.Resampling.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, "PNG", compress_level=3 if full else 1)
                raw = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                if full:
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="overlay_{t:.2f}s.png"',
                    )
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if parsed.path == "/api/export":
                fmt = (qs.get("format", ["csv"])[0]).lower()
                try:
                    with tempfile.TemporaryDirectory() as td:
                        out_p = Path(td) / f"telemetry.{fmt}"
                        with state.lock:
                            export_telemetry(state.telemetry, out_p, fmt=fmt)
                        raw = out_p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="telemetry.{fmt}"')
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", "0"))

            if parsed.path == "/api/upload":
                raw_name = qs.get("filename", ["upload.csv"])[0]
                safe_name = Path(raw_name).name or "upload.csv"
                dest = Path(state.upload_dir.name) / safe_name
                try:
                    self._stream_upload(dest, length)
                    tel = load_data(dest)
                    with state.lock:
                        state.load_new_telemetry(tel, source_path=dest)
                        self._send_json(200, state.state_dict())
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/upload_asset":
                raw_name = qs.get("filename", ["logo.png"])[0]
                safe_name = Path(raw_name).name or "logo.png"
                assets_dir = Path(state.upload_dir.name) / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                dest = assets_dir / safe_name
                try:
                    self._stream_upload(dest, length)
                    self._send_json(200, {"path": str(dest)})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            raw = self.rfile.read(length)

            if parsed.path == "/api/sample":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    name = Path(str(body.get("name", ""))).name
                    target = _EXAMPLES_DIR / name
                    if not target.is_file():
                        raise FileNotFoundError(f"sample not found: {name}")
                    tel = load_data(target)
                    with state.lock:
                        state.load_new_telemetry(tel, source_path=target)
                        self._send_json(200, state.state_dict())
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/config":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    t_val = body.get("t")
                    with state.lock:
                        if "preset" in body:
                            preset_p = _PRESET_DIR / f"{body['preset']}.yaml"
                            new_yaml = preset_p.read_text(encoding="utf-8")
                        elif "theme" in body:
                            parsed_cfg = yaml.safe_load(state.yaml_text) or {}
                            parsed_cfg.setdefault("theme", {})["base"] = body["theme"]
                            new_yaml = yaml.safe_dump(parsed_cfg, sort_keys=False, allow_unicode=True)
                        else:
                            new_yaml = str(body.get("yaml", ""))
                        new_cfg = _load_config_from_yaml_text(new_yaml)
                        new_renderer = state._make_renderer(new_cfg)
                        state.yaml_text = new_yaml
                        state.config = new_cfg
                        state.renderer = new_renderer
                        self._send_json(200, state.state_dict(data_t=t_val))
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            # --- Visual Widget Editor Endpoints ---
            if parsed.path == "/api/widgets/add":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    wtype = str(body.get("type", "")).strip()
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        dw, dh = state.design_size()
                        icon_p = state.ensure_default_icon() if wtype == "image" else None
                        new_spec = default_widget_spec(
                            wtype, state.telemetry, doc["widgets"],
                            design_w=dw, design_h=dh, icon_path=icon_p,
                        )
                        if isinstance(body.get("overrides"), dict):
                            new_spec.update(body["overrides"])
                        doc["widgets"].append(new_spec)
                        state.commit_yaml_doc(doc)
                        resp = state.state_dict(data_t=t_val)
                        resp["selected_index"] = len(doc["widgets"]) - 1
                        self._send_json(200, resp)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/widgets/update":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    idx = int(body["index"])
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        widgets = doc["widgets"]
                        if not 0 <= idx < len(widgets):
                            raise IndexError(f"widget index out of range: {idx}")
                        dw, dh = state.design_size()
                        if isinstance(body.get("spec"), dict):
                            updated = copy.deepcopy(body["spec"])
                        else:
                            updated = apply_widget_patch(
                                widgets[idx],
                                patch=body.get("patch"),
                                unset=body.get("unset"),
                                resize_ratio=body.get("resize_ratio"),
                                design_w=dw,
                                design_h=dh,
                            )
                        widgets[idx] = updated
                        state.commit_yaml_doc(doc)
                        resp = state.state_dict(data_t=t_val)
                        resp["selected_index"] = idx
                        self._send_json(200, resp)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/widgets/delete":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    idx = int(body["index"])
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        widgets = doc["widgets"]
                        if not 0 <= idx < len(widgets):
                            raise IndexError(f"widget index out of range: {idx}")
                        widgets.pop(idx)
                        state.commit_yaml_doc(doc)
                        self._send_json(200, state.state_dict(data_t=t_val))
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/widgets/duplicate":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    idx = int(body["index"])
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        widgets = doc["widgets"]
                        if not 0 <= idx < len(widgets):
                            raise IndexError(f"widget index out of range: {idx}")
                        dw, dh = state.design_size()
                        rects = measure_widget_rects(
                            state.renderer, state.config.widgets,
                            float(t_val) if t_val is not None else state.renderer.timeline.data_start,
                            design_w=dw, design_h=dh,
                        )
                        dup = copy.deepcopy(widgets[idx])
                        base_name = str(dup.get("name", dup.get("type", "widget")))
                        dup["name"] = unique_widget_name(base_name, widgets)
                        info = rects[idx] if idx < len(rects) else None
                        if info and info.get("origin"):
                            nx = min(dw - 60.0, info["origin"]["x"] + 32.0)
                            ny = min(dh - 40.0, info["origin"]["y"] + 32.0)
                            dup["x"] = f"{nx / max(1.0, float(dw)) * 100.0:.2f}%"
                            dup["y"] = f"{ny / max(1.0, float(dh)) * 100.0:.2f}%"
                        widgets.insert(idx + 1, dup)
                        state.commit_yaml_doc(doc)
                        resp = state.state_dict(data_t=t_val)
                        resp["selected_index"] = idx + 1
                        self._send_json(200, resp)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/widgets/reorder":
                try:
                    body = json.loads(raw.decode("utf-8"))
                    src_i = int(body["from_index"])
                    dst_i = int(body["to_index"])
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        widgets = doc["widgets"]
                        if not (0 <= src_i < len(widgets) and 0 <= dst_i < len(widgets)):
                            raise IndexError("reorder index out of range")
                        item = widgets.pop(src_i)
                        widgets.insert(dst_i, item)
                        state.commit_yaml_doc(doc)
                        resp = state.state_dict(data_t=t_val)
                        resp["selected_index"] = dst_i
                        self._send_json(200, resp)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/widgets/clear":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    t_val = body.get("t")
                    with state.lock:
                        doc = state.parsed_doc()
                        doc["widgets"] = []
                        state.commit_yaml_doc(doc)
                        self._send_json(200, state.state_dict(data_t=t_val))
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            if parsed.path == "/api/render":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    codec_name = str(body.get("codec", "qtrle")).lower()
                    if codec_name not in VIDEO_CODECS:
                        raise ValueError(f"Unknown codec: {codec_name}")
                    res_str = str(body.get("resolution", "1080p"))
                    width, height = parse_resolution(res_str)
                    fps = float(body.get("fps", 30))
                    spec = codec_for_path(f"out{VIDEO_CODECS[codec_name].suffix}", codec_name)

                    with state.lock:
                        render_cfg = Config(**{
                            **state.config.__dict__,
                            "width": width,
                            "height": height,
                            "fps": fps,
                        })
                        telemetry = (
                            _clone_telemetry(state.raw_telemetry)
                            if (render_cfg.filters or render_cfg.derived)
                            else state.raw_telemetry
                        )
                        src_stem = Path(telemetry.source).stem
                        source_path = state.source_path

                    out_name = f"{src_stem}_overlay{spec.suffix}"
                    with tempfile.TemporaryDirectory() as td:
                        out_path = Path(td) / out_name
                        render_inst = OverlayRenderer(render_cfg, telemetry)
                        export_video(
                            render_cfg,
                            source_path if (source_path and source_path.is_file()) else "memory.csv",
                            out_path,
                            codec=codec_name,
                            workers=1,
                            renderer=render_inst,
                            overwrite=True,
                        )
                        video_bytes = out_path.read_bytes()

                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{out_name}"')
                    self.send_header("Content-Length", str(len(video_bytes)))
                    self.end_headers()
                    self.wfile.write(video_bytes)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": str(exc)})
                return

            self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    orig_close = server.server_close

    def _close_with_cleanup() -> None:
        try:
            state.close()
        finally:
            orig_close()

    server.server_close = _close_with_cleanup  # type: ignore[method-assign]
    return server


def run_studio(
    data_path: str | Path | None = None,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    open_browser: bool = False,
) -> None:
    """Start the local Web Studio server and block until Ctrl+C."""
    server: ThreadingHTTPServer | None = None
    last_err: OSError | None = None
    candidates = [port] if port == 0 else [port + i for i in range(10)]
    for candidate_port in candidates:
        try:
            server = create_studio_server(data_path, config_path, host=host, port=candidate_port)
            break
        except OSError as exc:
            last_err = exc
    if server is None:
        raise last_err or OSError(f"Could not bind Studio server on port {port}")

    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"Telemetry Overlay Studio v{__version__} running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
