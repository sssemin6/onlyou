@echo off
setlocal
cd /d "%~dp0"
rem Use the bundled readpst unless a later --readpst argument overrides it.
set "BUNDLED_READPST=%~dp0vendor\readpst-win64\readpst.exe"
set "ARGS=%*"
if exist "%BUNDLED_READPST%" set "ARGS=--readpst "%BUNDLED_READPST%" %*"
rem Reuse Git for Windows' bundled pdftotext when none is on PATH and none was passed.
where pdftotext >nul 2>nul
if not errorlevel 1 goto run
echo %*|find /i "pdftotext">nul
if not errorlevel 1 goto run
if exist "%ProgramFiles%\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%ProgramFiles%\Git\mingw64\bin\pdftotext.exe"
if not defined GIT_PDFTOTEXT if exist "%ProgramFiles(x86)%\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%ProgramFiles(x86)%\Git\mingw64\bin\pdftotext.exe"
if not defined GIT_PDFTOTEXT if exist "%LOCALAPPDATA%\Programs\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%LOCALAPPDATA%\Programs\Git\mingw64\bin\pdftotext.exe"
if defined GIT_PDFTOTEXT set "ARGS=%ARGS% --pdftotext "%GIT_PDFTOTEXT%""
:run
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m mailsearch %ARGS%
exit /b %errorlevel%
:use_python
python -m mailsearch %ARGS%
exit /b %errorlevel%
