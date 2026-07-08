# ThroughLog — one-step Windows bootstrap.
#
# Verifies Python >= 3.12, creates a local venv, installs ThroughLog with the capture
# extras, seeds config.json from the example, and launches the app (`tl up`).
# Re-running is safe: existing venv/config are reused, never clobbered.
#
#   Right-click -> "Run with PowerShell", or:   powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#   Flags:  -NoLaunch   set up only, don't start the app
#           -Shortcut   also create a desktop + Start-menu launcher

param(
    [switch]$NoLaunch,
    [switch]$Shortcut
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Find-Python {
    foreach ($cmd in @("py -3.12", "py -3", "python", "python3")) {
        $parts = $cmd.Split(" ")
        $exe = $parts[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $ver = & $exe $parts[1..($parts.Length-1)] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $maj, $min = $ver.Split(".")
                if ([int]$maj -gt 3 -or ([int]$maj -eq 3 -and [int]$min -ge 12)) {
                    return ,$parts
                }
            }
        }
    }
    return $null
}

$py = Find-Python
if ($null -eq $py) {
    Write-Host "[tl] Python 3.12+ not found. Install it from https://www.python.org/downloads/ and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "[tl] using Python: $($py -join ' ')"

$venv = Join-Path $repo "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "[tl] creating venv ..."
    & $py[0] $py[1..($py.Length-1)] -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"

Write-Host "[tl] installing ThroughLog + capture extras (this can take a minute) ..."
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -e ".[capture]"

$cfg = Join-Path $repo "config.json"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $repo "config.example.json") $cfg
    Write-Host "[tl] created config.json (edit it later or use the in-app Settings to add your API key)."
}

if ($Shortcut) {
    & $vpy -m throughlog.cli shortcut create
}

if ($NoLaunch) {
    Write-Host "[tl] setup complete. Start the app any time with:  venv\Scripts\python -m throughlog.cli up"
    exit 0
}

Write-Host "[tl] launching the app (Ctrl+C to stop) ..." -ForegroundColor Green
& $vpy -m throughlog.cli up
