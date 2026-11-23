"""Resolve source and storage paths and protect a PST against concurrent Windows writes."""
import contextlib
import os
from pathlib import Path


LOCAL_DRIVES = (2, 3, 5, 6)
DRIVE_REMOTE = 4


def windows_drive_type(path):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    return get_drive_type(path.anchor)


def resolve_path(path, *, strict, allow_network):
    resolved = Path(path).expanduser().resolve(strict=strict)
    drive_type = windows_drive_type(resolved)
    if drive_type is None:
        return resolved
    network = drive_type == DRIVE_REMOTE or is_unc_share(resolved.drive)
    if network:
        if allow_network:
            return resolved
    elif drive_type in LOCAL_DRIVES:
        return resolved
    raise OSError("Unsupported storage for this path")


def is_unc_share(drive):
    # Windows resolves mapped drives and DFS links to UNC on some systems.
    if drive.casefold().startswith("\\\\?\\unc\\"):
        drive = "\\\\" + drive[8:]
    if not drive.startswith("\\\\"):
        return False
    host, separator, share = drive[2:].partition("\\")
    return bool(host and host not in {".", "?"} and separator and share and "\\" not in share)


def local_path(path, strict=False):
    return resolve_path(path, strict=strict, allow_network=False)


def source_path(path):
    return resolve_path(path, strict=True, allow_network=True)


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
