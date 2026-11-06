@echo off
REM Run the Muse files server in this window (for testing). Ctrl+C to stop.
cd /d "%~dp0"
py -3 muse_files.py
pause
