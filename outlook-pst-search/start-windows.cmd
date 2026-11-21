@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m mailsearch %*
exit /b %errorlevel%
:use_python
python -m mailsearch %*
exit /b %errorlevel%
