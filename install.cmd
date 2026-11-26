@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Telemetry Overlay - Instalace

set "REPO_URL=https://github.com/Tomasraketak/TelemetryOverlay.git"
set "ZIP_URL=https://github.com/Tomasraketak/TelemetryOverlay/archive/refs/heads/main.zip"
set "BRANCH=main"

echo ============================================================
echo   Telemetry Overlay - Instalator (Windows CMD)
echo ============================================================
echo.

REM 1) Determine install directory
if defined TELEMETRY_OVERLAY_DIR (
    set "INSTALL_DIR=%TELEMETRY_OVERLAY_DIR%"
    goto :dir_ready
)
if not exist "%~dp0pyproject.toml" goto :default_dir
for %%I in ("%~dp0.") do set "INSTALL_DIR=%%~fI"
goto :dir_ready

:default_dir
set "INSTALL_DIR=%USERPROFILE%\TelemetryOverlay"

:dir_ready
echo [1/5] Cilova slozka: "%INSTALL_DIR%"

REM 2) Find a real Python 3.10+ interpreter (skip Windows Store stub)
set "PY_CMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :py_found
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :py_found
)
python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python3"
    goto :py_found
)

echo [INFO] Python 3.10+ nenalezen. Zkousim nainstalovat pres winget...
where winget >nul 2>&1
if errorlevel 1 goto :py_missing

winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :py_found
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :py_found
)

:py_missing
echo [CHYBA] Nenalezen Python 3.10 nebo novejsi!
echo Nainstalujte Python z https://www.python.org/downloads/ - zaskrtnete "Add Python to PATH"
echo a potom spustte tento prikaz znovu.
pause
exit /b 1

:py_found
echo [2/5] Pouzivam Python: %PY_CMD%

REM 3) Clone or update repository (with Python ZIP fallback if git is missing)
echo [3/5] Stahuji / aktualizuji TelemetryOverlay [%BRANCH%]...
where git >nul 2>&1
if errorlevel 1 goto :download_zip

if exist "%INSTALL_DIR%\.git" (
    git -C "%INSTALL_DIR%" fetch origin %BRANCH% --quiet
    git -C "%INSTALL_DIR%" pull --ff-only origin %BRANCH%
    goto :repo_ready
)
if exist "%INSTALL_DIR%\pyproject.toml" (
    echo       Existujici instalace bez .git - ponechavam soubory.
    goto :repo_ready
)
git clone --branch %BRANCH% "%REPO_URL%" "%INSTALL_DIR%"
if errorlevel 1 (
    echo [CHYBA] Nepodarilo se naklonovat repozitar.
    pause
    exit /b 1
)
goto :repo_ready

:download_zip
if exist "%INSTALL_DIR%\pyproject.toml" goto :repo_ready
echo       Git nenalezen - stahuji ZIP archiv z GitHubu...
%PY_CMD% -c "import io, pathlib, shutil, urllib.request, zipfile; dst=pathlib.Path(r'%INSTALL_DIR%'); dst.mkdir(parents=True, exist_ok=True); data=urllib.request.urlopen('%ZIP_URL%', timeout=30).read(); zf=zipfile.ZipFile(io.BytesIO(data)); [((dst/pathlib.Path(*pathlib.Path(m.filename).parts[1:])).mkdir(parents=True, exist_ok=True) if m.is_dir() else ((dst/pathlib.Path(*pathlib.Path(m.filename).parts[1:])).parent.mkdir(parents=True, exist_ok=True), shutil.copyfileobj(zf.open(m), open(dst/pathlib.Path(*pathlib.Path(m.filename).parts[1:]), 'wb')))) for m in zf.infolist() if len(pathlib.Path(m.filename).parts)>1]"
if errorlevel 1 (
    echo [CHYBA] Nepodarilo se stahnout ZIP archiv z GitHubu.
    pause
    exit /b 1
)

:repo_ready
REM 4) Create virtual environment and install package + ffmpeg bundle
echo [4/5] Vytvarim virtualni prostredi a instaluji zavislosti...
set "VENV_PY=%INSTALL_DIR%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :venv_ready
%PY_CMD% -m venv "%INSTALL_DIR%\.venv"
if errorlevel 1 (
    echo [CHYBA] Nepodarilo se vytvorit .venv prostredi.
    pause
    exit /b 1
)

:venv_ready
"%VENV_PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VENV_PY%" -m pip install -e "%INSTALL_DIR%" --disable-pip-version-check
if errorlevel 1 (
    echo [CHYBA] Instalace Python balicku selhala.
    pause
    exit /b 1
)

REM 5) Generate custom icon and create auto-updating Desktop shortcut
echo [5/5] Vytvarim ikonu na plose s automatickou aktualizaci...
"%VENV_PY%" -m telemetry_overlay install-desktop --repo-dir "%INSTALL_DIR%" --branch %BRANCH%
if errorlevel 1 (
    echo [CHYBA] Nepodarilo se vytvorit ikonu na plose.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   HOTOVO! Telemetry Overlay je nainstalovan!
echo ============================================================
echo   Slozka : "%INSTALL_DIR%"
echo   Ikona  : Na plose byla vytvorena ikona "Telemetry Overlay".
echo            Po kliknuti se aplikace vzdy automaticky aktualizuje
echo            z GitHubu a otevre Studio v prohlizeci.
echo            Muzete na ni i pretahnout CSV/XLSX/GPX/FIT/MP4 soubor!
echo ============================================================
echo.

if defined TELEMETRY_OVERLAY_NO_LAUNCH goto :done
echo Spoustim Telemetry Overlay Studio...
start "Telemetry Overlay Studio" "%VENV_PY%" -m telemetry_overlay studio --open

:done
endlocal
exit /b 0
