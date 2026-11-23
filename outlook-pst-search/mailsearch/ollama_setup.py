"""Check or explicitly install the local BGE-M3 embedding backend."""
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .answer import ollama_binary
from .embedding import MODEL as BGE_MODEL
from .extract import CREATE_FLAGS

PACKAGE_PERCENTAGE = re.compile(r"(?<![\d.])([0-9]{1,3})\s*%")
PACKAGE_BYTES = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMGT](?:i)?B|B)\s*/\s*"
                           r"([0-9]+(?:[.,][0-9]+)?)\s*([KMGT](?:i)?B|B)", re.I)
UNIT_SCALE = {unit: 1024 ** power for power, unit in enumerate("BKMGT")}
MAX_PULL_EVENT = 64 * 1024


def winget_binary():
    if os.name != "nt":
        return None
    found = shutil.which("winget.exe")
    if found:
        return found
    folder = os.environ.get("LOCALAPPDATA")
    alias = Path(folder) / "Microsoft" / "WindowsApps" / "winget.exe" if folder else None
    return str(alias) if alias and alias.is_file() else None


def package_progress(line):
    size = PACKAGE_BYTES.search(line)
    if size:
        done, done_unit, total, total_unit = size.groups()
        done = float(done.replace(",", ".")) * UNIT_SCALE[done_unit[0].upper()]
        total = float(total.replace(",", ".")) * UNIT_SCALE[total_unit[0].upper()]
        if 0 < total < float("inf") and done < float("inf"):
            return min(100, int(100 * (done / total)))
    percentage = PACKAGE_PERCENTAGE.search(line)
    if percentage and int(percentage[1]) <= 100:
        return int(percentage[1])
    return None


def install_ollama(winget, report):
    command = [
        winget, "install", "--id", "Ollama.Ollama", "--exact", "--source", "winget", "--scope", "user",
        "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
    ]
    deadline = time.monotonic() + 900
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "winget.log"
        with path.open("wb") as output, path.open("rb") as log, subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=CREATE_FLAGS,
        ) as process:
            pending = ""
            try:
                while True:
                    code = process.poll()
                    lines = re.split(r"[\r\n]+", pending + log.read(8192).decode("utf-8", errors="replace"))
                    pending = lines.pop()[-1024:] if code is None else ""
                    for line in lines:
                        progress = package_progress(line)
                        if progress is not None:
                            report(progress)
                    if code is not None:
                        if code:
                            raise subprocess.CalledProcessError(code, command)
                        return
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, 900)
                    time.sleep(.35)
            finally:
                if process.poll() is None:
                    process.kill()


def pull_model(ai, report):
    layers = {}
    with ai.response("/api/pull", {"model": BGE_MODEL, "stream": True}, timeout=1800) as response:
        while line := response.readline(MAX_PULL_EVENT + 1):
            if len(line) > MAX_PULL_EVENT:
                raise ValueError("ollama_pull_event")
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("error"):
                raise ValueError("ollama_pull_event")
            if event.get("status") == "success":
                report(100)
                return
            digest, total, done = event.get("digest"), event.get("total"), event.get("completed", 0)
            if isinstance(digest, str) and type(total) is int and type(done) is int and 0 < total and 0 <= done <= total:
                layers[digest] = done, total
                received, expected = map(sum, zip(*layers.values()))
                report(min(99, 100 * received // expected))
    raise ValueError("ollama_pull_incomplete")


class OllamaSetup:
    def __init__(self, ai):
        self.ai = ai
        self.lock = threading.Lock()
        self.status = {
            "phase": "idle", "installed": None, "running": None, "model_present": None,
            "can_install": False, "error": "", "progress": None,
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
                                   can_install=False, error="", progress=None)
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
                self.update(phase="installing", progress=None)
                install_ollama(winget, self.report_progress)
                if not ollama_binary():
                    self.update(error=operation)
                    return
                operation = "ollama_not_started"
                self.update(phase="checking", installed=True, can_install=False, progress=None)
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
                self.update(phase="downloading", progress=None)
                pull_model(self.ai, self.report_progress)
                self.ai.ensure_model(BGE_MODEL)
            self.update(model_present=True, can_install=False)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, http.client.HTTPException,
                subprocess.SubprocessError):
            self.update(error=operation)
        finally:
            self.update(phase="idle", progress=None)

    def report_progress(self, value):
        self.update(progress=value)
