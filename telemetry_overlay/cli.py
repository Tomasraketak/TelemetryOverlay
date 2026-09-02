"""Command line interface.

    telemetry-overlay inspect data.csv
    telemetry-overlay init    data.csv -o overlay.yaml
    telemetry-overlay preview data.csv -c overlay.yaml --at 2.3
    telemetry-overlay render  data.csv -c overlay.yaml -o overlay.mov
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    Config, RESOLUTIONS, load_config, parse_resolution, save_config, suggest_config,
)
from .data import LoadOptions, load as load_data
from .events import build_events
from .expressions import add_derived
from .export import (
    VIDEO_CODECS, VIDEO_SUFFIXES, codec_for_path, export_frames,
    export_video, find_ffmpeg,
)
from .render import build_renderer
from .timeline import Hold, Ramp

_PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"


class _Fail(Exception):
    """A user-facing error; printed without a traceback."""


# ---------------------------------------------------------------------------
# Shared argument groups
# ---------------------------------------------------------------------------

def _add_data_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("data reading")
    group.add_argument("--time-column", metavar="NAME",
                       help="column to use as the time axis (default: sniffed)")
    group.add_argument("--time-unit", choices=["s", "ms", "us"],
                       help="unit of the time column (default: from its header)")
    group.add_argument("--delimiter", metavar="CHAR",
                       help="CSV field separator (default: sniffed)")
    group.add_argument("--decimal", choices=[".", ","],
                       help="decimal mark (default: sniffed)")
    group.add_argument("--encoding", metavar="ENC",
                       help="text encoding (default: utf-8 then cp1250)")
    group.add_argument("--sheet", metavar="NAME",
                       help="worksheet to read from an .xlsx file")


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
                       help="frame size, e.g. 1080p, 4k or 2560x1440")
    group.add_argument("--background", metavar="COLOR",
                       help="fill the frame instead of leaving it transparent "
                            "(e.g. '#000000' or '#101010 @ 60%%')")


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


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("fps", "speed", "start", "end", "t0", "resolution", "background",
                "delimiter", "decimal", "time_column", "time_unit", "encoding",
                "sheet", "max_duration"):
        value = getattr(args, key, None)
        if value is not None:
            out[key] = value
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
    return config


def _load_options(args: argparse.Namespace) -> LoadOptions:
    return LoadOptions(
        delimiter=getattr(args, "delimiter", None),
        decimal=getattr(args, "decimal", None),
        time_column=getattr(args, "time_column", None),
        time_unit=getattr(args, "time_unit", None),
        sheet=getattr(args, "sheet", None),
        encoding=getattr(args, "encoding", None),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    telemetry = load_data(args.data, _load_options(args))
    print(telemetry.summary())
    if telemetry.meta:
        print("\nmetadata found in the file:")
        for key, value in list(telemetry.meta.items())[:40]:
            print(f"  {key:<34} {value}")
    events = build_events(telemetry, True)
    if events:
        print("\ndetected events:")
        for event in events:
            print(f"  {event['time']:9.4f} s   {event['label']}")
    print("\nNext step:  telemetry-overlay init "
          f"{args.data} -o overlay.yaml")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    telemetry = load_data(args.data, _load_options(args))
    config = suggest_config(telemetry,
                            resolution=args.resolution or "1080p",
                            fps=args.fps or 60.0)
    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise _Fail(f"{out} already exists - pass --overwrite to replace it")
    written = save_config(config, out)
    print(f"Wrote a starter config to {written}")
    print(f"  channels used : "
          f"{', '.join(w.get('channel', '') for w in config['widgets'] if w.get('channel'))}")
    print(f"\nPreview it with:\n  telemetry-overlay preview {args.data} "
          f"-c {written} --at peak -o preview.png")
    print(f"\nThen render the video:\n  telemetry-overlay render {args.data} "
          f"-c {written} -o overlay.mov")
    return 0


def _resolve_at(value: str, renderer) -> float:
    """Accept a timestamp, a percentage, or the name of a detected event."""
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
    renderer = build_renderer(config, args.data)
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
    renderer = build_renderer(config, args.data)
    print(renderer.describe())

    output = Path(args.output)
    # The output path decides the format: a file extension means video, a
    # bare name means a folder of PNG frames.
    if args.format == "auto":
        as_video = _is_video_target(args.output)
    else:
        as_video = args.format == "video"
        if as_video and not _is_video_target(args.output):
            output = output.with_suffix(
                codec_for_path(output, args.codec).suffix)

    if as_video:
        spec = codec_for_path(output, args.codec)
        print(f"\noutput        : {output}  ({spec.name}, "
              f"{'with alpha' if spec.alpha else 'NO alpha'})")
        if not spec.alpha:
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

    if as_video:
        if find_ffmpeg() is None and args.format == "auto":
            print("\nffmpeg not found - falling back to a PNG sequence.")
            print("  Install it with:  pip install imageio-ffmpeg")
            as_video = False
            output = output.with_suffix("")

    if as_video:
        print(f"\nRendering {renderer.frame_count} frames straight into video ...")
        info = export_video(
            config, args.data, output,
            codec=args.codec,
            workers=args.workers,
            progress=reporter,
            renderer=renderer if single else None,
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
        print("\nDrop it into DaVinci Resolve on a track above your footage;")
        print("  the alpha channel is honoured automatically. If Resolve shows")
        print("  a black background, set the clip's Alpha Mode to 'Premultiplied'")
        print("  in Clip Attributes. See docs/DAVINCI_RESOLVE.md.")
        return 0

    print(f"\nRendering {renderer.frame_count} frames to {output} ...")
    result = export_frames(
        config, args.data, output,
        prefix=args.prefix,
        digits=args.digits,
        workers=args.workers,
        compress_level=args.compression,
        overwrite=args.overwrite,
        progress=reporter,
        renderer=renderer if single else None,
    )
    print(f"\nDone: {result['frames']} PNG frames, "
          f"{result['size'][0]}x{result['size'][1]}, "
          f"{result['duration']:.2f} s at {result['fps']:g} fps")
    print(f"  {result['seconds']:.1f} s of rendering "
          f"({result['frames_per_second']:.1f} frames/s)")
    print(f"  pattern: {result['pattern']}")
    print("\nImport into DaVinci Resolve:")
    print("  Media Pool -> right click -> Import Media, pick the FIRST frame;")
    print("  Resolve reads the numbering and loads the sequence as one clip.")
    print("  See docs/DAVINCI_RESOLVE.md.")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-overlay",
        description="Generate transparent telemetry overlays from CSV/XLSX logs, "
                    "ready to import into DaVinci Resolve.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Full documentation: README.md and docs/",
    )
    parser.add_argument("--version", action="version",
                        version=f"telemetry-overlay {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    inspect = subparsers.add_parser(
        "inspect", help="show the channels, ranges and detected events in a log")
    inspect.add_argument("data", help="CSV, TSV or XLSX file")
    _add_data_args(inspect)
    inspect.set_defaults(func=cmd_inspect)

    init = subparsers.add_parser(
        "init", help="write a starter config tailored to a log")
    init.add_argument("data", help="CSV, TSV or XLSX file")
    init.add_argument("-o", "--output", default="overlay.yaml",
                      help="config file to write (default: overlay.yaml)")
    init.add_argument("-r", "--resolution", help="target resolution, e.g. 4k")
    init.add_argument("--fps", type=float, help="target frame rate")
    init.add_argument("--overwrite", action="store_true",
                      help="replace an existing config file")
    _add_data_args(init)
    init.set_defaults(func=cmd_init)

    preview = subparsers.add_parser(
        "preview", help="render a single frame to check the layout")
    preview.add_argument("data", help="CSV, TSV or XLSX file")
    preview.add_argument("-c", "--config", help="config file or bundled preset name")
    preview.add_argument("-o", "--output", default="preview.png",
                         help="PNG to write (default: preview.png)")
    preview.add_argument("--at", default="peak", metavar="T",
                         help="when to sample: seconds, a percentage like 60%%, "
                              "or an event name such as peak or ignition "
                              "(default: peak)")
    _add_output_args(preview)
    _add_time_args(preview)
    _add_data_args(preview)
    preview.set_defaults(func=cmd_preview)

    render = subparsers.add_parser(
        "render", help="render the animated overlay as video (or PNG frames)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The output path picks the format:\n"
               "  -o overlay.mov    ProRes 4444 with alpha (Resolve-ready)\n"
               "  -o overlay.webm   VP9 with alpha (web)\n"
               "  -o overlay.mp4    H.264, NO alpha - preview only\n"
               "  -o frames/        numbered PNG sequence with alpha")
    render.add_argument("data", help="CSV, TSV or XLSX file")
    render.add_argument("-c", "--config", help="config file or bundled preset name")
    render.add_argument("-o", "--output", default="overlay.mov",
                        help="video file or frame folder (default: overlay.mov)")
    render.add_argument("--format", default="auto",
                        choices=["auto", "video", "frames"],
                        help="force the output format instead of deriving it "
                             "from the output path (default: auto)")
    render.add_argument("--codec", default=None, choices=sorted(VIDEO_CODECS),
                        help="video codec (default: from the file extension, "
                             "i.e. prores4444 for .mov)")
    render.add_argument("--prefix", default="overlay",
                        help="frame name prefix for a PNG sequence "
                             "(default: overlay)")
    render.add_argument("--digits", type=int, default=5,
                        help="digits in the frame number (default: 5)")
    render.add_argument("-j", "--workers", type=int, metavar="N",
                        help="parallel worker processes (default: all cores, max 8)")
    render.add_argument("--compression", type=int, default=3, metavar="0-9",
                        help="PNG compression for a frame sequence; 0 is "
                             "fastest, 9 is smallest (default: 3)")
    render.add_argument("--overwrite", action="store_true",
                        help="replace an existing output")
    render.add_argument("--dry-run", action="store_true",
                        help="print the plan without rendering anything")
    render.add_argument("--quiet", action="store_true", help="no progress bar")
    _add_output_args(render)
    _add_time_args(render)
    _add_data_args(render)
    render.set_defaults(func=cmd_render)

    presets = subparsers.add_parser("presets", help="list the bundled presets")
    presets.set_defaults(func=cmd_presets)

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
