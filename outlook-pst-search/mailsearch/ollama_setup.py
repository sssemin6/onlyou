"""Check or explicitly install the local BGE-M3 embedding backend."""
import hashlib
import http.client
import json
import logging
import os
import platform
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

PACKAGE_PERCENTAGE = re.compile(r"(?<![\d.])([0-9]{1,3}(?:[.,][0-9]+)?)\s*%")
PACKAGE_BYTES = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMGT](?:i)?B|B)\s*/\s*"
                           r"([0-9]+(?:[.,][0-9]+)?)\s*([KMGT](?:i)?B|B)", re.I)
UNIT_SCALE = {unit: 1024 ** power for power, unit in enumerate("BKMGT")}
MAX_PULL_EVENT = 64 * 1024
OLLAMA_INSTALLER_URL = "https://github.com/ollama/ollama/releases/download/v0.40.14/OllamaSetup.exe"
OLLAMA_INSTALLER_SHA256 = "e069c7cc7d4808f77684e81efa0b1f853a5190b4b8f070bc8fd63041180e3eba"
TLS_FAILURES = ("crypt_e_no_revocation_check", "sec_e_untrusted_root", "crypt_e_revocation_offline",
                "0x80092012", "0x80090325", "0x80092013", "0x80072f8f",
                "certificate verify failed", "certificate revocation", "ssl certificate problem")
DOWNLOAD_FAILURES = (*TLS_FAILURES, "0x8a150008", "0x8a15000b", "0x80072ee2", "0x80072efd",
                     "downloading failed", "download failed", "failed to download")
logger = logging.getLogger("mailsearch.gateway.ollama")


class InstallCommandError(RuntimeError):
    def __init__(self, code, output):
        self.code = code
        self.output = output
        super().__init__(f"exit {code} (0x{code & 0xffffffff:08X})")

    def contains(self, markers):
        return any(marker in f"{self} {self.output}".lower() for marker in markers)


def windows_tool(name):
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for folder in ("Sysnative", "System32"):
        path = root / folder / name
        if path.is_file():
            return str(path)
    return None


def windows_app_installer_paths():
    # AppX can resolve the installed package even if WindowsApps cannot be listed.
    powershell = windows_tool("WindowsPowerShell/v1.0/powershell.exe")
    if powershell:
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                 "Get-AppxPackage -Name Microsoft.DesktopAppInstaller | "
                 "Sort-Object Version -Descending | Select-Object -ExpandProperty InstallLocation"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=8, creationflags=CREATE_FLAGS, check=True,
            )
            for folder in result.stdout.splitlines():
                path = Path(folder.strip())
                if path.is_absolute() and path.name.startswith("Microsoft.DesktopAppInstaller_"):
                    yield path / "winget.exe"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("App Installer package lookup failed: %s", exc)

    architecture = (os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine()).lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "i386": "x86", "i686": "x86"}.get(architecture, architecture)
    root = Path(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files") / "WindowsApps"
    packages = []
    try:
        for folder in root.glob("Microsoft.DesktopAppInstaller_*"):
            match = re.fullmatch(r"Microsoft\.DesktopAppInstaller_([0-9.]+)_([^_]+)__8wekyb3d8bbwe", folder.name, re.I)
            if match and match[2].lower() in (architecture, "neutral"):
                version = tuple(map(int, match[1].split("."))) if re.fullmatch(r"\d+(\.\d+){3}", match[1]) else ()
                packages.append((version, match[2].lower() == architecture, folder))
    except OSError as exc:
        logger.debug("WindowsApps lookup failed: %s", exc)
    for _, _, folder in sorted(packages, reverse=True):
        yield folder / "winget.exe"


def winget_binary():
    if os.name != "nt":
        return None
    found = shutil.which("winget.exe")
    folder = os.environ.get("LOCALAPPDATA")
    alias = Path(folder) / "Microsoft" / "WindowsApps" / "winget.exe" if folder else None
    seen = set()
    for candidates in ((found, alias), windows_app_installer_paths()):
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            normalized = str(path).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                # A zero-byte app alias may be disabled or fail to launch.
                if not path.is_file():
                    continue
                result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                        timeout=5, creationflags=CREATE_FLAGS, check=True)
                if re.fullmatch(rb"v?\d+(?:\.\d+){1,3}(?:-[\w.]+)?", result.stdout.strip()):
                    logger.info("Using WinGet %s at %s", result.stdout.decode("ascii").strip(), path)
                    return str(path)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("WinGet candidate unavailable: %s (%s)", path, type(exc).__name__)
    return None


def package_progress(line):
    size = PACKAGE_BYTES.search(line)
    if size:
        done, done_unit, total, total_unit = size.groups()
        done = float(done.replace(",", ".")) * UNIT_SCALE[done_unit[0].upper()]
        total = float(total.replace(",", ".")) * UNIT_SCALE[total_unit[0].upper()]
        if 0 < total < float("inf") and done < float("inf"):
            return min(100, int(100 * (done / total)))
    percentage = PACKAGE_PERCENTAGE.search(line)
    if percentage:
        value = float(percentage[1].replace(",", "."))
        if value <= 100:
            return int(value)
    return None


def run_install_command(command, label, report, timeout):
    deadline = time.monotonic() + timeout
    logger.info("%s started", label)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "output.log"
        with path.open("wb") as output:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                  creationflags=CREATE_FLAGS) as process, path.open("rb") as log:
                pending = b""
                tail = ""
                last = None
                try:
                    while True:
                        code = process.poll()
                        chunk = log.read(8192)
                        if chunk or code is not None:
                            lines = re.split(rb"[\r\n]+", pending + chunk)
                            pending = lines.pop()[-2048:] if chunk or code is None else b""
                            for raw in lines:
                                line = raw.decode("utf-8", errors="replace").strip()
                                progress = package_progress(line)
                                if progress is not None:
                                    if progress != last:
                                        report(progress)
                                        last = progress
                                elif line.strip("\\|/- "):
                                    tail = (tail + "\n" + line)[-6000:]
                        if code is not None and not chunk:
                            if code:
                                error = InstallCommandError(code, tail)
                                logger.error("%s failed: %s%s", label, error, tail)
                                raise error
                            logger.info("%s completed", label)
                            return
                        if time.monotonic() >= deadline:
                            logger.error("%s timed out%s", label, tail)
                            raise subprocess.TimeoutExpired(command, timeout)
                        if not chunk:
                            time.sleep(.35)
                finally:
                    if process.poll() is None:
                        process.kill()


def install_ollama(winget, report):
    if winget:
        try:
            run_install_command([
                winget, "install", "--id", "Ollama.Ollama", "--exact", "--source", "winget", "--scope", "user",
                "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
            ], "WinGet Ollama install", report, 900)
            return
        except InstallCommandError as exc:
            if not exc.contains(DOWNLOAD_FAILURES):
                raise
    if ollama_binary():
        return
    curl = windows_tool("curl.exe")
    if not curl:
        raise RuntimeError("Windows curl.exe is unavailable")
    logger.info("Using the pinned official Ollama installer fallback")
    with tempfile.TemporaryDirectory() as directory:
        installer = Path(directory) / "OllamaSetup.exe"
        command = [curl, "--disable", "--fail", "--show-error", "--location", "--max-redirs", "8", "--proto", "=https",
                   "--proto-redir", "=https", "--progress-bar", "--output", str(installer)]
        report(None)
        try:
            run_install_command([*command, OLLAMA_INSTALLER_URL], "Ollama installer download", report, 1800)
        except InstallCommandError as exc:
            if exc.code != 60 and not exc.contains(TLS_FAILURES):
                raise
            logger.warning("Retrying only the pinned Ollama installer download with the published SHA-256 required")
            report(None)
            run_install_command([*command, "--insecure", OLLAMA_INSTALLER_URL], "Ollama installer download retry", report, 1800)
        with installer.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != OLLAMA_INSTALLER_SHA256:
            logger.error("Ollama installer SHA-256 mismatch: expected %s, got %s", OLLAMA_INSTALLER_SHA256, digest)
            raise RuntimeError("Ollama installer SHA-256 mismatch")
        report(None)
        run_install_command([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                            "Verified Ollama installer", report, 900)


def pull_model(ai, report):
    layers = {}
    with ai.response("/api/pull", {"model": BGE_MODEL, "stream": True}, timeout=1800) as response:
        while line := response.readline(MAX_PULL_EVENT + 1):
            if len(line) > MAX_PULL_EVENT:
                raise ValueError("ollama_pull_event")
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("ollama_pull_event")
            if event.get("error"):
                reason = str(event["error"]).lower()
                if any(marker in reason for marker in (*TLS_FAILURES, "x509:", "unknown authority")):
                    raise RuntimeError("BGE-M3 registry TLS certificate verification failed")
                raise RuntimeError("BGE-M3 registry returned a download error")
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
                can_install = bool(winget or (os.name == "nt" and windows_tool("curl.exe")))
                self.update(installed=False, running=False, model_present=None, can_install=can_install)
                if not install or not can_install:
                    return
                operation = "ollama_install_failed"
                self.update(phase="installing", progress=None)
                install_ollama(winget, self.report_progress)
                if not ollama_binary():
                    raise RuntimeError("Ollama executable not found after installer completed")
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
                subprocess.SubprocessError) as exc:
            logger.error("%s: %s", operation, exc)
            self.update(error=operation)
        finally:
            self.update(phase="idle", progress=None)

    def report_progress(self, value):
        self.update(progress=value)
