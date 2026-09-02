"""Writing the rendered overlay out - as video with alpha, or as PNG frames.

Video is the primary output. Frames are piped straight into ffmpeg as raw
RGBA, so nothing but the finished file ever touches the disk. Rendering is
parallelised by cutting the clip into segments, encoding each in its own
process, then concatenating them without re-encoding.

If ffmpeg is not on PATH the bundled one from the ``imageio-ffmpeg`` wheel is
used, which means ``pip install`` is enough to get video output working.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .config import Config
from .render import OverlayRenderer, build_renderer

__all__ = [
    "export_video", "export_frames", "find_ffmpeg", "ffmpeg_available",
    "VIDEO_CODECS", "codec_for_path", "CodecSpec",
]


@dataclass(frozen=True)
class CodecSpec:
    """One encoder configuration."""

    name: str
    args: tuple[str, ...]
    suffix: str
    alpha: bool
    note: str


VIDEO_CODECS: dict[str, CodecSpec] = {
    "prores4444": CodecSpec(
        name="prores4444",
        args=("-c:v", "prores_ks", "-profile:v", "4444",
              "-pix_fmt", "yuva444p10le", "-alpha_bits", "16", "-vendor", "apl0"),
        suffix=".mov", alpha=True,
        note="ProRes 4444 with a real alpha channel. The standard hand-off to "
             "DaVinci Resolve - the free edition imports and plays it.",
    ),
    "qtrle": CodecSpec(
        name="qtrle",
        args=("-c:v", "qtrle", "-pix_fmt", "argb"),
        suffix=".mov", alpha=True,
        note="QuickTime Animation (RLE): lossless with alpha, and very small "
             "for overlays because the empty parts of the frame cost nothing.",
    ),
    "png": CodecSpec(
        name="png",
        args=("-c:v", "png", "-pix_fmt", "rgba"),
        suffix=".mov", alpha=True,
        note="PNG frames inside a MOV: lossless with alpha, largest files.",
    ),
    "vp9": CodecSpec(
        name="vp9",
        args=("-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
              "-crf", "24", "-row-mt", "1"),
        suffix=".webm", alpha=True,
        note="WebM/VP9 with alpha - for web pages, not for Resolve.",
    ),
    "h264": CodecSpec(
        name="h264",
        args=("-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
              "-preset", "medium"),
        suffix=".mp4", alpha=False,
        note="H.264 has NO alpha - the transparent areas turn black. Use it "
             "only for a quick look, never for compositing.",
    ),
}

_SUFFIX_DEFAULT_CODEC = {".mov": "prores4444", ".webm": "vp9", ".mp4": "h264",
                         ".mkv": "prores4444"}
VIDEO_SUFFIXES = tuple(_SUFFIX_DEFAULT_CODEC)


def codec_for_path(path: str | Path, requested: str | None = None) -> CodecSpec:
    """Pick a codec from an explicit name or from the output file extension."""
    if requested:
        if requested not in VIDEO_CODECS:
            raise ValueError(
                f"unknown codec {requested!r}; available: "
                f"{', '.join(VIDEO_CODECS)}")
        return VIDEO_CODECS[requested]
    suffix = Path(path).suffix.lower()
    return VIDEO_CODECS[_SUFFIX_DEFAULT_CODEC.get(suffix, "prores4444")]


def find_ffmpeg() -> str | None:
    """ffmpeg from PATH, else the binary bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - any failure just means "not available"
        return None
    return exe if exe and Path(exe).exists() else None


def ffmpeg_available() -> str | None:
    """Backwards-compatible alias for :func:`find_ffmpeg`."""
    return find_ffmpeg()


def _require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is needed to write a video file and was not found.\n"
            "  Easiest fix:   pip install imageio-ffmpeg\n"
            "  Or install it: https://ffmpeg.org/download.html\n"
            "  Or render a PNG sequence instead, which needs no ffmpeg:\n"
            "      telemetry-overlay render data.csv -o frames/"
        )
    return ffmpeg


def _encode_command(ffmpeg: str, size: tuple[int, int], fps: float,
                    codec: CodecSpec, output: Path) -> list[str]:
    """ffmpeg invocation reading raw RGBA on stdin."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{size[0]}x{size[1]}", "-r", f"{fps:g}",
        "-i", "pipe:0",
        *codec.args,
        "-r", f"{fps:g}",
        str(output),
    ]


# Worker processes build their own renderer once and reuse it; shipping a
# prepared renderer across the process boundary would be far more expensive
# than rebuilding it.
_WORKER: dict[str, Any] = {}


def _init_worker(config: Config, data_path: str) -> None:  # pragma: no cover
    _WORKER["renderer"] = build_renderer(config, data_path)


def _encode_segment(job: tuple[int, int, str, str, tuple[int, int], float, str]
                    ) -> tuple[int, str, int]:
    """Render a run of frames straight into its own video segment."""
    start, stop, out_path, codec_name, size, fps, ffmpeg = job
    renderer: OverlayRenderer = _WORKER["renderer"]
    codec = VIDEO_CODECS[codec_name]
    command = _encode_command(ffmpeg, size, fps, codec, Path(out_path))
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    written = 0
    try:
        assert process.stdin is not None
        for index in range(start, stop):
            frame = renderer.render_frame(index)
            process.stdin.write(frame.tobytes())
            written += 1
        process.stdin.close()
    except BrokenPipeError as exc:  # pragma: no cover - ffmpeg died early
        process.wait()
        stderr = (process.stderr.read().decode(errors="replace")
                  if process.stderr else "")
        raise RuntimeError(f"ffmpeg closed the pipe: {stderr.strip()}") from exc
    code = process.wait()
    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}) on frames {start}-{stop}: "
                           f"{stderr.strip()}")
    return start, out_path, written


def _segment_ranges(total: int, workers: int) -> list[tuple[int, int]]:
    """One segment per worker; more would only add concat overhead."""
    if total <= 0:
        return []
    count = max(1, min(workers, total))
    size = -(-total // count)
    return [(i, min(i + size, total)) for i in range(0, total, size)]


def export_video(
    config: Config,
    data_path: str | Path,
    output: str | Path,
    *,
    codec: str | None = None,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    renderer: OverlayRenderer | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Render the clip straight to a video file, keeping the alpha channel.

    Returns a summary dict describing the file that was written.
    """
    ffmpeg = _require_ffmpeg()
    output = Path(output)
    spec = codec_for_path(output, codec)
    if not output.suffix:
        output = output.with_suffix(spec.suffix)
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} already exists - pass --overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    data_path = str(Path(data_path))
    renderer = renderer or build_renderer(config, data_path)
    total = renderer.frame_count
    size = renderer.size
    fps = config.fps

    if workers is None:
        workers = max(1, min(os.cpu_count() or 1, 8))
    workers = max(1, int(workers))

    began = time.perf_counter()
    done = 0
    if progress:
        progress(0, total)

    if workers == 1 or total < 8:
        # Single stream: render and pipe frame by frame.
        command = _encode_command(ffmpeg, size, fps, spec, output)
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            assert process.stdin is not None
            for index in range(total):
                process.stdin.write(renderer.render_frame(index).tobytes())
                done += 1
                if progress:
                    progress(done, total)
            process.stdin.close()
        except BrokenPipeError as exc:
            process.wait()
            stderr = (process.stderr.read().decode(errors="replace")
                      if process.stderr else "")
            raise RuntimeError(f"ffmpeg closed the pipe: {stderr.strip()}") from exc
        code = process.wait()
        if code != 0:
            stderr = (process.stderr.read().decode(errors="replace")
                      if process.stderr else "")
            raise RuntimeError(f"ffmpeg failed ({code}): {stderr.strip()}")
    else:
        # Parallel: one segment per worker, concatenated without re-encoding.
        with tempfile.TemporaryDirectory(prefix="telemetry-overlay-") as tmp:
            tmp_dir = Path(tmp)
            ranges = _segment_ranges(total, workers)
            jobs = [
                (start, stop, str(tmp_dir / f"seg_{i:04d}{spec.suffix}"),
                 spec.name, size, fps, ffmpeg)
                for i, (start, stop) in enumerate(ranges)
            ]
            segments: dict[int, str] = {}
            with ProcessPoolExecutor(max_workers=workers,
                                     initializer=_init_worker,
                                     initargs=(config, data_path)) as pool:
                futures = [pool.submit(_encode_segment, job) for job in jobs]
                for future in as_completed(futures):
                    start, path, written = future.result()
                    segments[start] = path
                    done += written
                    if progress:
                        progress(done, total)

            listing = tmp_dir / "segments.txt"
            listing.write_text(
                "\n".join(f"file '{segments[start]}'"
                          for start, _ in ranges) + "\n",
                encoding="utf-8",
            )
            concat = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", str(output),
            ]
            result = subprocess.run(concat, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"joining the segments failed: {result.stderr.strip()}")

    elapsed = time.perf_counter() - began
    return {
        "output": str(output),
        "codec": spec.name,
        "alpha": spec.alpha,
        "note": spec.note,
        "frames": total,
        "fps": fps,
        "duration": total / fps if fps else 0.0,
        "size": size,
        "bytes": output.stat().st_size if output.exists() else 0,
        "seconds": elapsed,
        "frames_per_second": total / elapsed if elapsed > 0 else float("inf"),
        "ffmpeg": ffmpeg,
    }


# ---------------------------------------------------------------------------
# PNG sequence
# ---------------------------------------------------------------------------

def _render_chunk(args: tuple[int, int, str, str, int]) -> tuple[int, int]:
    start, stop, out_dir, pattern, compress = args
    renderer: OverlayRenderer = _WORKER["renderer"]
    directory = Path(out_dir)
    written = 0
    for index in range(start, stop):
        renderer.render_frame(index).save(
            directory / (pattern % index), "PNG", compress_level=compress)
        written += 1
    return start, written


def _chunks(total: int, workers: int) -> list[tuple[int, int]]:
    """Split a frame range into roughly four chunks per worker.

    More chunks than workers keeps the pool busy when some frames cost more
    than others, without paying per-frame dispatch overhead.
    """
    if total <= 0:
        return []
    target = max(1, min(total, workers * 4))
    size = max(1, -(-total // target))
    return [(i, min(i + size, total)) for i in range(0, total, size)]


def export_frames(
    config: Config,
    data_path: str | Path,
    out_dir: str | Path,
    *,
    prefix: str = "overlay",
    digits: int = 5,
    workers: int | None = None,
    compress_level: int = 3,
    start: int = 0,
    stop: int | None = None,
    overwrite: bool = False,
    progress: Callable[[int, int], None] | None = None,
    renderer: OverlayRenderer | None = None,
) -> dict[str, Any]:
    """Render the clip to a numbered PNG sequence with alpha.

    Useful when ffmpeg is unavailable, or when an editor prefers frames.
    """
    out_dir = Path(out_dir)
    data_path = str(Path(data_path))
    renderer = renderer or build_renderer(config, data_path)

    total = renderer.frame_count
    stop = total if stop is None else min(stop, total)
    start = max(0, start)
    if stop <= start:
        raise ValueError(f"nothing to render: frame range {start}..{stop}")

    if out_dir.exists():
        existing = sorted(out_dir.glob(f"{prefix}_*.png"))
        if existing and not overwrite:
            raise FileExistsError(
                f"{out_dir} already holds {len(existing)} frame(s) named "
                f"'{prefix}_*.png'. Pass --overwrite to replace them, or pick "
                f"an empty folder - an editor reads a sequence by its "
                f"numbering, so leftovers would be picked up as well."
            )
        for path in existing:
            path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    pattern = f"{prefix}_%0{digits}d.png"
    count = stop - start
    if workers is None:
        workers = max(1, min(os.cpu_count() or 1, 8))
    workers = max(1, int(workers))
    compress_level = max(0, min(9, int(compress_level)))

    began = time.perf_counter()
    done = 0
    if progress:
        progress(0, count)

    if workers == 1:
        for index in range(start, stop):
            renderer.render_frame(index).save(
                out_dir / (pattern % index), "PNG", compress_level=compress_level)
            done += 1
            if progress:
                progress(done, count)
    else:
        jobs = [(a + start, b + start, str(out_dir), pattern, compress_level)
                for a, b in _chunks(count, workers)]
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(config, data_path)) as pool:
            futures = [pool.submit(_render_chunk, job) for job in jobs]
            for future in as_completed(futures):
                _, written = future.result()
                done += written
                if progress:
                    progress(done, count)

    elapsed = time.perf_counter() - began
    return {
        "frames": count,
        "directory": str(out_dir),
        "pattern": pattern,
        "first_frame": str(out_dir / (pattern % start)),
        "fps": config.fps,
        "duration": count / config.fps,
        "seconds": elapsed,
        "frames_per_second": count / elapsed if elapsed > 0 else float("inf"),
        "size": renderer.size,
    }
