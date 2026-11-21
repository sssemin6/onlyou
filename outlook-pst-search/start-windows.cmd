@echo off
setlocal
cd /d "%~dp0"
rem Use the bundled readpst unless a later --readpst argument overrides it.
set "BUNDLED_READPST=%~dp0vendor\readpst-win64\readpst.exe"
set "ARGS=%*"
if exist "%BUNDLED_READPST%" set "ARGS=--readpst "%BUNDLED_READPST%" %*"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m mailsearch %ARGS%
exit /b %errorlevel%
:use_python
python -m mailsearch %ARGS%
exit /b %errorlevel%
