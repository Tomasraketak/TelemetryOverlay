# Telemetry Overlay — One-line Windows Installer (PowerShell / CMD)
# Run directly from Windows Command Prompt (CMD) or PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Tomasraketak/TelemetryOverlay/main/install.ps1 | iex"

$ErrorActionPreference = "Stop"

$RepoOwner = "Tomasraketak"
$RepoName  = "TelemetryOverlay"
$Branch    = "main"
$RepoUrl   = "https://github.com/$RepoOwner/$RepoName.git"
$ZipUrl    = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Branch.zip"

$InstallDir = if ($env:TELEMETRY_OVERLAY_DIR) {
    $env:TELEMETRY_OVERLAY_DIR
} else {
    Join-Path $env:USERPROFILE $RepoName
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Telemetry Overlay - Instalator (Windows)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "[1/5] Cilova slozka: $InstallDir"

function Find-Python {
    foreach ($candidate in @(
        @("py", @("-3")),
        @("python", @()),
        @("python3", @())
    )) {
        $exe = $candidate[0]
        $prefix = $candidate[1]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $args = $prefix + @("-c", "import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)")
                $out = & $exe @args 2>$null
                if ($LASTEXITCODE -eq 0 -and $out) {
                    return ($out | Select-Object -First 1).Trim()
                }
            } catch {}
        }
    }
    return $null
}

$PythonExe = Find-Python
if (-not $PythonExe) {
    Write-Host "[INFO] Python 3.10+ nenalezen. Zkousim nainstalovat pres winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $env:Path = "$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts;$env:Path"
        $PythonExe = Find-Python
    }
}
if (-not $PythonExe) {
    throw "Nenalezen Python 3.10+. Nainstalujte Python z https://www.python.org/downloads/ (zaskrtnete 'Add Python to PATH') a spustte prikaz znovu."
}
Write-Host "[2/5] Pouzivam Python: $PythonExe"

Write-Host "[3/5] Stahuji / aktualizuji TelemetryOverlay ($Branch)..."
$HasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ($HasGit) {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        & git -C $InstallDir fetch origin $Branch --quiet
        & git -C $InstallDir pull --ff-only origin $Branch
    } elseif (-not (Test-Path (Join-Path $InstallDir "pyproject.toml"))) {
        & git clone --branch $Branch $RepoUrl $InstallDir
    }
} else {
    Write-Host "      Git nenalezen - stahuji ZIP archiv z GitHubu..." -ForegroundColor DarkGray
    $tmpZip = Join-Path $env:TEMP "telemetry_overlay_main.zip"
    $tmpExt = Join-Path $env:TEMP "telemetry_overlay_extract"
    if (Test-Path $tmpExt) { Remove-Item $tmpExt -Recurse -Force }
    Invoke-WebRequest -Uri $ZipUrl -OutFile $tmpZip -UseBasicParsing
    Expand-Archive -Path $tmpZip -DestinationPath $tmpExt -Force
    $srcDir = (Get-ChildItem $tmpExt | Select-Object -First 1).FullName
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Get-ChildItem -Path $srcDir -Force | Where-Object { $_.Name -ne ".venv" } | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination $InstallDir -Recurse -Force
    }
    Remove-Item $tmpZip, $tmpExt -Recurse -Force
}

Write-Host "[4/5] Vytvarim virtualni prostredi a instaluji zavislosti..."
$VenvDir = Join-Path $InstallDir ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    & $PythonExe -m venv $VenvDir
}
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -e $InstallDir --disable-pip-version-check

Write-Host "[5/5] Vytvarim ikonu na plose (s automatickou aktualizaci)..."
& $VenvPy -m telemetry_overlay install-desktop --repo-dir $InstallDir --branch $Branch

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  HOTOVO! Telemetry Overlay je nainstalovan!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Slozka : $InstallDir"
Write-Host "  Ikona  : Na plose mate ikonu 'Telemetry Overlay'."
Write-Host "           Po kliknuti se vzdy aktualizuje z GitHubu a otevre Studio."
Write-Host "           (Muzete na ni i primo pretahnout CSV/XLSX/GPX/FIT/MP4 soubor!)"
Write-Host "============================================================" -ForegroundColor Green

if (-not $env:TELEMETRY_OVERLAY_NO_LAUNCH) {
    Write-Host "Spoustim Telemetry Overlay Studio..." -ForegroundColor Cyan
    Start-Process -FilePath $VenvPy -ArgumentList "-m", "telemetry_overlay", "studio", "--open" -WorkingDirectory $InstallDir
}
