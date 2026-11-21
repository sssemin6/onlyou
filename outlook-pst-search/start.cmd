@echo off
rem Embedding runs on this PC's Ollama (default port). The model files can live on a
rem NAS share: setup-windows.cmd records the share in OLLAMA_MODELS for you.
call "%~dp0start-windows.cmd" --embedding-model bge-m3 --ollama-port 11434 %*
