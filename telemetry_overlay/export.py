"""Writing rendered overlays out: alpha video, composited footage, or PNG sequences.

Optimisations:
- Frame deduplication during freeze-frame holds (reuses exact encoded RGBA bytes
  when consecutive frames share the same data timestamp).
- Safe stderr draining to a temporary file so ffmpeg never deadlocks on verbose output.
- Parallel segment encoding with lossless stream-copy concatenation.
- Direct compositing onto source footage (``export_composite_video``) with
  audio passthrough, sync offset and automatic footage-resolution probing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import Config
from .render import OverlayRenderer, build_renderer

__all__ = [
    "export_video", "export_composite_video", "export_frames",
    "find_ffmpeg", "ffmpeg_available", "probe_video",
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
        note="ProRes 4444 with 16-bit alpha. Industry-standard hand-off to "
             "DaVinci Resolve, Premiere Pro and Final Cut.",
    ),
    "qtrle": CodecSpec(
        name="qtrle",
        args=("-c:v", "qtrle", "-pix_fmt", "argb"),
        suffix=".mov", alpha=True,
        note="QuickTime Animation (RLE): lossless with alpha, and very small "
             "and fast for overlays because empty areas compress to almost zero.",
    ),
    "dnxhr": CodecSpec(
        name="dnxhr",
        args=("-c:v", "dnxhd", "-profile:v", "dnxhr_444", "-pix_fmt", "yuva444p10le"),
        suffix=".mov", alpha=True,
        note="Avid DNxHR 444 with alpha - fast hardware-friendly Resolve/Avid master.",
    ),
    "png": CodecSpec(
        name="png",
        args=("-c:v", "png", "-pix_fmt", "rgba"),
        suffix=".mov", alpha=True,
        note="PNG frames inside a MOV: lossless with alpha.",
    ),
    "vp9": CodecSpec(
        name="vp9",
        args=("-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
              "-crf", "24", "-row-mt", "1"),
        suffix=".webm", alpha=True,
        note="WebM/VP9 with alpha - ideal for web browsers and OBS.",
    ),
    "webp": CodecSpec(
        name="webp",
        args=("-c:v", "libwebp", "-lossless", "0", "-q:v", "80", "-loop", "0",
              "-pix_fmt", "yuva420p"),
        suffix=".webp", alpha=True,
        note="Animated WebP with alpha - compact web/social graphics.",
    ),
    "gif": CodecSpec(
        name="gif",
        args=("-lavfi", "split[s0][s1];[s0]palettegen=reserve_transparent=1[p];"
              "[s1][p]paletteuse=alpha_threshold=128"),
        suffix=".gif", alpha=True,
        note="Animated GIF with transparency - quick sharing & previews.",
    ),
    "h264": CodecSpec(
        name="h264",
        args=("-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
              "-preset", "fast", "-movflags", "+faststart"),
        suffix=".mp4", alpha=False,
        note="H.264 MP4 (no alpha) - best for direct footage compositing or previews.",
    ),
    "h265": CodecSpec(
        name="h265",
        args=("-c:v", "libx265", "-pix_fmt", "yuv420p", "-crf", "20",
              "-preset", "fast", "-tag:v", "hvc1", "-movflags", "+faststart"),
        suffix=".mp4", alpha=False,
        note="H.265 / HEVC MP4 (no alpha) - smaller high-res composites.",
    ),
}

_SUFFIX_DEFAULT_CODEC = {
    ".mov": "prores4444",
    ".webm": "vp9",
    ".mp4": "h264",
    ".mkv": "prores4444",
    ".gif": "gif",
    ".webp": "webp",
}
VIDEO_SUFFIXES = tuple(_SUFFIX_DEFAULT_CODEC)


def codec_for_path(path: str | Path, requested: str | None = None) -> CodecSpec:
    """Pick a codec from an explicit name or from the output file extension."""
    if requested:
        if requested not in VIDEO_CODECS:
            raise ValueError(
                f"unknown codec {requested!r}; available: {', '.join(VIDEO_CODECS)}"
            )
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
    except Exception:  # noqa: BLE001
        return None
    return exe if exe and Path(exe).exists() else None


def ffmpeg_available() -> str | None:
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


def probe_video(video_path: str | Path) -> dict[str, Any]:
    """Read width, height, fps and duration of a footage file using ffmpeg/ffprobe."""
    ffmpeg = _require_ffmpeg()
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,duration",
             "-show_entries", "format=duration",
             "-of", "json", str(video_path)],
            capture_output=True, text=True,
        )
        if res.returncode == 0 and res.stdout.strip():
            info = json.loads(res.stdout)
            stream = (info.get("streams") or [{}])[0]
            fps_str = str(stream.get("r_frame_rate", "30/1"))
            num, _, den = fps_str.partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 30.0
            dur = float(stream.get("duration") or info.get("format", {}).get("duration") or 0.0)
            return {
                "width": int(stream.get("width", 1920)),
                "height": int(stream.get("height", 1080)),
                "fps": fps,
                "duration": dur,
            }

    # Fallback: parse ffmpeg -i stderr
    import re
    res = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video_path)],
                         capture_output=True, text=True)
    text = res.stderr
    m_res = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    m_fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    w, h = (int(m_res.group(1)), int(m_res.group(2))) if m_res else (1920, 1080)
    fps = float(m_fps.group(1)) if m_fps else 30.0
    dur = (
        int(m_dur.group(1)) * 3600 + int(m_dur.group(2)) * 60 + float(m_dur.group(3))
        if m_dur else 0.0
    )
    return {"width": w, "height": h, "fps": fps, "duration": dur}


def _encode_command(
    ffmpeg: str, size: tuple[int, int], fps: float, codec: CodecSpec, output: Path
) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{size[0]}x{size[1]}", "-r", f"{fps:g}",
        "-i", "pipe:0",
        *codec.args,
        "-r", f"{fps:g}",
        str(output),
    ]


def _escape_concat_path(path_str: str) -> str:
    return path_str.replace("'", "'\\''")


_WORKER: dict[str, Any] = {}


def _init_worker(config: Config, data_path: Any) -> None:  # pragma: no cover
    _WORKER["renderer"] = build_renderer(config, data_path)


def _write_frames_to_stdin(
    renderer: OverlayRenderer,
    start: int,
    stop: int,
    stdin,
    progress: Callable[[int, int], None] | None = None,
    total: int = 0,
) -> int:
    """Render frames [start, stop) to ``stdin``, deduplicating identical hold frames."""
    written = 0
    last_t: float | None = None
    last_bytes: bytes | None = None
    for index in range(start, stop):
        t = renderer.timeline.data_time(index)
        if last_bytes is not None and last_t is not None and abs(t - last_t) < 1e-12:
            raw = last_bytes
        else:
            raw = renderer.render_frame(index).tobytes()
            last_t = t
            last_bytes = raw
        stdin.write(raw)
        written += 1
        if progress:
            progress(written, total)
    return written


def _encode_segment(
    job: tuple[int, int, str, str, tuple[int, int], float, str]
) -> tuple[int, str, int]:
    """Render a run of frames straight into its own video segment."""
    start, stop, out_path, codec_name, size, fps, ffmpeg = job
    renderer: OverlayRenderer = _WORKER["renderer"]
    codec = VIDEO_CODECS[codec_name]
    command = _encode_command(ffmpeg, size, fps, codec, Path(out_path))
    with tempfile.TemporaryFile() as err_file:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file
        )
        try:
            assert process.stdin is not None
            written = _write_frames_to_stdin(renderer, start, stop, process.stdin)
            process.stdin.close()
        except BrokenPipeError as exc:  # pragma: no cover
            process.wait()
            err_file.seek(0)
            stderr = err_file.read().decode(errors="replace")
            raise RuntimeError(f"ffmpeg closed the pipe: {stderr.strip()}") from exc
        code = process.wait()
        if code != 0:
            err_file.seek(0)
            stderr = err_file.read().decode(errors="replace")
            raise RuntimeError(
                f"ffmpeg failed ({code}) on frames {start}-{stop}: {stderr.strip()}"
            )
    return start, out_path, written


def _segment_ranges(total: int, workers: int) -> list[tuple[int, int]]:
    if total <= 0:
        return []
    count = max(1, min(workers, total))
    size = -(-total // count)
    return [(i, min(i + size, total)) for i in range(0, total, size)]


def export_video(
    config: Config,
    data_path: str | Path | Sequence[str | Path],
    output: str | Path,
    *,
    codec: str | None = None,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    renderer: OverlayRenderer | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Render the clip straight to a video file, keeping the alpha channel."""
    ffmpeg = _require_ffmpeg()
    output = Path(output)
    spec = codec_for_path(output, codec)
    if not output.suffix:
        output = output.with_suffix(spec.suffix)
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} already exists - pass --overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    norm_data: Any = (
        [str(Path(p)) for p in data_path]
        if isinstance(data_path, (list, tuple))
        else str(Path(data_path))
    )
    renderer = renderer or build_renderer(config, norm_data)
    total = renderer.frame_count
    size = renderer.size
    fps = config.fps

    if workers is None:
        workers = max(1, min(os.cpu_count() or 1, 8))
    workers = max(1, int(workers))
    # GIF / WebP cannot be losslessly stream-concatenated as segments
    if spec.name in ("gif", "webp"):
        workers = 1

    began = time.perf_counter()
    done = 0
    if progress:
        progress(0, total)

    if workers == 1 or total < 8:
        command = _encode_command(ffmpeg, size, fps, spec, output)
        with tempfile.TemporaryFile() as err_file:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file
            )
            try:
                assert process.stdin is not None
                done = _write_frames_to_stdin(
                    renderer, 0, total, process.stdin, progress=progress, total=total
                )
                process.stdin.close()
            except BrokenPipeError as exc:
                process.wait()
                err_file.seek(0)
                stderr = err_file.read().decode(errors="replace")
                raise RuntimeError(f"ffmpeg closed the pipe: {stderr.strip()}") from exc
            code = process.wait()
            if code != 0:
                err_file.seek(0)
                stderr = err_file.read().decode(errors="replace")
                raise RuntimeError(f"ffmpeg failed ({code}): {stderr.strip()}")
    else:
        with tempfile.TemporaryDirectory(prefix="telemetry-overlay-") as tmp:
            tmp_dir = Path(tmp)
            ranges = _segment_ranges(total, workers)
            jobs = [
                (start, stop, str(tmp_dir / f"seg_{i:04d}{spec.suffix}"),
                 spec.name, size, fps, ffmpeg)
                for i, (start, stop) in enumerate(ranges)
            ]
            segments: dict[int, str] = {}
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_worker,
                initargs=(config, norm_data),
            ) as pool:
                futures = [pool.submit(_encode_segment, job) for job in jobs]
                for future in as_completed(futures):
                    start, path, written = future.result()
                    segments[start] = path
                    done += written
                    if progress:
                        progress(done, total)

            listing = tmp_dir / "segments.txt"
            listing.write_text(
                "\n".join(
                    f"file '{_escape_concat_path(segments[start])}'"
                    for start, _ in ranges
                ) + "\n",
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
                    f"joining the segments failed: {result.stderr.strip()}"
                )

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


def export_composite_video(
    config: Config,
    data_path: str | Path | Sequence[str | Path],
    video_path: str | Path,
    output: str | Path,
    *,
    codec: str | None = None,
    sync_offset: float = 0.0,
    progress: Callable[[int, int], None] | None = None,
    renderer: OverlayRenderer | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Composite the animated RGBA telemetry overlay directly onto a source video.

    Preserves the source audio track losslessly (``-c:a copy``) and aligns
    overlay frames with footage using ``sync_offset`` seconds (positive starts
    reading footage later).
    """
    ffmpeg = _require_ffmpeg()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"source video not found: {video_path}")
    output = Path(output)
    spec = codec_for_path(output, codec or "h264")
    if not output.suffix:
        output = output.with_suffix(spec.suffix)
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} already exists - pass --overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    norm_data: Any = (
        [str(Path(p)) for p in data_path]
        if isinstance(data_path, (list, tuple))
        else str(Path(data_path))
    )
    renderer = renderer or build_renderer(config, norm_data)
    total = renderer.frame_count
    size = renderer.size
    fps = config.fps

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if sync_offset > 0:
        cmd.extend(["-ss", f"{sync_offset:.6f}"])
    cmd.extend([
        "-i", str(video_path),
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{size[0]}x{size[1]}", "-r", f"{fps:g}",
        "-i", "pipe:0",
        "-filter_complex",
        f"[0:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
        f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2[bg];"
        f"[bg][1:v]overlay=0:0:shortest=1:format=auto[outv]",
        "-map", "[outv]", "-map", "0:a?",
        *spec.args,
        "-c:a", "copy",
        "-shortest",
        str(output),
    ])

    began = time.perf_counter()
    if progress:
        progress(0, total)

    with tempfile.TemporaryFile() as err_file:
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file
        )
        try:
            assert process.stdin is not None
            done = _write_frames_to_stdin(
                renderer, 0, total, process.stdin, progress=progress, total=total
            )
            process.stdin.close()
        except BrokenPipeError:
            # Source video may be shorter than telemetry (`shortest=1`); check exit code.
            pass
        code = process.wait()
        if code != 0:
            err_file.seek(0)
            stderr = err_file.read().decode(errors="replace")
            raise RuntimeError(f"ffmpeg composite failed ({code}): {stderr.strip()}")

    elapsed = time.perf_counter() - began
    return {
        "output": str(output),
        "video": str(video_path),
        "codec": spec.name,
        "alpha": False,
        "note": f"Composited onto {video_path.name} ({spec.note})",
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
    last_t: float | None = None
    last_path: Path | None = None
    for index in range(start, stop):
        t = renderer.timeline.data_time(index)
        target = directory / (pattern % index)
        if last_path is not None and last_t is not None and abs(t - last_t) < 1e-12:
            shutil.copyfile(last_path, target)
        else:
            renderer.render_frame(index).save(target, "PNG", compress_level=compress)
            last_t = t
            last_path = target
        written += 1
    return start, written


def _chunks(total: int, workers: int) -> list[tuple[int, int]]:
    if total <= 0:
        return []
    target = max(1, min(total, workers * 4))
    size = max(1, -(-total // target))
    return [(i, min(i + size, total)) for i in range(0, total, size)]


def export_frames(
    config: Config,
    data_path: str | Path | Sequence[str | Path],
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
    """Render the clip to a numbered PNG sequence with alpha."""
    out_dir = Path(out_dir)
    norm_data: Any = (
        [str(Path(p)) for p in data_path]
        if isinstance(data_path, (list, tuple))
        else str(Path(data_path))
    )
    renderer = renderer or build_renderer(config, norm_data)

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
        last_t: float | None = None
        last_path: Path | None = None
        for index in range(start, stop):
            t = renderer.timeline.data_time(index)
            target = out_dir / (pattern % index)
            if last_path is not None and last_t is not None and abs(t - last_t) < 1e-12:
                shutil.copyfile(last_path, target)
            else:
                renderer.render_frame(index).save(target, "PNG", compress_level=compress_level)
                last_t = t
                last_path = target
            done += 1
            if progress:
                progress(done, count)
    else:
        jobs = [(a + start, b + start, str(out_dir), pattern, compress_level)
                for a, b in _chunks(count, workers)]
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(config, norm_data)
        ) as pool:
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
