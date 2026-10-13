@echo off
rem Double-click to install LocalFlow. Same as running install.ps1; any
rem arguments are passed through (e.g. install.cmd -Cleanup rules).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
