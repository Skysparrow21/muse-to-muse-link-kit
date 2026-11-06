@echo off
REM Muse relay - system startup setup.
REM Copy this file and setup_relay.ps1 into the relay folder (next to
REM muse_relay.py), then right-click this file and choose
REM "Run as administrator".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_relay.ps1"
pause
