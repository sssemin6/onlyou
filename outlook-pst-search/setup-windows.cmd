@echo off
rem One-time setup on a new PC: checks Python, the bundled PST converter.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
pause
