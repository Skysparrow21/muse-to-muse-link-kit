# Muse files - scheduled task setup (runs with no user logged in)
# Copy this file and setup_files.bat into the files folder (next to
# muse_files.py), then right-click setup_files.bat and choose
# "Run as administrator".
#
# What was wrong with calling bare pyw.exe:
# - Under SYSTEM the launcher cannot see a per-user Python install, so
#   the task fails and the server never starts. The server code itself
#   is fine.
# - No boot delay, so at startup the tailnet may not be up yet when the
#   server tries to bind to the tailnet IP.
#
# This version resolves the real pythonw.exe at install time, sets the
# working directory explicitly, and delays the boot start by 2 minutes.

$ErrorActionPreference = "Stop"

# Must run as administrator.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please right-click setup_files.bat and choose 'Run as administrator'."
    exit 1
}

$filesDir = $PSScriptRoot
$scriptPath = Join-Path $filesDir "muse_files.py"
if (-not (Test-Path $scriptPath)) {
    Write-Host "muse_files.py not found next to this script. Copy the files folder contents into one folder and run again."
    exit 1
}

# Find the real windowless Python, so SYSTEM does not have to guess.
$pyw = $null
$useLauncher = $false
try {
    $pythonExe = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim()
} catch {
    $pythonExe = $null
}
if ($pythonExe -and (Test-Path $pythonExe)) {
    $candidate = $pythonExe -replace "python\.exe$", "pythonw.exe"
    if (Test-Path $candidate) { $pyw = $candidate } else { $pyw = $pythonExe }
}
if (-not $pyw) {
    $cmd = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source; $useLauncher = $true }
}
if (-not $pyw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source }
}
if (-not $pyw) {
    Write-Host "Could not find pyw.exe or pythonw.exe. Is Python installed?"
    exit 1
}
Write-Host "Using Python: $pyw"

if ($useLauncher) {
    $arguments = "-3 `"$scriptPath`""
} else {
    $arguments = "`"$scriptPath`""
}

# Give SYSTEM full control of this folder (the served tree lives under it).
try {
    icacls $filesDir /grant "NT AUTHORITY\SYSTEM:(OI)(CI)F" /T | Out-Null
    Write-Host "Granted SYSTEM access to $filesDir"
} catch {
    Write-Host "Warning: could not set permissions on $filesDir. The task may still work if SYSTEM can already read it."
}

# Give SYSTEM read/execute on the Python folder, unless it is C:\Windows.
$pythonDir = Split-Path $pyw -Parent
if ($pythonDir -and ($pythonDir -ne $env:SystemRoot)) {
    try {
        icacls $pythonDir /grant "NT AUTHORITY\SYSTEM:(OI)(CI)RX" /T | Out-Null
        Write-Host "Granted SYSTEM read access to $pythonDir"
    } catch {
        Write-Host "Warning: could not set permissions on $pythonDir."
    }
}

# Firewall rule for the file server (only if missing).
$rule = Get-NetFirewallRule -DisplayName "Muse files 8788" -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName "Muse files 8788" -Direction Inbound -Protocol TCP -LocalPort 8788 -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
    Write-Host "Firewall rule added."
} else {
    Write-Host "Firewall rule already present."
}

# Remove the old task if it is still there.
Unregister-ScheduledTask -TaskName "Muse files" -Confirm:$false -ErrorAction SilentlyContinue

# Trigger: at system startup, delayed 2 minutes so Tailscale is up.
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT2M"

# Run as SYSTEM, no user logged in, highest privileges.
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Settings: start when available, retry on failure, no time limit.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Action: windowless Python running the file server.
$action = New-ScheduledTaskAction -Execute $pyw -Argument $arguments -WorkingDirectory $filesDir

Register-ScheduledTask -TaskName "Muse files" -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Action $action -Force | Out-Null
Write-Host "Task 'Muse files' created: starts at boot, runs as SYSTEM."

# Start it now so you do not have to reboot to test.
Start-ScheduledTask -TaskName "Muse files"
Start-Sleep -Seconds 10
$info = Get-ScheduledTaskInfo -TaskName "Muse files"
Write-Host "LastRunTime: $($info.LastRunTime)  LastTaskResult: $($info.LastTaskResult)"
$listen = Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host "Server is listening on 8788."
} else {
    Write-Host "Not listening yet - check the log:"
    Write-Host "  $filesDir\muse_files.log"
}
Write-Host "Done."
