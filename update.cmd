@echo off
rem Double-click to update LocalFlow to the latest version (settings are kept).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
if %errorlevel%==3 echo LocalFlow is already up to date.
echo.
pause
