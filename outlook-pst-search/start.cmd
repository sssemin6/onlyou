@echo off
rem Central embedding on a shared Ollama server (NAS). Edit the two lines below once.
rem On the server run:  ollama pull qwen3-embedding:4b
rem Clear EMBEDDING_URL to embed with a local Ollama on this PC instead.
set "EMBEDDING_URL=http://192.168.0.10:11434"
set "EMBEDDING_MODEL=qwen3-embedding:4b"
setlocal
set "EXTRA="
if defined EMBEDDING_URL set "EXTRA=--embedding-url %EMBEDDING_URL%"
call "%~dp0start-windows.cmd" --embedding-model %EMBEDDING_MODEL% --ollama-port 11434 %EXTRA% %*
endlocal
