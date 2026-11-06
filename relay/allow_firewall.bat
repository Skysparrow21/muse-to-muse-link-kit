@echo off
REM Run this one as administrator, one time only.
REM Allows inbound TCP on port 8787 from tailnet addresses only (100.64.0.0/10).
netsh advfirewall firewall add rule name="Muse relay (tailnet)" dir=in action=allow protocol=TCP localport=8787 remoteip=100.64.0.0/10
pause
