"""Prepare optional document readers before starting the Windows app."""
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

READERS = ("pypdf", "xlrd")


def missing_readers():
    missing = []
    for package in READERS:
        try:
            import_module(package)
        except Exception:
            missing.append(package)
    return missing


def main():
    if os.environ.get("MAILSEARCH_SKIP_PIP") == "1":
        return
    missing = missing_readers()
    if not missing:
        return
    print(f"[onlyou] PDF/XLS 추출 패키지 설치 중 ({', '.join(missing)})...", flush=True)
    local_wheels = Path(__file__).resolve().parent.parent / "wheels"
    configured = os.environ.get("MAILSEARCH_WHEEL_DIR")
    wheel_dir = Path(configured) if configured and Path(configured).is_dir() else local_wheels
    command = [sys.executable, "-m", "pip", "--disable-pip-version-check", "--no-input", "install",
               "--quiet", "--retries", "0", "--timeout", "5"]
    if wheel_dir.is_dir():
        command.extend(["--no-index", "--find-links", str(wheel_dir.resolve())])
    try:
        # Installer output can contain private package-index URLs and credentials.
        subprocess.run([*command, *missing], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        print("[onlyou] PDF/XLS 추출 패키지 설치 실패. 앱은 계속 시작합니다.", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
