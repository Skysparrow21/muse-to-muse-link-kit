@echo off
cd /d "%~dp0"
py -3 muse_relay.py
if errorlevel 1 python muse_relay.py
pause
