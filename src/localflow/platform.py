"""Which OS this is, and stand-ins that keep Windows-only modules importable
on a Mac.

Golden Flow began on Windows and most of its desktop code is Win32. Rather
than split every module, the Windows calls stay where they are and, on a
Mac, the DLL handles are stand-ins: setting ``argtypes`` on them works,
calling them raises. The Mac versions of the functions (``localflow.mac``)
replace the Windows ones at the end of each module that has them.
"""

from __future__ import annotations

import ctypes
import sys

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


class _NotHere:
    """A Windows DLL function on a Mac: configurable, never callable."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attr: str):
        return _NotHere(f"{self._name}.{attr}")

    def __setattr__(self, attr: str, value) -> None:
        object.__setattr__(self, attr, value)

    def __call__(self, *args, **kwargs):
        raise OSError(f"{self._name} is Windows only")


def dll(name: str, use_last_error: bool = True):
    """ctypes.WinDLL(name) on Windows; a stand-in elsewhere."""
    if IS_WIN:
        return ctypes.WinDLL(name, use_last_error=use_last_error)
    return _NotHere(name)


def winfunctype(*types):
    """ctypes.WINFUNCTYPE on Windows; CFUNCTYPE elsewhere (never called there)."""
    return (ctypes.WINFUNCTYPE if IS_WIN else ctypes.CFUNCTYPE)(*types)


try:
    from ctypes import wintypes  # works on Windows (and on newer Pythons elsewhere)
except (ImportError, ValueError):  # pragma: no cover - older non-Windows Pythons
    import types as _types

    wintypes = _types.SimpleNamespace(
        BOOL=ctypes.c_int, BYTE=ctypes.c_byte, WORD=ctypes.c_ushort, DWORD=ctypes.c_ulong,
        UINT=ctypes.c_uint, INT=ctypes.c_int, LONG=ctypes.c_long, ULONG=ctypes.c_ulong,
        HANDLE=ctypes.c_void_p, HWND=ctypes.c_void_p, HMONITOR=ctypes.c_void_p,
        HDC=ctypes.c_void_p, HINSTANCE=ctypes.c_void_p, LPARAM=ctypes.c_ssize_t,
        WPARAM=ctypes.c_size_t, LPWSTR=ctypes.c_wchar_p, LPCWSTR=ctypes.c_wchar_p,
        POINT=type("POINT", (ctypes.Structure,), {"_fields_": [("x", ctypes.c_long),
                                                               ("y", ctypes.c_long)]}),
        RECT=type("RECT", (ctypes.Structure,), {"_fields_": [
            ("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long),
            ("bottom", ctypes.c_long)]}),
    )
