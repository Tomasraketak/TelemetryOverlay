"""Zero-dependency local interactive Web Studio for Telemetry Overlay.

Run with:
    telemetry-overlay studio                         # opens with sample rocket flight
    telemetry-overlay studio --open                  # also opens your web browser
    telemetry-overlay studio examples/rocket_flight.csv

Serves a responsive dark-mode browser UI using Python's stdlib ``http.server``:
- Zero-argument startup with bundled samples + drag-and-drop / Open File upload
  (CSV, XLSX, GoPro MP4, DJI SRT/MP4, Garmin FIT, GPX, KML/KMZ, TCX, NMEA, IGC, JSON)
- Live rendered overlay preview over a transparency checkerboard or dark backdrop
- Interactive timeline scrubber, Play/Pause and jump-to-event buttons
- Instant preset & theme switching
- Channel inspector (ranges, units, kinds)
- Live YAML config editor with instant validation & preview
- One-click full-resolution PNG frame & animated overlay video export
- One-click data export (CSV, GPX, KML, GeoJSON, MGJSON, JSON, SRT)
"""

from __future__ import annotations

import io
import json
import os
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
from .config import Config, load_config, parse_resolution, suggest_config
from .data import Channel, Telemetry, load as load_data
from .export import VIDEO_CODECS, codec_for_path, export_video, find_ffmpeg
from .extract import EXPORT_FORMATS, export_telemetry
from .render import OverlayRenderer
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

_STUDIO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Telemetry Overlay Studio</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>
  :root {
    --bg: #0b0f17; --panel: #121826; --panel2: #1a2235;
    --border: rgba(255,255,255,0.09); --text: #f1f5f9; --dim: #94a3b8;
    --accent: #ff9f0a; --good: #22c55e; --bad: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column;
    overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 10px 16px;
    background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }
  header h1 {
    margin: 0; font-size: 15px; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--accent); display: flex; align-items: center; gap: 8px;
  }
  .ver {
    font-size: 10px; color: var(--dim); background: rgba(255,255,255,0.06);
    padding: 2px 6px; border-radius: 999px; font-weight: 600;
  }
  .src { font-size: 12px; color: var(--dim); font-family: ui-monospace, monospace; }
  .spacer { flex: 1; min-width: 8px; }
  select, button, input[type="number"] {
    background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 6px 10px; font-size: 12.5px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: #090d16; font-weight: 600; border: none; }
  button.good { background: var(--good); color: #052e16; font-weight: 600; border: none; }
  button:hover:not(:disabled) { filter: brightness(1.12); }
  button:disabled { opacity: 0.55; cursor: wait; }
  main { flex: 1; display: grid; grid-template-columns: 1fr 420px; min-height: 0; }
  .stage-wrap { display: flex; flex-direction: column; padding: 14px; gap: 12px; min-height: 0; }
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
  .canvas-box.dark-bg { background-image: none; background-color: #0e131e; }
  .canvas-box.drag-over { outline: 2px dashed var(--accent); outline-offset: -6px; }
  .drop-overlay {
    position: absolute; inset: 0; background: rgba(11,15,23,0.85); display: none;
    align-items: center; justify-content: center; flex-direction: column; gap: 8px;
    font-size: 18px; font-weight: 600; color: var(--accent); pointer-events: none; z-index: 5;
  }
  .canvas-box.drag-over .drop-overlay { display: flex; }
  #preview { max-width: 100%; max-height: 100%; object-fit: contain; display: block; }
  .scrub {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; display: flex; flex-direction: column; gap: 8px;
  }
  .scrub-row { display: flex; align-items: center; gap: 10px; }
  input[type="range"] { flex: 1; accent-color: var(--accent); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
  .events { display: flex; gap: 6px; flex-wrap: wrap; }
  .ev-btn {
    font-size: 11px; padding: 4px 8px; border-radius: 999px;
    background: rgba(255,159,10,0.14); border: 1px solid rgba(255,159,10,0.4); color: #ffd089;
  }
  aside {
    background: var(--panel); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
  }
  .tabs { display: flex; border-bottom: 1px solid var(--border); }
  .tabs button {
    flex: 1; border: none; border-radius: 0; padding: 10px 6px;
    background: transparent; color: var(--dim); font-weight: 600; font-size: 12px;
  }
  .tabs button.active { color: var(--accent); border-bottom: 2px solid var(--accent); }
  .tab-pane { flex: 1; display: none; flex-direction: column; padding: 12px; gap: 10px; min-height: 0; overflow: auto; }
  .tab-pane.active { display: flex; }
  textarea {
    flex: 1; width: 100%; resize: none; background: #0a0e17; color: #e2e8f0;
    border: 1px solid var(--border); border-radius: 8px; padding: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45;
  }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--dim); font-weight: 600; }
  .err {
    color: #fca5a5; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35);
    border-radius: 8px; padding: 8px 10px; font-size: 12px; display: none;
  }
  .ok-msg {
    color: #86efac; background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35);
    border-radius: 8px; padding: 8px 10px; font-size: 12px; display: none;
  }
  .row-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .card {
    background: var(--panel2); border: 1px solid var(--border); border-radius: 9px;
    padding: 12px; display: flex; flex-direction: column; gap: 10px;
  }
  .card h3 { margin: 0; font-size: 13px; color: var(--text); letter-spacing: 0.03em; }
  .form-grid { display: grid; grid-template-columns: 110px 1fr; gap: 8px; align-items: center; font-size: 12.5px; }
  .form-grid label { color: var(--dim); }
  @media (max-width: 960px) {
    main { grid-template-columns: 1fr; grid-template-rows: 52vh 1fr; }
    aside { border-left: none; border-top: 1px solid var(--border); }
  }
</style>
</head>
<body>
<header>
  <h1>Telemetry Overlay Studio <span class="ver" id="verTag"></span></h1>
  <button id="openFileBtn" class="primary">Open File...</button>
  <input id="fileInput" type="file" style="display:none"
         accept=".csv,.tsv,.txt,.xlsx,.xlsm,.gpx,.kml,.kmz,.tcx,.fit,.nmea,.igc,.srt,.json,.geojson,.mp4,.mov" />
  <label class="src">Sample: <select id="sampleSel"></select></label>
  <span class="src" id="sourceLabel"></span>
  <span class="spacer"></span>
  <label class="src">Preset: <select id="presetSel"></select></label>
  <label class="src">Theme: <select id="themeSel"></select></label>
  <button id="bgToggle">Backdrop: Checker</button>
</header>
<main>
  <section class="stage-wrap">
    <div class="canvas-box" id="canvasBox">
      <div class="drop-overlay">Drop telemetry file here<span style="font-size:13px;color:#cbd5e1;font-weight:400">CSV · XLSX · GoPro MP4 · DJI SRT · FIT · GPX · KML · IGC · NMEA · JSON</span></div>
      <img id="preview" alt="Overlay Preview" />
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
      <button class="active" data-tab="yamlTab">YAML Config</button>
      <button data-tab="channelsTab">Channels</button>
      <button data-tab="renderTab">Render Video</button>
      <button data-tab="exportTab">Extract Data</button>
    </div>
    <div class="tab-pane active" id="yamlTab">
      <div class="err" id="errBox"></div>
      <textarea id="yamlEdit" spellcheck="false"></textarea>
      <div class="row-actions">
        <button id="applyBtn" class="primary">Apply &amp; Preview</button>
        <button id="downloadYaml">Download YAML</button>
      </div>
    </div>
    <div class="tab-pane" id="channelsTab">
      <table id="chanTable">
        <thead><tr><th>Key</th><th>Unit</th><th>Min</th><th>Max</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="tab-pane" id="renderTab">
      <div class="card">
        <h3>Snapshot (Current Frame)</h3>
        <p class="src" style="margin:0">Download full-resolution transparent PNG at the current playhead timestamp.</p>
        <div class="row-actions">
          <button id="savePngBtn" class="primary">Save Full-Res PNG</button>
        </div>
      </div>
      <div class="card">
        <h3>Animated Overlay Video</h3>
        <div class="form-grid">
          <label for="resSel">Resolution</label>
          <select id="resSel">
            <option value="720p">720p (1280×720)</option>
            <option value="1080p" selected>1080p (1920×1080)</option>
            <option value="1440p">1440p (2560×1440)</option>
            <option value="4k">4K (3840×2160)</option>
            <option value="vertical1080">Vertical 1080×1920</option>
          </select>
          <label for="fpsSel">FPS</label>
          <select id="fpsSel">
            <option value="24">24 fps</option>
            <option value="30" selected>30 fps</option>
            <option value="60">60 fps</option>
          </select>
          <label for="codecSel">Format</label>
          <select id="codecSel">
            <option value="qtrle">.mov — QuickTime RLE (alpha, fast &amp; small)</option>
            <option value="prores4444">.mov — ProRes 4444 (alpha, DaVinci/Premiere)</option>
            <option value="vp9">.webm — VP9 (alpha, web/OBS)</option>
            <option value="gif">.gif — Animated GIF (alpha)</option>
            <option value="webp">.webp — Animated WebP (alpha)</option>
            <option value="h264">.mp4 — H.264 (no alpha, opaque)</option>
          </select>
        </div>
        <div class="err" id="renderErr"></div>
        <div class="ok-msg" id="renderOk"></div>
        <div class="row-actions">
          <button id="renderVideoBtn" class="good">Render &amp; Download Video</button>
        </div>
      </div>
    </div>
    <div class="tab-pane" id="exportTab">
      <p class="src">Export all loaded &amp; auto-enriched channels offline:</p>
      <div class="row-actions" id="exportBtns"></div>
    </div>
  </aside>
</main>
<script>
let meta = null, playing = false, timer = null, darkBg = false, inflight = false;
const $ = id => document.getElementById(id);

function applyMetaToUI(data, resetSlider = false) {
  meta = data;
  $('verTag').textContent = 'v' + (meta.version || '');
  $('sourceLabel').textContent = `${meta.source} (${meta.samples} pts @ ${meta.sample_rate.toFixed(1)} Hz)`;

  const sSel = $('sampleSel');
  if (meta.samples_list) {
    sSel.innerHTML = '<option value="">-- samples --</option>' +
      meta.samples_list.map(s => `<option value="${s}" ${s===meta.source?'selected':''}>${s}</option>`).join('');
  }

  const pSel = $('presetSel');
  pSel.innerHTML = '<option value="__custom__">(Auto / Current)</option>' +
    meta.presets.map(p => `<option value="${p}">${p}</option>`).join('');

  const tSel = $('themeSel');
  tSel.innerHTML = meta.themes.map(t => `<option value="${t}" ${t===meta.theme?'selected':''}>${t}</option>`).join('');

  $('yamlEdit').value = meta.yaml;
  renderChannels();
  renderEvents();

  $('exportBtns').innerHTML = meta.export_formats.map(f =>
    `<a href="/api/export?format=${f}" download><button>${f.toUpperCase()}</button></a>`
  ).join('');

  if (resetSlider) {
    // Jump near peak event if present, otherwise 35%
    const peak = (meta.events || []).find(e => /peak|burn|ign/i.test(e.label));
    if (peak && meta.t1 > meta.t0) {
      const frac = Math.max(0, Math.min(1, (peak.time - meta.t0) / (meta.t1 - meta.t0)));
      $('slider').value = Math.round(frac * 1000);
    } else {
      $('slider').value = 350;
    }
  }
  updateFrame();
}

async function init() {
  const res = await fetch('/api/state');
  const data = await res.json();
  applyMetaToUI(data, true);
}

function renderChannels() {
  const tbody = $('chanTable').querySelector('tbody');
  tbody.innerHTML = meta.channels.map(c =>
    `<tr><td class="mono">${c.key}</td><td>${c.unit}</td><td class="mono">${c.min}</td><td class="mono">${c.max}</td></tr>`
  ).join('');
}

function renderEvents() {
  const bar = $('eventsBar');
  if (!meta.events.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = meta.events.map(e => {
    const frac = Math.max(0, Math.min(1, (e.time - meta.t0) / Math.max(1e-6, meta.t1 - meta.t0)));
    return `<button class="ev-btn" onclick="jumpTo(${frac})">${e.label} @ ${e.time.toFixed(2)}s</button>`;
  }).join('');
}

window.jumpTo = (frac) => {
  $('slider').value = Math.round(frac * 1000);
  updateFrame();
};

function currentTime() {
  const frac = Number($('slider').value) / 1000;
  return meta.t0 + frac * (meta.t1 - meta.t0);
}

async function updateFrame() {
  if (!meta || inflight) return;
  inflight = true;
  const t = currentTime();
  $('timeLabel').textContent = `${t.toFixed(2)} s / ${meta.t1.toFixed(2)} s`;
  try {
    const resp = await fetch(`/api/frame?t=${t.toFixed(4)}&w=960&h=540&_=${Date.now()}`);
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
  $('bgToggle').textContent = darkBg ? 'Backdrop: Dark' : 'Backdrop: Checker';
});

async function uploadFile(file) {
  if (!file) return;
  $('errBox').style.display = 'none';
  $('sourceLabel').textContent = `Loading ${file.name} ...`;
  try {
    const res = await fetch('/api/upload?filename=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await res.json();
    if (!res.ok) {
      $('errBox').textContent = data.error || 'Could not load file';
      $('errBox').style.display = 'block';
      return;
    }
    applyMetaToUI(data, true);
  } catch (err) {
    $('errBox').textContent = String(err);
    $('errBox').style.display = 'block';
  }
}

$('openFileBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', (e) => {
  if (e.target.files && e.target.files[0]) uploadFile(e.target.files[0]);
  e.target.value = '';
});

const box = $('canvasBox');
['dragenter', 'dragover'].forEach(ev => box.addEventListener(ev, (e) => {
  e.preventDefault(); box.classList.add('drag-over');
}));
['dragleave', 'drop'].forEach(ev => box.addEventListener(ev, (e) => {
  e.preventDefault(); box.classList.remove('drag-over');
}));
box.addEventListener('drop', (e) => {
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
  if (res.ok) applyMetaToUI(data, true);
});

async function applyConfig(payload) {
  $('errBox').style.display = 'none';
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    $('errBox').textContent = data.error || 'Configuration error';
    $('errBox').style.display = 'block';
    return;
  }
  meta = data;
  $('yamlEdit').value = meta.yaml;
  $('themeSel').value = meta.theme;
  renderEvents();
  updateFrame();
}

$('applyBtn').addEventListener('click', () => applyConfig({ yaml: $('yamlEdit').value }));
$('presetSel').addEventListener('change', (e) => {
  if (e.target.value !== '__custom__') applyConfig({ preset: e.target.value });
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
  btn.textContent = 'Rendering video... (please wait)';
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
      $('renderErr').textContent = data.error || 'Render failed';
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
    $('renderOk').textContent = `Rendered ${filename} (${(blob.size / 1e6).toFixed(2)} MB)`;
    $('renderOk').style.display = 'block';
  } catch (err) {
    $('renderErr').textContent = String(err);
    $('renderErr').style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Render & Download Video';
  }
});

document.querySelectorAll('.tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $(btn.dataset.tab).classList.add('active');
  });
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
        self.telemetry = telemetry
        self.source_path = source_path
        self.yaml_text = config_yaml
        self.config = config
        self.renderer = self._make_renderer(config)
        self.upload_dir = tempfile.TemporaryDirectory(prefix="telemetry_studio_")

    def close(self) -> None:
        self.upload_dir.cleanup()

    def _make_renderer(self, cfg: Config, width: int = 960, height: int = 540) -> OverlayRenderer:
        preview_cfg = Config(**{**cfg.__dict__, "width": width, "height": height})
        return OverlayRenderer(preview_cfg, self.telemetry)

    def load_new_telemetry(
        self,
        telemetry: Telemetry,
        source_path: Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        yaml_text, cfg = _build_initial_config(telemetry, config_path)
        self.telemetry = telemetry
        self.source_path = source_path
        self.yaml_text = yaml_text
        self.config = cfg
        self.renderer = self._make_renderer(cfg)

    def state_dict(self) -> dict[str, Any]:
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
        return {
            "version": __version__,
            "source": Path(self.telemetry.source).name,
            "samples": int(self.telemetry.time.size),
            "sample_rate": float(self.telemetry.sample_rate),
            "t0": float(self.renderer.timeline.data_start),
            "t1": float(self.renderer.timeline.data_end),
            "theme": self.config.theme.name,
            "presets": presets,
            "samples_list": _list_samples(),
            "themes": sorted(THEMES),
            "export_formats": sorted(EXPORT_FORMATS),
            "has_ffmpeg": find_ffmpeg() is not None,
            "channels": channels,
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
    """Create (without blocking) the Studio HTTP server for interactive previewing.

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
                with state.lock:
                    self._send_json(200, state.state_dict())
                return

            if parsed.path == "/api/samples":
                self._send_json(200, {"samples": _list_samples()})
                return

            if parsed.path == "/api/frame":
                t = float(qs.get("t", ["0"])[0])
                full = qs.get("full", ["0"])[0] in ("1", "true", "yes")
                with state.lock:
                    if full:
                        full_renderer = OverlayRenderer(state.config, state.telemetry)
                        img = full_renderer.render_at(t, frame=0)
                    else:
                        img = state.renderer.render_at(t, frame=0)
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
                # Stream request body directly to a temp file so large MP4/XLSX files work
                dest = Path(state.upload_dir.name) / safe_name
                try:
                    remaining = length
                    with dest.open("wb") as fh:
                        while remaining > 0:
                            chunk = self.rfile.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                break
                            fh.write(chunk)
                            remaining -= len(chunk)
                    tel = load_data(dest)
                    with state.lock:
                        state.load_new_telemetry(tel, source_path=dest)
                        self._send_json(200, state.state_dict())
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
                        self._send_json(200, state.state_dict())
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
                        telemetry = state.telemetry
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
    # Attach cleanup hook so temp upload dir is cleaned on server_close()
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
