@echo off
REM Muse files - scheduled task setup.
REM Copy this file and setup_files.ps1 into the files folder (next to
REM muse_files.py), then right-click this file and choose
REM "Run as administrator".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_files.ps1"
pause
