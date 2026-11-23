"""Check or explicitly install the local BGE-M3 embedding backend."""
import http.client
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .answer import ollama_binary
from .embedding import MODEL as BGE_MODEL
from .extract import CREATE_FLAGS


def winget_binary():
    if os.name != "nt":
        return None
    found = shutil.which("winget.exe")
    if found:
        return found
    folder = os.environ.get("LOCALAPPDATA")
    alias = Path(folder) / "Microsoft" / "WindowsApps" / "winget.exe" if folder else None
    return str(alias) if alias and alias.is_file() else None


def install_ollama(winget):
    subprocess.run([
        winget, "install", "--id", "Ollama.Ollama", "--exact", "--source", "winget", "--scope", "user",
        "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_FLAGS, timeout=900, check=True)


class OllamaSetup:
    def __init__(self, ai):
        self.ai = ai
        self.lock = threading.Lock()
        self.status = {
            "phase": "idle", "installed": None, "running": None, "model_present": None,
            "can_install": False, "error": "",
        }

    def snapshot(self):
        with self.lock:
            return self.status.copy()

    def update(self, **fields):
        with self.lock:
            self.status.update(fields)

    def start(self, action):
        if action not in ("check", "install"):
            raise ValueError("ollama_setup_action")
        with self.lock:
            if self.status["phase"] == "idle":
                self.status.update(phase="checking", installed=None, running=None, model_present=None,
                                   can_install=False, error="")
                threading.Thread(target=self.run, args=(action == "install",), daemon=True).start()
            return self.status.copy()

    def run(self, install):
        operation = "ollama_not_started"
        try:
            self.update(installed=bool(ollama_binary()))
            try:
                self.ai.ensure_server()
            except OSError as exc:
                if exc.args != ("ollama_not_found",):
                    raise
                winget = winget_binary()
                self.update(installed=False, running=False, model_present=None, can_install=bool(winget))
                if not install or not winget:
                    return
                operation = "ollama_install_failed"
                self.update(phase="installing")
                install_ollama(winget)
                if not ollama_binary():
                    self.update(error=operation)
                    return
                operation = "ollama_not_started"
                self.update(phase="checking", installed=True, can_install=False)
                self.ai.ensure_server()
            self.update(installed=True, running=True, can_install=False)
            operation = "ollama_model_check_failed"
            try:
                self.ai.ensure_model(BGE_MODEL)
            except RuntimeError as exc:
                if exc.args != ("local_model_missing",):
                    raise
                self.update(model_present=False, can_install=True)
                if not install:
                    return
                operation = "ollama_model_download_failed"
                self.update(phase="downloading")
                result = self.ai.call("/api/pull", {"model": BGE_MODEL, "stream": False}, timeout=1800)
                if not isinstance(result, dict) or result.get("status") != "success":
                    self.update(error=operation)
                    return
                self.ai.ensure_model(BGE_MODEL)
            self.update(model_present=True, can_install=False)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, http.client.HTTPException,
                subprocess.SubprocessError):
            self.update(error=operation)
        finally:
            self.update(phase="idle")
