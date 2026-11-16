<#
.SYNOPSIS
  Start the three ModernEdge mock client systems (payroll, dispatch, finance)
  with uvicorn — no Docker required.

.DESCRIPTION
  Mirrors mock_systems/README.md "run without Docker" guidance.
  Requires Python 3.12 exactly (mock app.pyc files are 3.12 bytecode).

  Ports:
    Payroll  http://localhost:8081
    Dispatch http://localhost:8082
    Finance  http://localhost:8083

.EXAMPLE
  .\scripts\run_local_mocks.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$MockRoot = Join-Path $RepoRoot "assessment_package\ModernEdge_AI_Solutions_Engineer_Assessment\mock_systems"

if (-not (Test-Path $MockRoot)) {
    Write-Error "mock_systems folder not found at $MockRoot"
}

function Find-Python312 {
    $candidates = @()

    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { $candidates += $found.Source }
    }

    # py launcher: list installed versions
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $lines = & py -0p 2>$null
            foreach ($line in $lines) {
                if ($line -match "3\.12.*?(\w:\\.+python\.exe)") {
                    $candidates += $Matches[1]
                }
            }
        } catch { }
        try {
            $p312 = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
            if ($p312) { $candidates += $p312.Trim() }
        } catch { }
    }

    $pathHints = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "C:\Python312\python.exe"
    )
    $candidates += $pathHints

    foreach ($exe in $candidates | Select-Object -Unique) {
        if (-not $exe -or -not (Test-Path $exe)) { continue }
        try {
            $ver = & $exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver -and $ver.Trim() -eq "3.12") {
                return $exe
            }
        } catch { }
    }
    return $null
}

$Python = Find-Python312
if (-not $Python) {
    Write-Host ""
    Write-Host "ERROR: Python 3.12 is required to run the mock systems (app.pyc is 3.12 bytecode)." -ForegroundColor Red
    Write-Host "Install Python 3.12, then re-run this script." -ForegroundColor Yellow
    Write-Host "Alternatively use Docker from mock_systems:" -ForegroundColor Yellow
    Write-Host "  cd `"$MockRoot`""
    Write-Host "  docker compose up --build"
    Write-Host ""
    exit 1
}

Write-Host "Using Python 3.12: $Python" -ForegroundColor Cyan

Push-Location $MockRoot
try {
    Write-Host "Installing mock_systems requirements..." -ForegroundColor Cyan
    & $Python -m pip install -r requirements.txt --quiet

    $env:SEED_BUNDLE = (Join-Path $MockRoot "seed.bin")
    $env:PAYROLL_DB = (Join-Path $MockRoot "payroll.db")

    $logDir = Join-Path $MockRoot ".local_logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null

    $services = @(
        @{ Name = "payroll";  Port = 8081; AppDir = "payroll"  },
        @{ Name = "dispatch"; Port = 8082; AppDir = "dispatch" },
        @{ Name = "finance";  Port = 8083; AppDir = "finance"  }
    )

    $procs = @()
    foreach ($svc in $services) {
        $logOut = Join-Path $logDir "$($svc.Name).out.log"
        $logErr = Join-Path $logDir "$($svc.Name).err.log"
        Write-Host "Starting $($svc.Name) on port $($svc.Port)..." -ForegroundColor Green
        $argList = @(
            "-m", "uvicorn", "app:app",
            "--app-dir", $svc.AppDir,
            "--host", "0.0.0.0",
            "--port", "$($svc.Port)"
        )
        $p = Start-Process -FilePath $Python `
            -ArgumentList $argList `
            -WorkingDirectory $MockRoot `
            -RedirectStandardOutput $logOut `
            -RedirectStandardError $logErr `
            -PassThru `
            -WindowStyle Hidden
        $procs += [pscustomobject]@{
            Name = $svc.Name
            Port = $svc.Port
            Pid  = $p.Id
            Log  = $logOut
        }
    }

    Start-Sleep -Seconds 2

    Write-Host ""
    Write-Host "Mock systems started:" -ForegroundColor Cyan
    foreach ($p in $procs) {
        Write-Host ("  {0,-8}  http://localhost:{1}  (PID {2})" -f $p.Name, $p.Port, $p.Pid)
    }
    Write-Host ""
    Write-Host "Logs under: $logDir"
    Write-Host "Stop with:  Stop-Process -Id $($procs.Pid -join ',') -Force" -ForegroundColor Yellow
    Write-Host "Or:         Get-Process -Id $($procs.Pid -join ',') | Stop-Process -Force"
    Write-Host ""

    # Persist PIDs for convenience
    $pidFile = Join-Path $logDir "pids.txt"
    $procs | ForEach-Object { "$($_.Name)=$($_.Pid)" } | Set-Content $pidFile
    Write-Host "PIDs written to $pidFile"
}
finally {
    Pop-Location
}
