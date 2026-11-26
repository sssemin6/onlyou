@echo off
setlocal
title onlyou - Outlook PST Search
set "PYTHONUTF8=1"
cd /d "%~dp0"
rem Use the bundled readpst unless a later --readpst argument overrides it.
set "BUNDLED_READPST=%~dp0vendor\readpst-win64\readpst.exe"
set "ARGS="
if exist "%BUNDLED_READPST%" set "ARGS=--readpst "%BUNDLED_READPST%""
rem Use Git for Windows' pdftotext as a default when none is on PATH.
where pdftotext >nul 2>nul
if not errorlevel 1 goto run
if exist "%ProgramFiles%\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%ProgramFiles%\Git\mingw64\bin\pdftotext.exe"
if not defined GIT_PDFTOTEXT if exist "%ProgramFiles(x86)%\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%ProgramFiles(x86)%\Git\mingw64\bin\pdftotext.exe"
if not defined GIT_PDFTOTEXT if exist "%LOCALAPPDATA%\Programs\Git\mingw64\bin\pdftotext.exe" set "GIT_PDFTOTEXT=%LOCALAPPDATA%\Programs\Git\mingw64\bin\pdftotext.exe"
if defined GIT_PDFTOTEXT set "ARGS=%ARGS% --pdftotext "%GIT_PDFTOTEXT%""
:run
set "PYTHON=python"
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
%PYTHON% -m mailsearch.windows_dependencies
%PYTHON% -u -m mailsearch --console-log %ARGS% %*
set "MAILSEARCH_EXIT_CODE=%errorlevel%"

:finished
if defined OUTLOOK_MAILSEARCH_NO_PAUSE exit /b %MAILSEARCH_EXIT_CODE%
echo(
echo Mail search stopped. Exit code: %MAILSEARCH_EXIT_CODE%
echo Check the messages above or app-runtime.log in the app data folder.
pause
exit /b %MAILSEARCH_EXIT_CODE%
