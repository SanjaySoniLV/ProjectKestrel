"""OS-shutdown / logoff / reboot detection.

Distinguishes "the OS told the process to exit" (reboot, logoff, power off)
from "the application crashed" so the next launch can suppress the false
crash dialog. See ``visualizer._mark_session_exit_reason`` for the consumer.

Single public entry point: :func:`install`. Best-effort and failsafe — any
import or setup error is swallowed; shutdown reporting is a quality-of-life
feature, not a correctness requirement.

Per-platform strategy:

* **Linux**: ``SIGTERM`` and ``SIGHUP`` handlers. systemd / GNOME / most
  desktop session managers send SIGTERM with a short grace period before
  SIGKILL; SIGHUP covers TTY logout. The handler marks exit reason and
  returns; the OS will follow up with SIGKILL.
* **macOS**: ``NSWorkspaceWillPowerOffNotification`` observer on the
  shared workspace notification center. Skipped if PyObjC is unavailable.
* **Windows**: hidden ``ctypes`` window pumped on a daemon thread, listening
  for ``WM_QUERYENDSESSION`` / ``WM_ENDSESSION``. ``SetConsoleCtrlHandler``
  is also registered as a belt-and-braces fallback (no-op for ``--windowed``
  PyInstaller builds where there's no console).
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional

_installed = False
_lock = threading.Lock()
# Module-level retain for macOS observer / Windows window class so they
# aren't garbage-collected.
_keepalive: list = []


def install(callback: Callable[[], None]) -> bool:
    """Register OS-shutdown listeners that invoke ``callback`` once.

    The callback receives no arguments and its return value is ignored.
    It MUST be fast (single small file write) — on Linux it runs in a
    signal handler context, and on Windows it runs in the message-pump
    thread during the system's shutdown grace window.

    Returns True if at least one listener was installed.
    """
    global _installed
    with _lock:
        if _installed:
            return True

        wrapped = _wrap_once(callback)
        installed_any = False

        try:
            if sys.platform.startswith('win'):
                installed_any |= _install_windows(wrapped)
            elif sys.platform == 'darwin':
                installed_any |= _install_macos(wrapped)
            else:
                installed_any |= _install_posix(wrapped)
        except Exception:
            pass

        if os.environ.get('KESTREL_FAKE_OS_SHUTDOWN') == '1':
            try:
                threading.Timer(0.5, wrapped).start()
                installed_any = True
            except Exception:
                pass

        _installed = installed_any
        return installed_any


def _wrap_once(callback: Callable[[], None]) -> Callable[[], None]:
    """Return a wrapper that runs ``callback`` at most once, swallowing errors."""
    fired = threading.Event()

    def _once(*_a, **_kw):
        if fired.is_set():
            return
        fired.set()
        try:
            callback()
        except Exception:
            pass

    return _once


def _install_posix(callback: Callable[[], None]) -> bool:
    import signal

    def _handler(signum, _frame):
        callback()
        # Don't raise — let pywebview's runloop continue. The OS will
        # follow up with SIGKILL during the shutdown grace window. If we
        # raised SystemExit here the finally-block would overwrite the
        # exit_reason with 'clean', which is harmless but loses metadata.

    installed = False
    for sig_name in ('SIGTERM', 'SIGHUP'):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
            installed = True
        except (ValueError, OSError):
            # ValueError: not on main thread. OSError: signal not allowed.
            pass
    return installed


def _install_macos(callback: Callable[[], None]) -> bool:
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False

    class _ShutdownObserver(NSObject):  # type: ignore[misc]
        def powerOff_(self, _notification):
            callback()

    observer = _ShutdownObserver.alloc().init()
    try:
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer,
            'powerOff:',
            'NSWorkspaceWillPowerOffNotification',
            None,
        )
    except Exception:
        return False
    _keepalive.append(observer)
    return True


def _install_windows(callback: Callable[[], None]) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016
    WM_DESTROY = 0x0002

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    # WNDPROC uses pointer-sized types (c_size_t / c_ssize_t) for WPARAM,
    # LPARAM, and LRESULT so the callback works correctly on 64-bit Windows.
    # wintypes.WPARAM / wintypes.LPARAM are defined as 32-bit c_uint / c_long
    # in Python's ctypes, which causes an OverflowError when the OS delivers
    # a message whose LPARAM holds a 64-bit pointer value. The same pointer-
    # sized types must also be declared on DefWindowProcW — without an
    # explicit argtypes ctypes falls back to c_int for integer arguments,
    # so forwarding a 64-bit lparam still overflows on the outbound call.
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,   # LRESULT = LONG_PTR
        wintypes.HWND, wintypes.UINT,
        ctypes.c_size_t,    # WPARAM = UINT_PTR
        ctypes.c_ssize_t,   # LPARAM = LONG_PTR
    )
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT,
        ctypes.c_size_t,    # WPARAM = UINT_PTR
        ctypes.c_ssize_t,   # LPARAM = LONG_PTR
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg in (WM_QUERYENDSESSION, WM_ENDSESSION):
            callback()
            if msg == WM_QUERYENDSESSION:
                return 1  # TRUE — we accept shutdown
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wnd_proc)
    _keepalive.append(proc)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ('style', wintypes.UINT),
            ('lpfnWndProc', WNDPROC),
            ('cbClsExtra', ctypes.c_int),
            ('cbWndExtra', ctypes.c_int),
            ('hInstance', wintypes.HINSTANCE),
            ('hIcon', wintypes.HICON),
            ('hCursor', wintypes.HANDLE),
            ('hbrBackground', wintypes.HBRUSH),
            ('lpszMenuName', wintypes.LPCWSTR),
            ('lpszClassName', wintypes.LPCWSTR),
        ]

    h_instance = kernel32.GetModuleHandleW(None)
    class_name = 'KestrelShutdownWatch'

    wc = WNDCLASS()
    wc.lpfnWndProc = proc
    wc.hInstance = h_instance
    wc.lpszClassName = class_name
    _keepalive.append(wc)

    user32.RegisterClassW.restype = wintypes.ATOM
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        # Class name may already be registered if install() were retried; ignore.
        pass

    user32.CreateWindowExW.restype = wintypes.HWND
    hwnd = user32.CreateWindowExW(
        0, class_name, 'KestrelShutdownWatch',
        0, 0, 0, 0, 0,
        None, None, h_instance, None,
    )
    if not hwnd:
        return False

    def _pump():
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    t = threading.Thread(target=_pump, name='KestrelShutdownWatch', daemon=True)
    t.start()
    _keepalive.append(t)

    # Belt-and-braces: SetConsoleCtrlHandler. No-op in --windowed
    # PyInstaller builds (no console), but free in console builds.
    try:
        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_SHUTDOWN_EVENT = 6
        CTRL_LOGOFF_EVENT = 5
        CTRL_CLOSE_EVENT = 2

        def _ctrl(event):
            if event in (CTRL_SHUTDOWN_EVENT, CTRL_LOGOFF_EVENT, CTRL_CLOSE_EVENT):
                callback()
                return True
            return False

        ctrl = HANDLER_ROUTINE(_ctrl)
        _keepalive.append(ctrl)
        kernel32.SetConsoleCtrlHandler(ctrl, True)
    except Exception:
        pass

    return True
