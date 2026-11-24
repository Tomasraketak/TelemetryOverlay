"""Zero-dependency local interactive Web Studio for Telemetry Overlay.

Run with:
    telemetry-overlay studio examples/static_fire_thrust.csv

Serves a responsive dark-mode browser UI using Python's stdlib ``http.server``:
- Live rendered overlay preview over a transparency checkerboard or dark backdrop
- Interactive timeline scrubber, Play/Pause and jump-to-event buttons
- Instant preset & theme switching
- Channel inspector (ranges, units, kinds)
- Live YAML config editor with instant validation & preview
- One-click data export (CSV, GPX, KML, GeoJSON, MGJSON, JSON)
"""

from __future__ import annotations

import io
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from .config import Config, load_config, suggest_config
from .data import Telemetry, load as load_data
from .extract import EXPORT_FORMATS, export_telemetry
from .render import OverlayRenderer
from .theme import THEMES

__all__ = ["run_studio", "create_studio_server"]

_PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"

_STUDIO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Telemetry Overlay Studio</title>
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
  }
  header {
    display: flex; align-items: center; gap: 14px; padding: 10px 18px;
    background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }
  header h1 { margin: 0; font-size: 16px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--accent); }
  .src { font-size: 12px; color: var(--dim); font-family: ui-monospace, monospace; }
  .spacer { flex: 1; }
  select, button, input[type="color"] {
    background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 6px 10px; font-size: 13px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: #090d16; font-weight: 600; border: none; }
  button:hover { filter: brightness(1.12); }
  main { flex: 1; display: grid; grid-template-columns: 1fr 410px; min-height: 0; }
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
  #preview { max-width: 100%; max-height: 100%; object-fit: contain; display: block; }
  .scrub {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;
  }
  .scrub-row { display: flex; align-items: center; gap: 10px; }
  input[type="range"] { flex: 1; accent-color: var(--accent); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  .events { display: flex; gap: 6px; flex-wrap: wrap; }
  .ev-btn { font-size: 11px; padding: 4px 8px; border-radius: 999px; background: rgba(255,159,10,0.14); border: 1px solid rgba(255,159,10,0.4); color: #ffd089; }
  aside {
    background: var(--panel); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
  }
  .tabs { display: flex; border-bottom: 1px solid var(--border); }
  .tabs button { flex: 1; border: none; border-radius: 0; padding: 10px; background: transparent; color: var(--dim); font-weight: 600; }
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
  .err { color: #fca5a5; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); border-radius: 8px; padding: 8px 10px; font-size: 12px; display: none; }
  .row-actions { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
</head>
<body>
<header>
  <h1>Telemetry Overlay Studio</h1>
  <span class="src" id="sourceLabel"></span>
  <span class="spacer"></span>
  <label class="src">Preset: <select id="presetSel"></select></label>
  <label class="src">Theme: <select id="themeSel"></select></label>
  <button id="bgToggle">Backdrop: Checker</button>
</header>
<main>
  <section class="stage-wrap">
    <div class="canvas-box" id="canvasBox">
      <img id="preview" alt="Overlay Preview" />
    </div>
    <div class="scrub">
      <div class="scrub-row">
        <button id="playBtn" class="primary">Play</button>
        <input id="slider" type="range" min="0" max="1000" value="500" />
        <span class="mono" id="timeLabel">0.00 s</span>
      </div>
      <div class="events" id="eventsBar"></div>
    </div>
  </section>
  <aside>
    <div class="tabs">
      <button class="active" data-tab="yamlTab">YAML Config</button>
      <button data-tab="channelsTab">Channels</button>
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
    <div class="tab-pane" id="exportTab">
      <p class="src">Export all loaded &amp; auto-enriched channels offline:</p>
      <div class="row-actions" id="exportBtns"></div>
    </div>
  </aside>
</main>
<script>
let meta = null, playing = false, timer = null, darkBg = false, inflight = false;
const $ = id => document.getElementById(id);

async function init() {
  const res = await fetch('/api/state');
  meta = await res.json();
  $('sourceLabel').textContent = `${meta.source} (${meta.samples} pts @ ${meta.sample_rate.toFixed(1)} Hz)`;
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
  updateFrame();
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


class _StudioState:
    def __init__(self, telemetry: Telemetry, config_yaml: str, config: Config):
        self.lock = threading.Lock()
        self.telemetry = telemetry
        self.yaml_text = config_yaml
        self.config = config
        self.renderer = self._make_renderer(config)

    def _make_renderer(self, cfg: Config) -> OverlayRenderer:
        preview_cfg = Config(**{**cfg.__dict__, "width": 960, "height": 540})
        return OverlayRenderer(preview_cfg, self.telemetry)

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
            "source": Path(self.telemetry.source).name,
            "samples": int(self.telemetry.time.size),
            "sample_rate": float(self.telemetry.sample_rate),
            "t0": float(self.renderer.timeline.data_start),
            "t1": float(self.renderer.timeline.data_end),
            "theme": self.config.theme.name,
            "presets": presets,
            "themes": sorted(THEMES),
            "export_formats": sorted(EXPORT_FORMATS),
            "channels": channels,
            "events": [
                {"time": float(e["time"]), "label": str(e.get("label", ""))}
                for e in self.renderer.events
            ],
            "yaml": self.yaml_text,
        }


def create_studio_server(
    data_path: str | Path,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create (without blocking) the Studio HTTP server for interactive previewing."""
    telemetry = load_data(data_path)
    if config_path is not None:
        cfg_p = Path(config_path)
        yaml_text = cfg_p.read_text(encoding="utf-8")
        cfg = load_config(cfg_p)
    else:
        suggested = suggest_config(telemetry)
        yaml_text = yaml.safe_dump(suggested, sort_keys=False, allow_unicode=True)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
            tf.write(yaml_text)
            tmp_p = Path(tf.name)
        try:
            cfg = load_config(tmp_p)
        finally:
            tmp_p.unlink(missing_ok=True)

    state = _StudioState(telemetry, yaml_text, cfg)

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

            if parsed.path == "/api/state":
                with state.lock:
                    self._send_json(200, state.state_dict())
                return

            if parsed.path == "/api/frame":
                t = float(qs.get("t", ["0"])[0])
                with state.lock:
                    img = state.renderer.render_at(t, frame=0)
                buf = io.BytesIO()
                img.save(buf, "PNG", compress_level=1)
                raw = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if parsed.path == "/api/export":
                fmt = (qs.get("format", ["csv"])[0]).lower()
                import tempfile
                try:
                    with tempfile.TemporaryDirectory() as td:
                        out_p = Path(td) / f"telemetry.{fmt}"
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
            if self.path != "/api/config":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
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
                    import tempfile
                    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
                        tf.write(new_yaml)
                        tmp_p = Path(tf.name)
                    try:
                        new_cfg = load_config(tmp_p)
                    finally:
                        tmp_p.unlink(missing_ok=True)
                    new_renderer = state._make_renderer(new_cfg)
                    state.yaml_text = new_yaml
                    state.config = new_cfg
                    state.renderer = new_renderer
                    self._send_json(200, state.state_dict())
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def run_studio(
    data_path: str | Path,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Start the local Web Studio server and block until Ctrl+C."""
    server = create_studio_server(data_path, config_path, host=host, port=port)
    actual_port = server.server_address[1]
    print(f"Telemetry Overlay Studio running at http://{host}:{actual_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
