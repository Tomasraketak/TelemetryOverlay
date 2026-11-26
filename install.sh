#!/usr/bin/env bash
# Telemetry Overlay — One-line Linux / macOS Installer
# Run:
#   curl -fsSL https://raw.githubusercontent.com/Tomasraketak/TelemetryOverlay/main/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/Tomasraketak/TelemetryOverlay.git"
BRANCH="main"
INSTALL_DIR="${TELEMETRY_OVERLAY_DIR:-$HOME/TelemetryOverlay}"

echo "============================================================"
echo "  Telemetry Overlay - Installer (Linux / macOS)"
echo "============================================================"
echo "[1/5] Target folder: $INSTALL_DIR"

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "[ERROR] Python 3.10+ is required." >&2
  exit 1
fi
echo "[2/5] Using Python: $($PYTHON_BIN --version)"

echo "[3/5] Cloning / updating repository ($BRANCH)..."
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch origin "$BRANCH" --quiet
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

echo "[4/5] Setting up virtual environment and dependencies..."
VENV_PY="$INSTALL_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
fi
"$VENV_PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$VENV_PY" -m pip install -e "$INSTALL_DIR" --disable-pip-version-check

echo "[5/5] Creating auto-updating Desktop icon..."
"$VENV_PY" -m telemetry_overlay install-desktop --repo-dir "$INSTALL_DIR" --branch "$BRANCH"

echo ""
echo "============================================================"
echo "  DONE! Telemetry Overlay installed in: $INSTALL_DIR"
echo "  Click the 'Telemetry Overlay' icon on your Desktop to"
echo "  automatically update from GitHub and launch Studio."
echo "============================================================"
