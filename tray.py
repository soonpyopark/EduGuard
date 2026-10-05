"""시스템 트레이(알림 영역) 아이콘 - 외부 라이브러리 없이 ctypes(Win32 API)로 구현.

- 별도 스레드에서 숨겨진 창 + 메시지 루프를 돌린다. (Tkinter 와 스레드를 분리)
- 아이콘 좌클릭 → "show", 우클릭 메뉴의 '열기' → "show", '종료' → "exit" 명령을 on_command 로 전달한다.
  on_command 는 트레이 스레드에서 호출되므로, 받는 쪽에서 queue 등으로 UI 스레드에 넘겨야 한다.
- 탐색기가 재시작되어 트레이가 사라져도 자동으로 다시 등록한다.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable, Optional

if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    WM_NULL = 0x0000
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_USER = 0x0400
    WM_TRAYICON = WM_USER + 20

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
    NIIF_INFO = 0x1

    MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
    TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
    SM_CXSMICON = 49
    IDI_APPLICATION = 32512

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

_ID_OPEN, _ID_EXIT = 1001, 1002
_CLASS_NAME = "EduGuardTrayWindow"


class TrayIcon:
    def __init__(self, icon_path: str, tooltip: str,
                 status_text: Callable[[], str], on_command: Callable[[str], None]):
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.status_text = status_text
        self.on_command = on_command
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._ok = False
        self._hwnd = None
        self._hicon = None
        self._taskbar_created = 0
        self._wndproc = None  # GC 방지용 참조

    @property
    def available(self) -> bool:
        return self._ok

    # ------------------------------------------------------------ 수명 주기
    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._thread and self._thread.is_alive():
            return self._ok
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self._ok

    def stop(self) -> None:
        if self._ok and self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._ok = False

    # ------------------------------------------------------------ 외부에서 호출
    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._ok:
            nid = self._nid(NIF_TIP)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def notify(self, title: str, message: str) -> None:
        """풍선(토스트) 알림 표시."""
        if not self._ok:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfo = message[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ------------------------------------------------------------ 내부 구현
    def _nid(self, flags: int) -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        return nid

    def _load_icon(self):
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if not hicon:
            hicon = user32.LoadIconW(None, IDI_APPLICATION)
        return hicon

    def _add_icon(self) -> None:
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _remove_icon(self) -> None:
        nid = self._nid(0)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def _emit(self, cmd: str) -> None:
        try:
            self.on_command(cmd)
        except Exception:
            pass

    def _show_menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        try:
            try:
                status = self.status_text()
            except Exception:
                status = ""
            user32.AppendMenuW(menu, MF_STRING, _ID_OPEN, "EduGuard 열기")
            if status:
                user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, _ID_EXIT, "종료 (비밀번호)")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)  # 없으면 메뉴 밖을 눌러도 메뉴가 안 닫힘
            cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                        pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if cmd == _ID_OPEN:
            self._emit("show")
        elif cmd == _ID_EXIT:
            self._emit("exit")

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self._emit("show")
                elif event == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._add_icon()  # 탐색기 재시작 후 트레이 복구
                return 0
            if msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        try:
            self._wndproc = WNDPROC(self._on_message)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))  # 이미 등록돼 있어도(재시작) 무시
            self._hwnd = user32.CreateWindowExW(0, _CLASS_NAME, "EduGuardTray", 0, 0, 0, 0, 0,
                                                None, None, hinst, None)
            if not self._hwnd:
                raise OSError("tray window creation failed")
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self._hicon = self._load_icon()
            self._add_icon()
            self._ok = True
        except Exception:
            self._ok = False
        finally:
            self._ready.set()
        if not self._ok:
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._ok = False
        self._hwnd = None
        user32.UnregisterClassW(_CLASS_NAME, hinst)
