"""Desktop shortcut installer, app icon generator, and auto-updater.

Powers:
    telemetry-overlay install-desktop   # creates a Desktop icon that auto-updates & opens Studio
    telemetry-overlay update            # pulls latest code from GitHub (git or zip fallback)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

__all__ = [
    "REPO_OWNER",
    "REPO_NAME",
    "DEFAULT_BRANCH",
    "generate_app_icon",
    "resolve_desktop_dir",
    "create_desktop_launcher",
    "self_update",
]

REPO_OWNER = "Tomasraketak"
REPO_NAME = "TelemetryOverlay"
DEFAULT_BRANCH = "main"


def _default_repo_dir() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Procedural high-res application icon (.png + multi-size .ico)
# ---------------------------------------------------------------------------

def generate_app_icon(out_dir: str | Path) -> dict[str, Path]:
    """Generate crisp rocket + thrust-curve app icons (``PNG`` and multi-size ``ICO``)."""
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Draw at 512x512 then LANCZOS-downsample for razor-sharp anti-aliasing
    S = 512
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded dark aerospace badge backdrop
    pad = 22
    radius = 96
    draw.rounded_rectangle((pad, pad, S - pad, S - pad), radius=radius, fill=(13, 20, 34, 255))

    # Subtle vertical top highlight gradient band
    top_glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tg_draw = ImageDraw.Draw(top_glow)
    tg_draw.ellipse((60, -140, S - 60, 260), fill=(36, 62, 108, 120))
    top_glow = top_glow.filter(ImageFilter.GaussianBlur(42))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, S - pad, S - pad), radius=radius, fill=255)
    img.paste(Image.alpha_composite(img, top_glow), (0, 0), mask)
    draw = ImageDraw.Draw(img)

    # Subtle instrument grid rings (drawn via alpha_composite to avoid RGBA overwrite)
    ring_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ring_layer)
    cx, cy = S // 2, int(S * 0.48)
    for r, a in ((175, 42), (122, 52)):
        rdraw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(148, 163, 184, a), width=3)
    img.paste(Image.alpha_composite(img, ring_layer), (0, 0), mask)
    draw = ImageDraw.Draw(img)

    # Glowing thrust-curve wave across bottom half
    curve_pts = []
    for i in range(60):
        u = i / 59.0
        x = 58 + u * (S - 116)
        # Asymmetric rocket burn curve profile
        if u < 0.18:
            val = 0.0
        else:
            v = max(0.0, min(1.0, (u - 0.18) / 0.82))
            val = (v ** 0.55) * (max(0.0, 1.0 - v) ** 1.8) * 3.4
            val = min(1.0, float(val))
        y = 408 - val * 145
        curve_pts.append((x, y))

    glow_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    # Soft filled area under thrust curve
    area_pts = [(curve_pts[0][0], 418)] + curve_pts + [(curve_pts[-1][0], 418)]
    gdraw.polygon(area_pts, fill=(255, 159, 10, 46))
    gdraw.line(curve_pts, fill=(255, 159, 10, 175), width=18, joint="curve")
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(10))
    img.paste(Image.alpha_composite(img, glow_layer), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    draw.line(curve_pts, fill=(255, 184, 48, 255), width=8, joint="curve")

    # Exhaust plume behind nozzle (outer flame + bright core)
    plume_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(plume_layer)
    pdraw.polygon([(cx - 34, 316), (cx + 34, 316), (cx, 456)], fill=(255, 102, 24, 210))
    plume_layer = plume_layer.filter(ImageFilter.GaussianBlur(9))
    img.paste(Image.alpha_composite(img, plume_layer), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    draw.polygon([(cx - 28, 316), (cx + 28, 316), (cx, 444)], fill=(255, 128, 20, 255))
    draw.polygon([(cx - 17, 316), (cx + 17, 316), (cx, 410)], fill=(255, 224, 96, 255))
    draw.polygon([(cx - 9, 316), (cx + 9, 316), (cx, 376)], fill=(255, 252, 232, 255))

    # Swept rocket fins (crimson left & right)
    draw.polygon([(cx - 42, 228), (cx - 104, 312), (cx - 90, 326), (cx - 38, 294)],
                 fill=(230, 57, 70, 255))
    draw.polygon([(cx + 42, 228), (cx + 104, 312), (cx + 90, 326), (cx + 38, 294)],
                 fill=(255, 130, 28, 255))

    # Nozzle bell
    draw.polygon([(cx - 24, 294), (cx + 24, 294), (cx + 32, 320), (cx - 32, 320)],
                 fill=(110, 124, 144, 255))

    # Rocket fuselage (shaded two-tone cylinder + ogive nosecone)
    body_pts_left = [
        (cx, 66), (cx - 26, 114), (cx - 44, 168), (cx - 44, 296), (cx, 296),
    ]
    body_pts_right = [
        (cx, 66), (cx + 26, 114), (cx + 44, 168), (cx + 44, 296), (cx, 296),
    ]
    draw.polygon(body_pts_left, fill=(244, 248, 255, 255))
    draw.polygon(body_pts_right, fill=(204, 214, 228, 255))

    # Orange nosecone cap
    nose_left = [(cx, 66), (cx - 26, 114), (cx - 41, 158), (cx, 158)]
    nose_right = [(cx, 66), (cx + 26, 114), (cx + 41, 158), (cx, 158)]
    draw.polygon(nose_left, fill=(255, 159, 10, 255))
    draw.polygon(nose_right, fill=(224, 120, 6, 255))

    # Roll band + telemetry porthole
    draw.rectangle((cx - 44, 214, cx, 238), fill=(24, 34, 52, 255))
    draw.rectangle((cx, 214, cx + 44, 238), fill=(230, 57, 70, 255))
    draw.ellipse((cx - 18, 172, cx + 18, 208), fill=(15, 23, 42, 255),
                 outline=(56, 189, 248, 255), width=5)
    draw.ellipse((cx - 8, 180, cx + 2, 190), fill=(186, 230, 253, 255))

    # Crisp opaque rim border around app icon tile
    draw.rounded_rectangle(
        (pad, pad, S - pad, S - pad), radius=radius,
        outline=(255, 168, 28, 255), width=8,
    )

    icon_256 = img.resize((256, 256), Image.Resampling.LANCZOS)
    png_path = target_dir / "telemetry_overlay.png"
    ico_path = target_dir / "telemetry_overlay.ico"
    icon_256.save(png_path, "PNG")
    icon_256.save(
        ico_path,
        "ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return {"png": png_path, "ico": ico_path}


# ---------------------------------------------------------------------------
# Desktop directory resolution
# ---------------------------------------------------------------------------

def resolve_desktop_dir(desktop_dir: str | Path | None = None) -> Path:
    """Return the user's real Desktop directory (handles Windows OneDrive & localization)."""
    if desktop_dir is not None:
        return Path(desktop_dir).expanduser().resolve()

    if sys.platform == "win32":
        # CSIDL_DESKTOPDIRECTORY (0x0010) respects OneDrive & Czech "Plocha" redirection
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
        return Path.home() / "Desktop"

    if sys.platform == "darwin":
        return Path.home() / "Desktop"

    # Linux / freedesktop
    xdg = shutil.which("xdg-user-dir")
    if xdg:
        try:
            out = subprocess.check_output([xdg, "DESKTOP"], text=True, timeout=3).strip()
            if out:
                return Path(out)
        except Exception:  # noqa: BLE001
            pass
    for candidate in ("Desktop", "Plocha"):
        p = Path.home() / candidate
        if p.is_dir():
            return p
    return Path.home() / "Desktop"


# ---------------------------------------------------------------------------
# Self-updater (git fast-forward pull with offline-safe ZIP fallback)
# ---------------------------------------------------------------------------

def _pip_reinstall(root: Path, python_exe: str | Path | None = None) -> bool:
    py = str(python_exe or sys.executable)
    cmd = [py, "-m", "pip", "install", "-e", str(root), "--quiet", "--disable-pip-version-check"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return res.returncode == 0


def _update_via_git(root: Path, branch: str, timeout: float, quiet: bool) -> dict[str, Any]:
    def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )

    old_head = _run_git("rev-parse", "HEAD")
    old_sha = old_head.stdout.strip() if old_head.returncode == 0 else ""

    fetch = _run_git("fetch", "origin", branch, "--quiet")
    if fetch.returncode != 0:
        msg = "Offline or remote unreachable — launching installed local version."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "offline", "method": "git", "message": msg}

    remote = _run_git("rev-parse", f"origin/{branch}")
    remote_sha = remote.stdout.strip() if remote.returncode == 0 else ""
    if old_sha and remote_sha and old_sha == remote_sha:
        msg = f"Already up to date ({old_sha[:7]})."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "up_to_date", "method": "git", "commit": old_sha, "message": msg}

    pull = _run_git("pull", "--ff-only", "origin", branch)
    if pull.returncode != 0:
        msg = f"Skipped git fast-forward (local edits present): {pull.stderr.strip()}"
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "skipped", "method": "git", "message": msg}

    new_head = _run_git("rev-parse", "HEAD").stdout.strip()
    if not quiet:
        print(f"[update] Updated {old_sha[:7]} -> {new_head[:7]}. Syncing dependencies...")
    _pip_reinstall(root)
    return {
        "status": "updated",
        "method": "git",
        "old_commit": old_sha,
        "commit": new_head,
        "message": f"Updated {old_sha[:7]} -> {new_head[:7]}",
    }


def _update_via_zip(root: Path, branch: str, timeout: float, quiet: bool) -> dict[str, Any]:
    api_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{branch}"
    zip_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{branch}.zip"
    stamp_file = root / ".telemetry_overlay_commit"
    old_sha = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.is_file() else ""

    req = urllib.request.Request(api_url, headers={"User-Agent": "TelemetryOverlay-Updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        remote_sha = str(info.get("sha", ""))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        msg = "Offline or GitHub unreachable — launching installed local version."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "offline", "method": "zip", "message": msg}

    if old_sha and remote_sha and old_sha == remote_sha:
        msg = f"Already up to date ({old_sha[:7]})."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "up_to_date", "method": "zip", "commit": old_sha, "message": msg}

    if not quiet:
        print(f"[update] Downloading latest version ({remote_sha[:7]})...")
    zip_req = urllib.request.Request(zip_url, headers={"User-Agent": "TelemetryOverlay-Updater"})
    try:
        with urllib.request.urlopen(zip_req, timeout=max(timeout, 25.0)) as resp:
            archive_bytes = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        msg = "Download interrupted — launching installed local version."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "offline", "method": "zip", "message": msg}

    root_resolved = root.resolve()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        for member in zf.infolist():
            parts = Path(member.filename).parts
            if len(parts) <= 1:
                continue
            rel = Path(*parts[1:])
            # Never touch virtualenvs or git internals
            if rel.parts and rel.parts[0] in (".venv", "venv", ".git"):
                continue
            dest = (root_resolved / rel).resolve()
            if not str(dest).startswith(str(root_resolved)):
                continue  # path traversal guard
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src_f, dest.open("wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)

    if remote_sha:
        stamp_file.write_text(remote_sha + "\n", encoding="utf-8")
    if not quiet:
        print(f"[update] Updated to {remote_sha[:7]}. Syncing dependencies...")
    _pip_reinstall(root)
    return {
        "status": "updated",
        "method": "zip",
        "old_commit": old_sha,
        "commit": remote_sha,
        "message": f"Updated to {remote_sha[:7]}",
    }


def self_update(
    repo_dir: str | Path | None = None,
    *,
    branch: str = DEFAULT_BRANCH,
    timeout: float = 8.0,
    quiet: bool = False,
) -> dict[str, Any]:
    """Update the local TelemetryOverlay checkout from GitHub (offline-safe).

    Uses ``git pull --ff-only`` when installed as a git clone, and falls back
    to GitHub ZIP archive updates when git is not available.
    """
    root = Path(repo_dir).resolve() if repo_dir is not None else _default_repo_dir()
    if not (root / "pyproject.toml").is_file():
        msg = f"Not a source install ({root}) — skipping self-update."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "skipped", "method": "none", "message": msg}

    try:
        if (root / ".git").exists() and shutil.which("git"):
            return _update_via_git(root, branch=branch, timeout=timeout, quiet=quiet)
        return _update_via_zip(root, branch=branch, timeout=timeout, quiet=quiet)
    except (subprocess.TimeoutExpired, OSError) as exc:
        msg = f"Update check timed out ({exc}) — launching local version."
        if not quiet:
            print(f"[update] {msg}")
        return {"status": "offline", "method": "none", "message": msg}


# ---------------------------------------------------------------------------
# Cross-platform Desktop shortcut & auto-update launcher creation
# ---------------------------------------------------------------------------

def _windows_launcher_cmd(python_hint: Path, auto_update: bool, branch: str) -> str:
    update_line = (
        f'echo Kontroluji aktualizace Telemetry Overlay...\r\n'
        f'"%PY%" -m telemetry_overlay update --branch {branch}\r\n'
        if auto_update else ""
    )
    return (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        "chcp 65001 >nul 2>&1\r\n"
        "title Telemetry Overlay Studio\r\n"
        'cd /d "%~dp0"\r\n'
        f'set "PY={python_hint}"\r\n'
        'if not exist "%PY%" set "PY=%~dp0.venv\\Scripts\\python.exe"\r\n'
        'if not exist "%PY%" set "PY=python"\r\n'
        "\r\n"
        "echo ============================================================\r\n"
        "echo   Telemetry Overlay Studio\r\n"
        "echo ============================================================\r\n"
        f"{update_line}"
        "echo Spoustim Studio a oteviram prohlizec...\r\n"
        "echo (Pro ukonceni aplikace staci zavrit toto okno.)\r\n"
        "echo.\r\n"
        '"%PY%" -m telemetry_overlay studio --open %*\r\n'
        "if errorlevel 1 (\r\n"
        "    echo.\r\n"
        "    echo [CHYBA] Telemetry Overlay skoncil s chybou.\r\n"
        "    pause\r\n"
        ")\r\n"
    )


def _unix_launcher_sh(repo_dir: Path, python_hint: Path, auto_update: bool, branch: str) -> str:
    update_block = (
        f'echo "Checking for Telemetry Overlay updates..."\n'
        f'"$PY" -m telemetry_overlay update --branch {branch} || true\n'
        if auto_update else ""
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'cd "{repo_dir}"\n'
        f'PY="{python_hint}"\n'
        'if [ ! -x "$PY" ]; then PY="$PWD/.venv/bin/python"; fi\n'
        'if [ ! -x "$PY" ]; then PY="python3"; fi\n'
        f"{update_block}"
        'echo "Starting Telemetry Overlay Studio..."\n'
        'exec "$PY" -m telemetry_overlay studio --open "$@"\n'
    )


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _create_windows_lnk(
    shortcut_path: Path,
    target_path: Path,
    working_dir: Path,
    icon_path: Path,
    description: str,
) -> bool:
    """Create a real Windows ``.lnk`` shortcut with custom icon via PowerShell WScript.Shell."""
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return False

    def _ps_esc(val: str) -> str:
        return val.replace("'", "''")

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{_ps_esc(str(shortcut_path))}'); "
        f"$sc.TargetPath = '{_ps_esc(str(target_path))}'; "
        f"$sc.WorkingDirectory = '{_ps_esc(str(working_dir))}'; "
        f"$sc.IconLocation = '{_ps_esc(str(icon_path))},0'; "
        f"$sc.Description = '{_ps_esc(description)}'; "
        "$sc.Save()"
    )
    res = subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )
    return res.returncode == 0 and shortcut_path.exists()


def create_desktop_launcher(
    repo_dir: str | Path | None = None,
    desktop_dir: str | Path | None = None,
    *,
    app_name: str = "Telemetry Overlay",
    python_exe: str | Path | None = None,
    auto_update: bool = True,
    branch: str = DEFAULT_BRANCH,
    platform_override: str | None = None,
) -> dict[str, Path]:
    """Create an auto-updating launcher script, app icon, and Desktop shortcut.

    Returns paths for ``launcher``, ``shortcut``, ``icon_png``, and ``icon_ico``.
    """
    root = Path(repo_dir).resolve() if repo_dir is not None else _default_repo_dir()
    desk = resolve_desktop_dir(desktop_dir)
    desk.mkdir(parents=True, exist_ok=True)

    assets_dir = root / "assets"
    icons = generate_app_icon(assets_dir)
    py_path = Path(python_exe).resolve() if python_exe else Path(sys.executable).resolve()
    plat = platform_override or sys.platform

    if plat == "win32":
        launcher = root / "launch_telemetry_overlay.cmd"
        launcher.write_text(
            _windows_launcher_cmd(py_path, auto_update=auto_update, branch=branch),
            encoding="utf-8",
            newline="",
        )
        lnk = desk / f"{app_name}.lnk"
        created = _create_windows_lnk(
            shortcut_path=lnk,
            target_path=launcher,
            working_dir=root,
            icon_path=icons["ico"],
            description="Telemetry Overlay Studio (auto-updates from GitHub and launches)",
        )
        if created:
            shortcut = lnk
        else:
            # Fallback wrapper .cmd directly on Desktop if WScript.Shell is unavailable
            shortcut = desk / f"{app_name}.cmd"
            shortcut.write_text(
                f'@echo off\r\ncall "{launcher}" %*\r\n',
                encoding="utf-8",
                newline="",
            )
    elif plat == "darwin":
        launcher = root / "launch_telemetry_overlay.sh"
        launcher.write_text(
            _unix_launcher_sh(root, py_path, auto_update=auto_update, branch=branch),
            encoding="utf-8",
        )
        _make_executable(launcher)
        shortcut = desk / f"{app_name}.command"
        shortcut.write_text(
            f'#!/usr/bin/env bash\nexec "{launcher}" "$@"\n',
            encoding="utf-8",
        )
        _make_executable(shortcut)
    else:
        launcher = root / "launch_telemetry_overlay.sh"
        launcher.write_text(
            _unix_launcher_sh(root, py_path, auto_update=auto_update, branch=branch),
            encoding="utf-8",
        )
        _make_executable(launcher)
        safe_id = app_name.replace(" ", "")
        shortcut = desk / f"{safe_id}.desktop"
        desktop_entry = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={app_name}\n"
            "Comment=Telemetry Overlay Studio (auto-updates and opens Web UI)\n"
            f'Exec="{launcher}" %F\n'
            f"Path={root}\n"
            f"Icon={icons['png']}\n"
            "Terminal=true\n"
            "Categories=AudioVideo;Video;Science;Utility;\n"
        )
        shortcut.write_text(desktop_entry, encoding="utf-8")
        _make_executable(shortcut)

    return {
        "launcher": launcher,
        "shortcut": shortcut,
        "icon_png": icons["png"],
        "icon_ico": icons["ico"],
    }
