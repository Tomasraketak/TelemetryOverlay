"""Command line interface.

    telemetry-overlay inspect data.csv|track.gpx|activity.fit|GX010001.MP4
    telemetry-overlay init    data.csv -o overlay.yaml
    telemetry-overlay preview data.csv -c overlay.yaml --at peak
    telemetry-overlay render  data.csv -c overlay.yaml -o overlay.mov
    telemetry-overlay render  GX010001.MP4 -c action_cam --video GX010001.MP4 -o out.mp4
    telemetry-overlay extract GX010001.MP4 -o track.gpx
    telemetry-overlay batch   *.gpx -c cycling -o renders/
    telemetry-overlay studio  examples/circuit_lap.gpx
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    Config, load_config, save_config, suggest_config,
)
from .data import LoadOptions, load as load_data, load_many
from .events import build_events
from .expressions import add_derived
from .export import (
    VIDEO_CODECS, VIDEO_SUFFIXES, codec_for_path, export_composite_video,
    export_frames, export_video, find_ffmpeg, probe_video,
)
from .extract import EXPORT_FORMATS, export_telemetry
from .formats import SUPPORTED_FORMATS, describe_formats
from .render import build_renderer
from .theme import THEMES
from .timeline import Hold, Ramp
from .widgets import WIDGET_ALIASES, WIDGET_TYPES

_PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"


class _Fail(Exception):
    """A user-facing error; printed without a traceback."""


# ---------------------------------------------------------------------------
# Shared argument groups
# ---------------------------------------------------------------------------

def _add_data_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("data reading & filtering")
    group.add_argument("--input-format", dest="input_format", metavar="FMT",
                       help="force input format (csv, xlsx, json, gpx, kml, tcx, fit, nmea, igc, srt, gpmf)")
    group.add_argument("--time-column", metavar="NAME",
                       help="column to use as the time axis (default: sniffed)")
    group.add_argument("--time-unit", choices=["s", "ms", "us", "ns"],
                       help="unit of the time column (default: from its header)")
    group.add_argument("--delimiter", metavar="CHAR",
                       help="CSV field separator (default: sniffed)")
    group.add_argument("--decimal", choices=[".", ","],
                       help="decimal mark (default: sniffed)")
    group.add_argument("--encoding", metavar="ENC",
                       help="text encoding (default: utf-8 then cp1250)")
    group.add_argument("--sheet", metavar="NAME",
                       help="worksheet to read from an .xlsx file")
    group.add_argument("--skip-rows", type=int, default=None, metavar="N",
                       help="skip N leading rows in CSV/XLSX files")
    group.add_argument("--merge", action="append", default=[], metavar="FILE[:OFFSET]",
                       help="merge extra sensor file (e.g. --merge garmin.fit or --merge imu.csv:0.5)")
    group.add_argument("--laps", action="store_true", default=False,
                       help="auto-detect GPS circuit laps and compute lap timer channels")
    group.add_argument("--no-enrich", action="store_true", default=False,
                       help="disable automatic GPS/IMU derived channels")
    group.add_argument("--smooth", type=int, metavar="N",
                       help="apply N-sample moving average smoothing to numeric channels")
    group.add_argument("--despike", type=int, metavar="N",
                       help="apply N-sample median despike filter to numeric channels")
    group.add_argument("--resample", type=float, metavar="HZ",
                       help="resample timeline uniformly to HZ samples per second")
    group.add_argument("--time-offset", type=float, metavar="SECONDS",
                       help="shift telemetry timestamps by SECONDS")


def _add_time_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("timing and slow motion")
    group.add_argument("--fps", type=float, metavar="N",
                       help="output frame rate (default: from the config, else 60)")
    group.add_argument("--speed", type=float, metavar="X",
                       help="playback speed; 1 is real time, 0.25 is 4x slower")
    group.add_argument("--start", type=float, metavar="T",
                       help="first data timestamp to cover, in seconds")
    group.add_argument("--end", type=float, metavar="T",
                       help="last data timestamp to cover, in seconds")
    group.add_argument("--t0", type=float, metavar="T",
                       help="data timestamp shown as T+0 by clocks and axes")
    group.add_argument(
        "--ramp", action="append", default=[], metavar="FROM:TO:SPEED[:EASE]",
        help="slow down (or speed up) one stretch; repeatable. "
             "Example: --ramp 1.6:2.6:0.2:0.15")
    group.add_argument(
        "--hold", action="append", default=[], metavar="AT:SECONDS",
        help="freeze the overlay at a timestamp for N seconds of video; repeatable")
    group.add_argument("--max-duration", type=float, metavar="SECONDS",
                       help="refuse to render a clip longer than this (default 900)")


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("output")
    group.add_argument("-r", "--resolution", metavar="WxH",
                       help="frame size, e.g. 1080p, 4k, 5.3k, vertical1080 or 2560x1440")
    group.add_argument("--theme", choices=sorted(THEMES),
                       help="override colour theme preset")
    group.add_argument("--background", metavar="COLOR",
                       help="fill the frame instead of leaving it transparent "
                            "(e.g. '#000000' or '#101010 @ 60%%')")
    group.add_argument("--chroma", choices=["green", "blue"],
                       help="render over a chroma-key green (#00ff00) or blue (#0000ff) backdrop")


def _parse_ramp(text: str) -> Ramp:
    parts = text.split(":")
    if len(parts) not in (3, 4):
        raise _Fail(
            f"--ramp expects FROM:TO:SPEED[:EASE], got {text!r}. "
            f"Example: --ramp 1.6:2.6:0.25:0.1")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise _Fail(f"--ramp {text!r}: every part must be a number") from exc
    try:
        return Ramp(start=values[0], end=values[1], speed=values[2],
                    ease=values[3] if len(values) == 4 else 0.0)
    except ValueError as exc:
        raise _Fail(f"--ramp {text!r}: {exc}") from exc


def _parse_hold(text: str) -> Hold:
    parts = text.split(":")
    if len(parts) != 2:
        raise _Fail(f"--hold expects AT:SECONDS, got {text!r}. Example: --hold 2.3:1.5")
    try:
        return Hold(at=float(parts[0]), duration=float(parts[1]))
    except ValueError as exc:
        raise _Fail(f"--hold {text!r}: {exc}") from exc


def _parse_merge_spec(text: str) -> dict[str, Any]:
    # Accept "file.fit" or "file.csv:1.25" (Windows drive letters C:\ handled safely)
    if ":" in text and not Path(text).exists():
        head, _, tail = text.rpartition(":")
        try:
            return {"path": head, "offset": float(tail)}
        except ValueError:
            pass
    return {"path": text, "offset": None}


def _cli_filters(args: argparse.Namespace) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    if getattr(args, "despike", None):
        filters.append({"type": "despike", "window": int(args.despike)})
    if getattr(args, "smooth", None):
        filters.append({"type": "smooth", "window": int(args.smooth)})
    if getattr(args, "resample", None):
        filters.append({"type": "resample", "hz": float(args.resample)})
    return filters


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("fps", "speed", "start", "end", "t0", "resolution", "background",
                "theme", "delimiter", "decimal", "time_column", "time_unit",
                "encoding", "sheet", "skip_rows", "max_duration", "time_offset"):
        value = getattr(args, key, None)
        if value is not None:
            out[key] = value
    if getattr(args, "input_format", None):
        out["format"] = args.input_format
    if getattr(args, "no_enrich", False):
        out["enrich"] = False
    if getattr(args, "laps", False):
        out["detect_laps"] = True
    chroma = getattr(args, "chroma", None)
    if chroma == "green":
        out["background"] = "#00ff00"
    elif chroma == "blue":
        out["background"] = "#0000ff"
    return out


def _resolve_config_path(value: str | None) -> Path | None:
    """Accept a file path or the bare name of a bundled preset."""
    if value is None:
        return None
    path = Path(value)
    if path.exists():
        return path
    for suffix in ("", ".yaml", ".yml", ".json"):
        candidate = _PRESET_DIR / f"{value}{suffix}"
        if candidate.exists():
            return candidate
    available = sorted(p.stem for p in _PRESET_DIR.glob("*.y*ml")) \
        if _PRESET_DIR.is_dir() else []
    raise _Fail(
        f"config not found: {value}"
        + (f"\nBundled presets: {', '.join(available)}" if available else "")
    )


def _build_config(args: argparse.Namespace) -> Config:
    path = _resolve_config_path(getattr(args, "config", None))
    config = load_config(path, _overrides(args))
    for text in getattr(args, "ramp", []) or []:
        config.ramps.append(_parse_ramp(text))
    for text in getattr(args, "hold", []) or []:
        config.holds.append(_parse_hold(text))
    for text in getattr(args, "merge", []) or []:
        config.data.merge.append(_parse_merge_spec(text))
    extra_filters = _cli_filters(args)
    if extra_filters:
        config.filters.extend(extra_filters)
    return config


def _load_options(args: argparse.Namespace) -> LoadOptions:
    merges = [_parse_merge_spec(m) for m in (getattr(args, "merge", []) or [])]
    return LoadOptions(
        delimiter=getattr(args, "delimiter", None),
        decimal=getattr(args, "decimal", None),
        time_column=getattr(args, "time_column", None),
        time_unit=getattr(args, "time_unit", None),
        sheet=getattr(args, "sheet", None),
        encoding=getattr(args, "encoding", None),
        skip_rows=int(getattr(args, "skip_rows", None) or 0),
        format=getattr(args, "input_format", None),
        enrich=not bool(getattr(args, "no_enrich", False)),
        detect_laps=bool(getattr(args, "laps", False)),
        time_offset=float(getattr(args, "time_offset", None) or 0.0),
        filters=_cli_filters(args),
        merge=merges,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    data_paths = args.data if isinstance(args.data, list) else [args.data]
    telemetry = (
        load_many(data_paths, _load_options(args))
        if len(data_paths) > 1
        else load_data(data_paths[0], _load_options(args))
    )
    print(telemetry.summary())
    if telemetry.meta:
        print("\nmetadata found in the file:")
        for key, value in list(telemetry.meta.items())[:40]:
            if key == "laps" and isinstance(value, list):
                print(f"  {key:<34} {len(value)} lap(s)")
            else:
                print(f"  {key:<34} {value}")
    events = build_events(telemetry, True)
    if events:
        print("\ndetected events:")
        for event in events[:20]:
            print(f"  {event['time']:9.4f} s   {event['label']}")
    print(f"\nNext step:  telemetry-overlay init {data_paths[0]} -o overlay.yaml")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    telemetry = load_data(args.data, _load_options(args))
    config = suggest_config(
        telemetry,
        resolution=args.resolution or "1080p",
        fps=args.fps or 60.0,
    )
    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise _Fail(f"{out} already exists - pass --overwrite to replace it")
    written = save_config(config, out)
    chans = []
    for w in config["widgets"]:
        ch = w.get("channel")
        if isinstance(ch, list):
            chans.append(str(ch[0]))
        elif ch:
            chans.append(str(ch))
    print(f"Wrote a starter config to {written}")
    print(f"  template      : {config.get('name', 'Telemetry overlay')}")
    if chans:
        print(f"  channels used : {', '.join(chans)}")
    print(f"\nPreview it with:\n  telemetry-overlay preview {args.data} "
          f"-c {written} --at 50% -o preview.png")
    print(f"\nOr open the interactive studio:\n  telemetry-overlay studio {args.data} -c {written}")
    print(f"\nThen render the video:\n  telemetry-overlay render {args.data} "
          f"-c {written} -o overlay.mov")
    return 0


def _resolve_at(value: str, renderer) -> float:
    text = value.strip().lower()
    timeline = renderer.timeline
    if text.endswith("%"):
        frac = float(text[:-1]) / 100.0
        return timeline.data_start + (timeline.data_end - timeline.data_start) * frac
    for event in renderer.events:
        if event.get("key") == text or event.get("label", "").lower() == text:
            return float(event["time"])
    try:
        return float(text)
    except ValueError:
        keys = sorted({str(e.get("key")) for e in renderer.events})
        raise _Fail(
            f"--at {value!r} is neither a number, a percentage, nor a known "
            f"event. Known events: {', '.join(keys) or 'none detected'}"
        ) from None


def cmd_preview(args: argparse.Namespace) -> int:
    config = _build_config(args)
    data_arg = args.data if len(args.data) > 1 else args.data[0]
    renderer = build_renderer(config, data_arg)
    print(renderer.describe())
    data_t = _resolve_at(args.at, renderer)
    image = renderer.render_at(data_t, frame=0)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG")
    opaque = image.getchannel("A").getextrema()[1]
    print(f"\nWrote {out}  ({image.width}x{image.height}, "
          f"at t = {data_t:.4f} s, max alpha {opaque}/255)")
    if opaque == 0:
        print("  Warning: the frame is fully transparent - check that the "
              "widgets' time windows include this timestamp.")
    return 0


def _progress_bar(done: int, total: int, began: float) -> None:
    width = min(38, max(10, (shutil.get_terminal_size((80, 20)).columns - 42)))
    frac = done / total if total else 1.0
    filled = int(width * frac)
    elapsed = time.perf_counter() - began
    rate = done / elapsed if elapsed > 0 and done else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(
        f"\r  [{bar}] {done:>6}/{total}  {rate:5.1f} fps  ETA {eta:5.1f}s")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")


def _is_video_target(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def cmd_render(args: argparse.Namespace) -> int:
    config = _build_config(args)
    data_arg = args.data if len(args.data) > 1 else args.data[0]

    # When compositing onto footage without explicit resolution/fps, match source video!
    video_in = getattr(args, "video", None)
    if video_in:
        info_v = probe_video(video_in)
        if getattr(args, "resolution", None) is None and args.config is None:
            config.width = info_v["width"]
            config.height = info_v["height"]
        if getattr(args, "fps", None) is None and info_v.get("fps"):
            config.fps = info_v["fps"]

    renderer = build_renderer(config, data_arg)
    print(renderer.describe())

    output = Path(args.output)
    if video_in:
        spec = codec_for_path(output, args.codec or "h264")
        print(f"\ncomposite     : {video_in} + overlay -> {output} ({spec.name})")
        if args.dry_run:
            print("\nDry run - nothing written.")
            return 0
        began = time.perf_counter()
        reporter = None if args.quiet else (lambda d, t: _progress_bar(d, t, began))
        info = export_composite_video(
            config, data_arg, video_in, output,
            codec=args.codec, sync_offset=float(args.sync or 0.0),
            progress=reporter, renderer=renderer, overwrite=args.overwrite,
        )
        print(f"\nDone: {info['output']}")
        print(f"  {info['frames']} frames, {info['size'][0]}x{info['size'][1]}, "
              f"{info['duration']:.2f} s at {info['fps']:g} fps")
        print(f"  {info['bytes'] / 1e6:.1f} MB, rendered in {info['seconds']:.1f} s "
              f"({info['frames_per_second']:.1f} frames/s)")
        return 0

    if args.format == "auto":
        as_video = _is_video_target(args.output)
    else:
        as_video = args.format == "video"
        if as_video and not _is_video_target(args.output):
            output = output.with_suffix(codec_for_path(output, args.codec).suffix)

    if as_video:
        spec = codec_for_path(output, args.codec)
        print(f"\noutput        : {output}  ({spec.name}, "
              f"{'with alpha' if spec.alpha else 'NO alpha'})")
        if not spec.alpha and not config.background:
            print("  Warning: this codec cannot store transparency - the empty "
                  "parts of the frame will be black.")
    else:
        print(f"\noutput        : {output}/  (PNG sequence with alpha)")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0

    began = time.perf_counter()
    reporter = (None if args.quiet else lambda d, t: _progress_bar(d, t, began))
    single = (args.workers or 0) == 1

    if as_video and find_ffmpeg() is None and args.format == "auto":
        print("\nffmpeg not found - falling back to a PNG sequence.")
        print("  Install it with:  pip install imageio-ffmpeg")
        as_video = False
        output = output.with_suffix("")

    if as_video:
        print(f"\nRendering {renderer.frame_count} frames straight into video ...")
        info = export_video(
            config, data_arg, output,
            codec=args.codec, workers=args.workers,
            progress=reporter, renderer=renderer if single else None,
            overwrite=args.overwrite,
        )
        print(f"\nDone: {info['output']}")
        print(f"  {info['frames']} frames, {info['size'][0]}x{info['size'][1]}, "
              f"{info['duration']:.2f} s at {info['fps']:g} fps")
        print(f"  {info['bytes'] / 1e6:.1f} MB, codec {info['codec']}"
              f"{' with alpha' if info['alpha'] else ' WITHOUT alpha'}")
        print(f"  rendered in {info['seconds']:.1f} s "
              f"({info['frames_per_second']:.1f} frames/s)")
        print(f"  {info['note']}")
        if info["codec"] == "prores4444":
            print("  Tip: --codec qtrle is also lossless with alpha and, because "
                  "an overlay\n       is mostly empty, usually several times "
                  "smaller and quicker.")
        return 0

    print(f"\nRendering {renderer.frame_count} frames to {output} ...")
    result = export_frames(
        config, data_arg, output,
        prefix=args.prefix, digits=args.digits, workers=args.workers,
        compress_level=args.compression, overwrite=args.overwrite,
        progress=reporter, renderer=renderer if single else None,
    )
    print(f"\nDone: {result['frames']} PNG frames, "
          f"{result['size'][0]}x{result['size'][1]}, "
          f"{result['duration']:.2f} s at {result['fps']:g} fps")
    print(f"  {result['seconds']:.1f} s of rendering "
          f"({result['frames_per_second']:.1f} frames/s)")
    print(f"  pattern: {result['pattern']}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract & convert telemetry (GoPro MP4, FIT, GPX, DJI SRT, CSV...) to data formats."""
    opts = _load_options(args)
    telemetry = (
        load_many(args.data, opts) if len(args.data) > 1
        else load_data(args.data[0], opts)
    )
    # Apply optional derived expressions from CLI (--derive "key=expr")
    derived_specs = []
    for item in getattr(args, "derive", []) or []:
        if "=" not in item:
            raise _Fail(f"--derive expects KEY=EXPRESSION, got {item!r}")
        k, _, expr = item.partition("=")
        derived_specs.append({"key": k.strip(), "expression": expr.strip()})
    if derived_specs:
        add_derived(telemetry, derived_specs)

    # Optional time slice
    if args.start is not None or args.end is not None:
        t0 = args.start if args.start is not None else telemetry.t0
        t1 = args.end if args.end is not None else telemetry.t1
        mask = (telemetry.time >= t0) & (telemetry.time <= t1)
        telemetry.time = telemetry.time[mask]
        for ch in telemetry.channels.values():
            ch.values = ch.values[mask]
            if ch.text_values is not None:
                ch.text_values = ch.text_values[mask]
            ch.invalidate_cache()

    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise _Fail(f"{out} already exists - pass --overwrite to replace it")
    channels = [c.strip() for c in args.channels.split(",") if c.strip()] if args.channels else None
    res = export_telemetry(
        telemetry, out, fmt=args.to, channels=channels,
        delimiter=args.out_delimiter or ",",
    )
    print(f"Extracted {res['samples']} samples ({res['channels']} channels) "
          f"-> {res['output']} [{res['format']}, {res['bytes'] / 1024:.1f} KB]")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Batch render or extract multiple telemetry files in one run."""
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for src_str in args.files:
        src = Path(src_str)
        if not src.exists():
            print(f"skip (missing): {src}", file=sys.stderr)
            continue
        if args.extract_to:
            target = out_dir / f"{src.stem}.{args.extract_to}"
            tel = load_data(src, _load_options(args))
            res = export_telemetry(tel, target, fmt=args.extract_to)
            print(f"  [ok] {src.name} -> {res['output']} ({res['samples']} samples)")
            ok += 1
        else:
            config = _build_config(args)
            ext = args.ext or codec_for_path("out.mov", args.codec).suffix
            if not ext.startswith("."):
                ext = "." + ext
            target = out_dir / f"{src.stem}_overlay{ext}"
            print(f"  rendering {src.name} -> {target.name} ...")
            info = export_video(
                config, src, target, codec=args.codec,
                workers=args.workers, overwrite=args.overwrite,
            )
            print(f"  [ok] {target.name} ({info['frames']} frames, {info['seconds']:.1f}s)")
            ok += 1
    print(f"\nBatch complete: {ok}/{len(args.files)} processed.")
    return 0


def cmd_studio(args: argparse.Namespace) -> int:
    from .studio import run_studio
    cfg_path = _resolve_config_path(args.config) if args.config else None
    run_studio(args.data, cfg_path, host=args.host, port=args.port)
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    if not _PRESET_DIR.is_dir():
        raise _Fail(f"preset folder is missing: {_PRESET_DIR}")
    files = sorted(_PRESET_DIR.glob("*.y*ml"))
    if not files:
        raise _Fail(f"no presets found in {_PRESET_DIR}")
    print(f"Presets in {_PRESET_DIR}:\n")
    for path in files:
        description = ""
        for line in path.read_text(encoding="utf-8").splitlines()[:12]:
            stripped = line.strip()
            if stripped.startswith("description:"):
                description = stripped.split(":", 1)[1].strip().strip('"\'')
                break
        print(f"  {path.stem:<18} {description}")
    print("\nUse one by name, e.g.:")
    print(f"  telemetry-overlay render data.csv -c {files[0].stem} -o overlay.mov")
    return 0


def cmd_formats(args: argparse.Namespace) -> int:
    print(describe_formats())
    print("\nSupported telemetry export formats (via `telemetry-overlay extract`):\n")
    for name, desc in EXPORT_FORMATS.items():
        print(f"  {name:<12} {desc}")
    print("\nSupported video codecs (via `telemetry-overlay render --codec`):\n")
    for name, spec in VIDEO_CODECS.items():
        alpha = "alpha" if spec.alpha else "opaque"
        print(f"  {name:<12} ({spec.suffix}, {alpha:<6})  {spec.note}")
    return 0


def cmd_widgets(args: argparse.Namespace) -> int:
    print(f"Available widgets ({len(WIDGET_TYPES)} types):\n")
    alias_rev: dict[str, list[str]] = {}
    for al, target in WIDGET_ALIASES.items():
        alias_rev.setdefault(target, []).append(al)
    for name in sorted(WIDGET_TYPES):
        cls = WIDGET_TYPES[name]
        doc = (cls.__doc__ or "").strip().splitlines()[0]
        als = alias_rev.get(name)
        al_str = f"  (aliases: {', '.join(sorted(als))})" if als else ""
        print(f"  {name:<14} {doc}{al_str}")
    print(f"\nAvailable themes ({len(THEMES)}): {', '.join(sorted(THEMES))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-overlay",
        description="Universal telemetry extractor, converter and animated overlay "
                    "renderer for GoPro, DJI, Garmin FIT, GPX, IGC, NMEA, CSV and XLSX.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Full documentation: README.md and docs/",
    )
    parser.add_argument("--version", action="version",
                        version=f"telemetry-overlay {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # inspect
    inspect = subparsers.add_parser(
        "inspect", help="show format, channels, GPS bounds, laps and detected events")
    inspect.add_argument("data", nargs="+", help="one or more telemetry files (CSV, XLSX, GPX, FIT, GoPro MP4, DJI SRT, ...)")
    _add_data_args(inspect)
    inspect.set_defaults(func=cmd_inspect)

    # init
    init = subparsers.add_parser(
        "init", help="generate a smart domain-aware starter config tailored to a log")
    init.add_argument("data", help="telemetry file")
    init.add_argument("-o", "--output", default="overlay.yaml",
                      help="config file to write (default: overlay.yaml)")
    init.add_argument("-r", "--resolution", help="target resolution, e.g. 4k")
    init.add_argument("--fps", type=float, help="target frame rate")
    init.add_argument("--overwrite", action="store_true",
                      help="replace an existing config file")
    _add_data_args(init)
    init.set_defaults(func=cmd_init)

    # preview
    preview = subparsers.add_parser(
        "preview", help="render a single frame to check the layout")
    preview.add_argument("data", nargs="+", help="telemetry file(s)")
    preview.add_argument("-c", "--config", help="config file or bundled preset name")
    preview.add_argument("-o", "--output", default="preview.png",
                         help="PNG to write (default: preview.png)")
    preview.add_argument("--at", default="peak", metavar="T",
                         help="when to sample: seconds, percentage like 50%%, or event name (default: peak)")
    _add_output_args(preview)
    _add_time_args(preview)
    _add_data_args(preview)
    preview.set_defaults(func=cmd_preview)

    # render
    render = subparsers.add_parser(
        "render", help="render animated overlay (alpha video, composited footage, or PNG sequence)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The output path picks the format:\n"
               "  -o overlay.mov    ProRes 4444 / QTRLE / DNxHR with alpha\n"
               "  -o overlay.webm   VP9 with alpha (web/OBS)\n"
               "  -o overlay.webp   Animated WebP with alpha\n"
               "  -o overlay.gif    Animated GIF with alpha\n"
               "  -o composite.mp4 --video footage.mp4   Burn directly onto video with audio\n"
               "  -o frames/        numbered PNG sequence with alpha")
    render.add_argument("data", nargs="+", help="telemetry file(s) — multiple files are concatenated chronologically")
    render.add_argument("-c", "--config", help="config file or bundled preset name")
    render.add_argument("-o", "--output", default="overlay.mov",
                        help="video file or frame folder (default: overlay.mov)")
    render.add_argument("--video", metavar="FOOTAGE",
                        help="composite overlay directly onto this source video (keeps audio)")
    render.add_argument("--sync", type=float, default=0.0, metavar="SECONDS",
                        help="footage start offset in seconds when compositing with --video")
    render.add_argument("--format", default="auto",
                        choices=["auto", "video", "frames"],
                        help="force output format instead of deriving from path")
    render.add_argument("--codec", default=None, choices=sorted(VIDEO_CODECS),
                        help="video codec (default: from extension)")
    render.add_argument("--prefix", default="overlay",
                        help="frame name prefix for PNG sequence (default: overlay)")
    render.add_argument("--digits", type=int, default=5,
                        help="digits in frame number (default: 5)")
    render.add_argument("-j", "--workers", type=int, metavar="N",
                        help="parallel worker processes (default: all cores, max 8)")
    render.add_argument("--compression", type=int, default=3, metavar="0-9",
                        help="PNG compression 0..9 (default: 3)")
    render.add_argument("--overwrite", action="store_true",
                        help="replace an existing output")
    render.add_argument("--dry-run", action="store_true",
                        help="print the plan without rendering anything")
    render.add_argument("--quiet", action="store_true", help="no progress bar")
    _add_output_args(render)
    _add_time_args(render)
    _add_data_args(render)
    render.set_defaults(func=cmd_render)

    # extract
    extract = subparsers.add_parser(
        "extract", help="extract & convert telemetry (GoPro MP4, FIT, GPX, DJI SRT, ...) to CSV/GPX/KML/GeoJSON/MGJSON/SRT")
    extract.add_argument("data", nargs="+", help="one or more input telemetry/video files")
    extract.add_argument("-o", "--output", required=True, help="output file path")
    extract.add_argument("--to", choices=sorted(EXPORT_FORMATS),
                         help="explicit output format (default: from extension)")
    extract.add_argument("--channels", metavar="A,B,C",
                         help="comma-separated subset of channels to export")
    extract.add_argument("--derive", action="append", default=[], metavar="KEY=EXPR",
                         help="compute extra derived channel before exporting (repeatable)")
    extract.add_argument("--start", type=float, metavar="T", help="trim start time (s)")
    extract.add_argument("--end", type=float, metavar="T", help="trim end time (s)")
    extract.add_argument("--out-delimiter", metavar="CHAR", help="CSV output delimiter (default: ',')")
    extract.add_argument("--overwrite", action="store_true", help="replace existing output")
    _add_data_args(extract)
    extract.set_defaults(func=cmd_extract)

    # batch
    batch = subparsers.add_parser(
        "batch", help="batch render or extract multiple telemetry files")
    batch.add_argument("files", nargs="+", help="input telemetry/video files")
    batch.add_argument("-o", "--output-dir", default="batch_out",
                       help="destination directory (default: batch_out)")
    batch.add_argument("-c", "--config", help="config file or preset for rendering")
    batch.add_argument("--codec", default="qtrle", choices=sorted(VIDEO_CODECS),
                       help="video codec for batch render (default: qtrle)")
    batch.add_argument("--ext", help="output video extension override (e.g. .mov, .mp4)")
    batch.add_argument("--extract-to", choices=sorted(EXPORT_FORMATS),
                       help="extract telemetry data to this format instead of rendering video")
    batch.add_argument("-j", "--workers", type=int, metavar="N", help="worker processes")
    batch.add_argument("--overwrite", action="store_true", help="overwrite existing files")
    _add_output_args(batch)
    _add_time_args(batch)
    _add_data_args(batch)
    batch.set_defaults(func=cmd_batch)

    # studio
    studio = subparsers.add_parser(
        "studio", help="launch interactive local Web UI for live preview & config editing")
    studio.add_argument("data", help="telemetry file to open in Studio")
    studio.add_argument("-c", "--config", help="optional starting config or preset")
    studio.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    studio.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    studio.set_defaults(func=cmd_studio)

    # discovery commands
    presets = subparsers.add_parser("presets", help="list the bundled activity presets")
    presets.set_defaults(func=cmd_presets)

    fmts = subparsers.add_parser("formats", help="list all supported input, export & video formats")
    fmts.set_defaults(func=cmd_formats)

    wdgs = subparsers.add_parser("widgets", help="list all available widgets and themes")
    wdgs.set_defaults(func=cmd_widgets)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args))
    except _Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
