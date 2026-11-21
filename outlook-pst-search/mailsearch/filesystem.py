"""Resolve local files and protect a PST against concurrent Windows writes."""
import contextlib
import os
from pathlib import Path


def local_path(path, strict=False):
    path = Path(path).expanduser().resolve(strict=strict)
    if os.name == "nt":
        import ctypes
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(path.anchor)
        if drive_type not in (2, 3, 5, 6):
            raise OSError("Local storage required")
    return path


@contextlib.contextmanager
def stable_input(path):
    if os.name != "nt":
        with path.open("rb"):
            yield
        return

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    opened = kernel.CreateFileW
    opened.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    opened.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # Permit concurrent readers while excluding writers and atomic replacements during export.
    handle = opened(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)
