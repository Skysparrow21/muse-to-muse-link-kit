# Muse relay - system startup setup (runs with no user logged in)
# Copy this file and setup_relay.bat into the relay folder (next to
# muse_relay.py), then right-click setup_relay.bat and choose
# "Run as administrator".
# What it does:
# - Deletes any old "Muse relay" task.
# - Creates a new "Muse relay" task that starts at system boot, runs as
#   SYSTEM, waits 2 minutes so Tailscale is up, and retries on failure.
# - Starts the task now so you do not have to reboot to test it.
# - The relay data folder carries over; nothing is deleted.

$ErrorActionPreference = "Stop"

# Must run as administrator.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please right-click setup_relay.bat and choose 'Run as administrator'."
    exit 1
}

$relayDir = $PSScriptRoot
$scriptPath = Join-Path $relayDir "muse_relay.py"
if (-not (Test-Path $scriptPath)) {
    Write-Host "muse_relay.py not found next to this script. Copy the relay files into one folder and run again."
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

# Give SYSTEM full control of the relay folder (data and log live here).
try {
    icacls $relayDir /grant "NT AUTHORITY\SYSTEM:(OI)(CI)F" /T | Out-Null
    Write-Host "Granted SYSTEM access to $relayDir"
} catch {
    Write-Host "Warning: could not set permissions on $relayDir. The task may still work if SYSTEM can already read it."
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

# Remove the old task if it is still there.
Unregister-ScheduledTask -TaskName "Muse relay" -Confirm:$false -ErrorAction SilentlyContinue

# Trigger: at system startup, delayed 2 minutes so Tailscale is up.
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT2M"

# Run as SYSTEM, no user logged in, highest privileges.
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Settings: start when available, retry on failure, no time limit.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Action: windowless Python running the relay.
$action = New-ScheduledTaskAction -Execute $pyw -Argument $arguments -WorkingDirectory $relayDir

Register-ScheduledTask -TaskName "Muse relay" -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Action $action -Force | Out-Null
Write-Host "Task 'Muse relay' created: starts at boot, runs as SYSTEM."

# Start it now so you do not have to reboot to test.
Start-ScheduledTask -TaskName "Muse relay"
Start-Sleep -Seconds 5
$task = Get-ScheduledTask -TaskName "Muse relay"
Write-Host "Task state: $($task.State)"
Write-Host "Check relay.log in $relayDir, or ask your Muse to test the relay."
