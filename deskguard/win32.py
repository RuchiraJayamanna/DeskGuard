"""Small Windows helpers implemented with ctypes/winreg (standard library only).

Every function is safe to call on other platforms: it either does nothing or
returns a neutral value, so the rest of the app (and the tests) can run
anywhere.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# Window helpers
# --------------------------------------------------------------------------- #
def set_rounded_corners(hwnd: int) -> bool:
    """Ask Windows 11 DWM to round the window corners. Silently no-op elsewhere."""
    if not IS_WINDOWS:
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
        )
        return hr == 0
    except (OSError, AttributeError):
        return False  # Windows 10 or DWM unavailable


def set_topmost(hwnd: int, on_top: bool) -> bool:
    """Change always-on-top without re-creating or activating the window."""
    if not IS_WINDOWS:
        return False
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    after = HWND_TOPMOST if on_top else HWND_NOTOPMOST
    return bool(
        _user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(after), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )
    )


_app_mutex = None


def hold_app_mutex(name: str = "DeskGuard.SingleInstance.Mutex") -> None:
    """Create a named mutex for the process lifetime. The installer uses it to
    detect a running DeskGuard and ask the user to close it before updating."""
    global _app_mutex
    if IS_WINDOWS and _app_mutex is None:
        _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.CreateMutexW.restype = wintypes.HANDLE
        _app_mutex = _kernel32.CreateMutexW(None, False, name)


# --------------------------------------------------------------------------- #
# Start with Windows (per-user Run key, no admin rights needed)
# --------------------------------------------------------------------------- #
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "DeskGuard"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller build
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{interpreter}" "{main_py}"'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.exception("Could not change the Start with Windows setting")
        return False


# --------------------------------------------------------------------------- #
# Camera names via DirectShow (same order OpenCV's CAP_DSHOW uses for indices)
# --------------------------------------------------------------------------- #
def list_video_devices() -> list[str] | None:
    """Return DirectShow video-capture device names, or None if unavailable.

    Enumerating does NOT open any camera, so it never turns a camera light on
    or disturbs Teams/Zoom.
    """
    if not IS_WINDOWS:
        return None
    try:
        return _dshow_device_names()
    except Exception:  # pragma: no cover - depends on Windows COM state
        log.exception("Could not enumerate cameras")
        return None


if IS_WINDOWS:

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _VARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", wintypes.WORD),
            ("r1", wintypes.WORD),
            ("r2", wintypes.WORD),
            ("r3", wintypes.WORD),
            ("val", ctypes.c_void_p),
            ("pad", ctypes.c_void_p),
        ]

    def _guid(text: str) -> _GUID:
        g = _GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    def _method(obj: ctypes.c_void_p, index: int, *argtypes):  # type: ignore[no-untyped-def]
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        fn = proto(vtable[index])
        return lambda *args: fn(obj, *args)

    def _release(obj: ctypes.c_void_p) -> None:
        if obj:
            vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(obj)

    def _dshow_device_names() -> list[str]:
        ole32 = ctypes.windll.ole32
        oleaut32 = ctypes.windll.oleaut32
        ole32.CoInitializeEx.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        must_uninit = hr in (0, 1)  # S_OK / S_FALSE; RPC_E_CHANGED_MODE is fine too
        names: list[str] = []
        dev_enum = ctypes.c_void_p()
        enum_moniker = ctypes.c_void_p()
        try:
            clsid_sde = _guid("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
            iid_icde = _guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
            cat_video = _guid("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
            iid_bag = _guid("{55272A00-42CB-11CE-8135-00AA004BB851}")
            CLSCTX_INPROC_SERVER = 1
            ctypes.oledll.ole32.CoCreateInstance(
                ctypes.byref(clsid_sde), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid_icde), ctypes.byref(dev_enum)
            )
            # ICreateDevEnum::CreateClassEnumerator (vtable 3); S_FALSE => no devices
            create = _method(dev_enum, 3, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD)
            hr = create(ctypes.byref(cat_video), ctypes.byref(enum_moniker), 0)
            if hr != 0 or not enum_moniker:
                return names
            next_ = _method(enum_moniker, 3, wintypes.ULONG, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
            while True:
                moniker = ctypes.c_void_p()
                fetched = wintypes.ULONG()
                if next_(1, ctypes.byref(moniker), ctypes.byref(fetched)) != 0 or not moniker:
                    break
                bag = ctypes.c_void_p()
                try:
                    # IMoniker::BindToStorage (vtable 9)
                    bind = _method(moniker, 9, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
                    bind(None, None, ctypes.byref(iid_bag), ctypes.byref(bag))
                    var = _VARIANT()
                    # IPropertyBag::Read (vtable 3)
                    read = _method(bag, 3, ctypes.c_wchar_p, ctypes.POINTER(_VARIANT), ctypes.c_void_p)
                    try:
                        read("FriendlyName", ctypes.byref(var), None)
                        names.append(ctypes.wstring_at(var.val) if var.vt == 8 and var.val else f"Camera {len(names)}")
                    except OSError:
                        names.append(f"Camera {len(names)}")
                    finally:
                        oleaut32.VariantClear(ctypes.byref(var))
                except OSError:
                    names.append(f"Camera {len(names)}")
                finally:
                    _release(bag)
                    _release(moniker)
        finally:
            _release(enum_moniker)
            _release(dev_enum)
            if must_uninit:
                ole32.CoUninitialize()
        return names


# --------------------------------------------------------------------------- #
# Resource usage (for --diag logging and tools/measure.py)
# --------------------------------------------------------------------------- #
def memory_usage_mb() -> tuple[float, float]:
    """(working set, private bytes) in MB. Private = what Task Manager's
    'Memory' column approximates. Falls back to RSS on non-Windows."""
    if IS_WINDOWS:

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if _kernel32.K32GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 2**20, pmc.PrivateUsage / 2**20
        return 0.0, 0.0
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024
                    return rss, rss
    except OSError:
        pass
    return 0.0, 0.0


class CpuSampler:
    """Average CPU use of this process between calls, as % of the whole machine
    (same scale as Task Manager's CPU column)."""

    def __init__(self) -> None:
        self._cpu = time.process_time()
        self._wall = time.monotonic()
        self._ncpu = os.cpu_count() or 1

    def sample(self) -> float:
        cpu, wall = time.process_time(), time.monotonic()
        d_cpu, d_wall = cpu - self._cpu, wall - self._wall
        self._cpu, self._wall = cpu, wall
        if d_wall <= 0:
            return 0.0
        return 100.0 * d_cpu / d_wall / self._ncpu
