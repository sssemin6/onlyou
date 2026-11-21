@echo off
rem Start with the bundled readpst and semantic search (Ollama bge-m3, default port).
rem Run setup-windows.cmd once on a new PC; without Ollama the app still runs with keyword search.
call "%~dp0start-windows.cmd" --embedding-model bge-m3 --ollama-port 11434 %*
