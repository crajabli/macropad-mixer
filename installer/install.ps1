# MacroPad Mixer - Windows installer.
#
# Started by "Install MacroPad Mixer (Windows).cmd". Installs the host daemon into
# %LOCALAPPDATA%\macropad-mixer\app (a private Python environment managed by uv) and sets it up
# to start at every login. Safe to run again: it replaces the previous install.
#
# Written for Windows PowerShell 5.1. Keep this file ASCII-only (5.1 reads BOM-less files as ANSI).

$ErrorActionPreference = 'Stop'

$AppRoot = Join-Path $env:LOCALAPPDATA 'macropad-mixer'
$AppDir  = Join-Path $AppRoot 'app'
$LogFile = Join-Path $AppRoot 'macropad-mixer.log'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# Run a native program and throw if it fails. $ErrorActionPreference is relaxed inside so that
# progress output on stderr (uv writes a lot there) is never turned into an error by PowerShell;
# success is judged only by the exit code.
function Invoke-Native([string]$What, [string]$FilePath, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "$What failed (exit code $code)."
    }
}

function Remove-Folder([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force
    } catch {
        # Files may still be locked for a moment by a process that was just stopped.
        Start-Sleep -Seconds 2
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Show-Box([string]$Text, [string]$Icon) {
    try {
        Add-Type -AssemblyName PresentationFramework
        [void][System.Windows.MessageBox]::Show($Text, 'MacroPad Mixer', [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]$Icon)
    } catch {
        Write-Host $Text
    }
}

function Find-HostPackage {
    $candidates = @(
        (Join-Path $PSScriptRoot 'host'),        # CIRCUITPY drive: mixer\install.ps1 + mixer\host
        (Join-Path $PSScriptRoot '..\host')      # repo: installer\install.ps1 + host
    )
    foreach ($dir in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $dir 'pyproject.toml')) {
            return (Resolve-Path -LiteralPath $dir).Path
        }
    }
    throw "Could not find the MacroPad Mixer program files (a 'host' folder with pyproject.toml) next to $PSScriptRoot."
}

function Find-Uv {
    $cmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Path }
    $local = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    return $null
}

try {
    Write-Host 'MacroPad Mixer installer' -ForegroundColor Green
    Write-Host "Installing to $AppDir"

    $HostSrc = Find-HostPackage

    # Run everything from the app folder: the current folder may be the CIRCUITPY drive, whose
    # code.py would shadow Python's standard "code" module when running "python -m".
    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
    Set-Location -LiteralPath $AppDir

    # --- 1. Stop a running daemon (it holds the serial port and locks files in the venv) ---------
    Write-Step 'Stopping MacroPad Mixer if it is running'
    $parentPid = 0
    try {
        $self = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
        if ($self) { $parentPid = [int]$self.ParentProcessId }
    } catch { }
    $stopped = 0
    try {
        $procs = @(Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -like '*macropad_mixer*' -or $_.CommandLine -like '*macropad-mixer.exe*') -and
            $_.ProcessId -ne $PID -and $_.ProcessId -ne $parentPid
        })
        foreach ($p in $procs) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                $stopped++
            } catch { }
        }
    } catch { }
    if ($stopped -gt 0) {
        Write-Host "Stopped $stopped process(es)."
        Start-Sleep -Seconds 1
    } else {
        Write-Host 'Not running.'
    }

    # --- 2. uv (installs Python and the packages) -----------------------------------------------
    Write-Step 'Checking for uv (Python installer)'
    $Uv = Find-Uv
    if (-not $Uv) {
        Write-Host 'uv not found; downloading it from astral.sh ...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $env:UV_NO_MODIFY_PATH = '1'
        $env:UV_INSTALL_DIR = Join-Path $env:USERPROFILE '.local\bin'
        # Run astral's installer in a separate PowerShell so that anything it does (exit, settings)
        # cannot affect this script.
        $uvInstall = '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; irm https://astral.sh/uv/install.ps1 | iex'
        Invoke-Native 'Downloading uv' 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $uvInstall)
        $Uv = Find-Uv
        if (-not $Uv) {
            throw 'uv was downloaded but uv.exe could not be found. Check your internet connection and try again.'
        }
    }
    Write-Host "Using $Uv"

    # --- 3. Copy the program files ---------------------------------------------------------------
    Write-Step 'Copying program files'
    $HostDst = Join-Path $AppDir 'host'
    Remove-Folder $HostDst
    New-Item -ItemType Directory -Path $HostDst -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $HostSrc 'pyproject.toml') -Destination $HostDst -Force
    Copy-Item -LiteralPath (Join-Path $HostSrc 'macropad_mixer') -Destination (Join-Path $HostDst 'macropad_mixer') -Recurse -Force
    $caches = @(Get-ChildItem -LiteralPath $HostDst -Recurse -Directory -Force | Where-Object { $_.Name -eq '__pycache__' })
    foreach ($c in $caches) {
        if (Test-Path -LiteralPath $c.FullName) { Remove-Item -LiteralPath $c.FullName -Recurse -Force }
    }
    Write-Host "Copied from $HostSrc"

    # --- 4. Python environment and packages ------------------------------------------------------
    Write-Step 'Creating the Python environment (downloads Python 3.12 if needed)'
    $Venv = Join-Path $AppDir '.venv'
    Remove-Folder $Venv
    Invoke-Native 'Creating the Python environment' $Uv @('venv', '--python', '3.12', $Venv)
    $Python = Join-Path $Venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "The Python environment was created but $Python is missing."
    }

    Write-Step 'Installing MacroPad Mixer and its packages'
    $Requirement = $HostDst + '[windows]'
    Invoke-Native 'Installing the packages' $Uv @('pip', 'install', '--python', $Python, '--reinstall-package', 'macropad-mixer', $Requirement)

    # --- 5. Start at login (and now) ------------------------------------------------------------
    Write-Step 'Setting up automatic start at login'
    Invoke-Native 'Setting up automatic start' $Python @('-m', 'macropad_mixer', 'autostart', 'install')

    Write-Host ''
    Write-Host 'Done.' -ForegroundColor Green
    $msg = "MacroPad Mixer is installed and running.`n`n" +
           "It starts automatically every time you log in, and the MacroPad works as soon as it is plugged in.`n`n" +
           "Log file:`n$LogFile"
    Show-Box $msg 'Information'
    exit 0
} catch {
    $err = $_.Exception.Message
    Write-Host ''
    Write-Host "ERROR: $err" -ForegroundColor Red
    $msg = "MacroPad Mixer could not be installed.`n`n$err`n`n" +
           "Check your internet connection and run the installer again. The messages in the installer window show what happened."
    Show-Box $msg 'Error'
    exit 1
}
