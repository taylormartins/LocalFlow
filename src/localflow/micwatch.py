"""Which microphone is Windows' default right now?

PortAudio (under sounddevice) reads the device list once, when it starts.
Plug in a headset or unplug a USB-C dock and Windows switches its default
microphone, but PortAudio never hears of it. A stream opened before the
change stays on the old mic: silent, or gone altogether. Always-on keeps its
stream open for hours, so it went deaf when a laptop was undocked, while a
hotkey dictation (which opens the mic fresh each time) still worked.

This asks Windows itself (Core Audio, IMMDeviceEnumerator) for the default
recording device's ID: cheap enough to poll every couple of seconds.
"""

from __future__ import annotations

import ctypes
import logging
import threading

log = logging.getLogger(__name__)

_local = threading.local()
E_CAPTURE, E_CONSOLE = 1, 0


def _enumerator():
    """This thread's IMMDeviceEnumerator (COM objects stay on their thread)."""
    e = getattr(_local, "enumerator", None)
    if e is not None:
        return e
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_uint, c_void_p, c_wchar_p

    comtypes.CoInitialize()

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                      (["in"], c_uint, "ctx"), (["in"], c_void_p, "params"),
                      (["out"], POINTER(c_void_p), "out")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_uint, "access"),
                      (["out"], POINTER(c_void_p), "store")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "id")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint), "state")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "flow"),
                      (["in"], c_uint, "mask"), (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "flow"),
                      (["in"], c_uint, "role"), (["out"], POINTER(POINTER(IMMDevice)), "device")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "id"),
                      (["out"], POINTER(POINTER(IMMDevice)), "device")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "cb")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "cb")),
        ]

    clsid = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    e = comtypes.CoCreateInstance(clsid, interface=IMMDeviceEnumerator,
                                  clsctx=comtypes.CLSCTX_INPROC_SERVER)
    _local.enumerator = e
    return e


def _mac_default() -> str:
    """The default input's name, as PortAudio sees it (a Mac has no
    IMMDeviceEnumerator; the listener's own reopen refreshes PortAudio)."""
    try:
        import sounddevice as sd

        info = sd.query_devices(kind="input")
        return str(info.get("name", ""))
    except Exception:
        return ""


def default_input_id() -> str:
    """Windows' default recording device right now ("" if there is none or it
    can't be told)."""
    import sys

    if sys.platform == "darwin":
        return _mac_default()
    try:
        dev = _enumerator().GetDefaultAudioEndpoint(E_CAPTURE, E_CONSOLE)
        ptr = dev.GetId()
        try:
            return ctypes.wstring_at(ptr)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(ptr))
    except Exception as exc:
        # No microphone at all reports "element not found": that's an answer too.
        log.debug("default microphone unknown: %s", exc)
        return ""
