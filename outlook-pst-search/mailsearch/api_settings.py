"""Persist dashboard settings, protecting API credentials with Windows DPAPI."""
import ctypes
import json
import logging
import os
import sys
import tempfile
from ctypes import wintypes

logger = logging.getLogger("mailsearch.gateway")
MAX_SETTINGS_BYTES = 128 * 1024
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def user_dpapi(data, *, decrypt=False):
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_ptr = ctypes.POINTER(DataBlob)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [blob_ptr, ctypes.c_void_p if decrypt else wintypes.LPCWSTR, blob_ptr,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = DataBlob(len(data), buffer), DataBlob()
    try:
        if not function(ctypes.byref(source), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.data, result.length)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.length)
            kernel.LocalFree(result.data)


class ApiSettings:
    secure = sys.platform == "win32"

    def __init__(self, root):
        self.path = root / "api-settings.dat"

    def load(self):
        try:
            with self.path.open("rb") as file:
                data = file.read(MAX_SETTINGS_BYTES + 1)
            if not 1 <= len(data) <= MAX_SETTINGS_BYTES:
                raise ValueError("settings_size")
            if self.secure:
                data = user_dpapi(data, decrypt=True)
            settings = json.loads(data)
            if not isinstance(settings, dict):
                raise ValueError("settings_format")
            return settings
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not load saved API settings (%s)", type(exc).__name__)
            return {}

    def save(self, settings):
        if not self.secure:
            settings = {
                name: {k: v for k, v in profile.items() if k != "key"} if isinstance(profile, dict) else profile
                for name, profile in settings.items()
            }
        data = json.dumps(settings, ensure_ascii=False).encode()
        if self.secure:
            data = user_dpapi(data)
        if len(data) > MAX_SETTINGS_BYTES:
            raise ValueError("settings_size")
        write_api_settings(self.path, data)


def write_api_settings(path, data):
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="api-settings-", suffix=".tmp", delete=False) as file:
            pending = file.name
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, path)
        pending = None
    finally:
        if pending is not None:
            os.unlink(pending)
