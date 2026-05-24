import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk


def get_app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir():
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir)
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
RESOURCE_DIR = get_resource_dir()
CONFIG_PATH = APP_DIR / "config.json"
ICON_PATH = RESOURCE_DIR / "icon.ico"
APP_NAME = "Command Tray"
LOG_LIMIT = 1000
STOP_TIMEOUT_SECONDS = 3
RETRY_INITIAL_DELAY_SECONDS = 3
RETRY_MAX_DELAY_SECONDS = 60
RETRY_RESET_AFTER_SECONDS = 300
SINGLE_INSTANCE_MUTEX = "Local\\cangjun.CommandTray.SingleInstance"
SINGLE_INSTANCE_WINDOW_CLASS = "cangjun.CommandTray.SingleInstanceWindow"
SHOW_MAIN_WINDOW_MESSAGE = 0x8000 + 2
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = APP_NAME
START_HIDDEN_ARG = "--start-hidden"


def set_tk_window_icon(window):
    if os.name != "nt" or not ICON_PATH.exists():
        return
    try:
        window.iconbitmap(default=str(ICON_PATH))
    except tk.TclError:
        pass


@dataclass
class AutoRetryConfig:
    enabled: bool = False
    max_attempts: int = 0
    initial_delay_seconds: int = RETRY_INITIAL_DELAY_SECONDS
    max_delay_seconds: int = RETRY_MAX_DELAY_SECONDS
    reset_after_seconds: int = RETRY_RESET_AFTER_SECONDS


@dataclass
class TunnelConfig:
    id: str
    name: str
    command: str
    enabled_on_start: bool = False
    auto_retry: AutoRetryConfig = field(default_factory=AutoRetryConfig)


def read_nonnegative_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def parse_auto_retry_config(value):
    if isinstance(value, bool):
        return AutoRetryConfig(enabled=value)
    if not isinstance(value, dict):
        return AutoRetryConfig()
    return AutoRetryConfig(
        enabled=bool(value.get("enabled", False)),
        max_attempts=read_nonnegative_int(value.get("max_attempts"), 0),
        initial_delay_seconds=read_nonnegative_int(
            value.get("initial_delay_seconds"),
            RETRY_INITIAL_DELAY_SECONDS,
        )
        or RETRY_INITIAL_DELAY_SECONDS,
        max_delay_seconds=read_nonnegative_int(
            value.get("max_delay_seconds"),
            RETRY_MAX_DELAY_SECONDS,
        )
        or RETRY_MAX_DELAY_SECONDS,
        reset_after_seconds=read_nonnegative_int(
            value.get("reset_after_seconds"),
            RETRY_RESET_AFTER_SECONDS,
        )
        or RETRY_RESET_AFTER_SECONDS,
    )


class TunnelRuntime:
    def __init__(self):
        self.process = None
        self.status = "stopped"
        self.logs = deque(maxlen=LOG_LIMIT)
        self.started_at = None
        self.returncode = None
        self.stop_requested = False
        self.retry_attempts = 0
        self.retry_after_id = None
        self.retry_due_at = None
        self.retry_generation = 0


class TunnelDialog:
    def __init__(self, parent, title, initial=None, command_readonly=False):
        self.result = None
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        set_tk_window_icon(self.window)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(True, False)

        self.name_var = tk.StringVar(value=(initial.name if initial else ""))
        self.autostart_var = tk.BooleanVar(value=(initial.enabled_on_start if initial else False))
        retry_config = initial.auto_retry if initial else AutoRetryConfig()
        self.retry_enabled_var = tk.BooleanVar(value=retry_config.enabled)
        self.retry_max_attempts_var = tk.StringVar(value=str(retry_config.max_attempts))

        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.window.columnconfigure(0, weight=1)

        ttk.Label(frame, text="名称").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        name_entry = ttk.Entry(frame, textvariable=self.name_var, width=42)
        name_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="命令").grid(row=1, column=0, sticky="nw", padx=(0, 10), pady=(0, 8))
        self.command_text = tk.Text(frame, width=68, height=4, wrap="word", undo=True)
        self.command_text.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        if initial:
            self.command_text.insert("1.0", initial.command)
        if command_readonly:
            self.command_text.configure(state="disabled", background="#f3f3f3")

        checkbox = ttk.Checkbutton(frame, text="启动程序时自动开启", variable=self.autostart_var)
        checkbox.grid(row=2, column=1, sticky="w", pady=(0, 12))

        retry_checkbox = ttk.Checkbutton(frame, text="异常退出后自动重试", variable=self.retry_enabled_var)
        retry_checkbox.grid(row=3, column=1, sticky="w", pady=(0, 8))

        retry_frame = ttk.Frame(frame)
        retry_frame.grid(row=4, column=1, sticky="w", pady=(0, 12))
        ttk.Label(retry_frame, text="最多重试次数").grid(row=0, column=0, sticky="w", padx=(0, 8))
        retry_entry = ttk.Entry(retry_frame, textvariable=self.retry_max_attempts_var, width=8)
        retry_entry.grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(retry_frame, text="0 表示无限，间隔按 3/6/12 秒递增，最多 60 秒").grid(
            row=0,
            column=2,
            sticky="w",
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self.window.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="保存", command=self.save).grid(row=0, column=1)

        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Control-Return>", lambda _event: self.save())

        name_entry.focus_set()
        self.center(parent)
        self.window.wait_window()

    def center(self, parent):
        self.window.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = max(parent.winfo_width(), 1)
        ph = max(parent.winfo_height(), 1)
        ww = self.window.winfo_width()
        wh = self.window.winfo_height()
        x = px + (pw - ww) // 2
        y = py + (ph - wh) // 3
        self.window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def save(self):
        name = self.name_var.get().strip()
        command = self.command_text.get("1.0", "end").strip()
        command = " ".join(line.strip() for line in command.splitlines() if line.strip())

        if not name:
            messagebox.showwarning("缺少名称", "请填写一个名称。", parent=self.window)
            return
        if not command:
            messagebox.showwarning("缺少命令", "请填写 SSH 命令。", parent=self.window)
            return
        try:
            max_attempts = int(self.retry_max_attempts_var.get().strip() or "0")
        except ValueError:
            messagebox.showwarning("重试次数无效", "最多重试次数必须是非负整数，0 表示无限重试。", parent=self.window)
            return
        if max_attempts < 0:
            messagebox.showwarning("重试次数无效", "最多重试次数不能小于 0。", parent=self.window)
            return

        self.result = {
            "name": name,
            "command": command,
            "enabled_on_start": self.autostart_var.get(),
            "auto_retry": AutoRetryConfig(
                enabled=self.retry_enabled_var.get(),
                max_attempts=max_attempts,
            ),
        }
        self.window.destroy()


class LogWindow:
    def __init__(self, app, tunnel_id, title):
        self.app = app
        self.tunnel_id = tunnel_id
        self.window = tk.Toplevel(app.root)
        self.window.title(f"日志 - {title}")
        set_tk_window_icon(self.window)
        self.window.geometry("860x460")
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.text = tk.Text(frame, wrap="word", state="disabled", height=18)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="清空日志", command=self.clear).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="关闭", command=self.close).grid(row=0, column=1)

        runtime = app.runtime_for(tunnel_id)
        for line in runtime.logs:
            self.append(line)

    def append(self, line):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        runtime = self.app.runtime_for(self.tunnel_id)
        runtime.logs.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def close(self):
        self.app.log_windows.pop(self.tunnel_id, None)
        self.window.destroy()


class WindowsTrayIcon:
    WM_USER = 0x0400
    WM_APP = 0x8000
    WM_TRAY = WM_APP + 1
    WM_CLOSE = 0x0010
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_NULL = 0x0000
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B

    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIM_SETVERSION = 0x00000004
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NOTIFYICON_VERSION_4 = 4
    NIIF_INFO = 0x00000001
    NIIF_WARNING = 0x00000002

    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    MF_STRING = 0x00000000
    TPM_RIGHTBUTTON = 0x00000002

    CMD_SHOW = 1001
    CMD_EXIT = 1002

    def __init__(self, app_name, events):
        self.app_name = app_name
        self.events = events
        self.thread = None
        self.ready = threading.Event()
        self.hwnd = None
        self._class_name = f"{app_name}_{uuid.uuid4().hex}"
        self._wndproc = None
        self._icon_added = False
        self.ctypes = None
        self.wintypes = None
        self.user32 = None
        self.shell32 = None
        self.NOTIFYICONDATAW = None
        self.last_error = ""
        self._backend = None
        self._hicon = None

    def start(self):
        if os.name != "nt":
            return False
        try:
            import win32api  # noqa: F401
            import win32con  # noqa: F401
            import win32gui  # noqa: F401

            self._backend = "pywin32"
            self.thread = threading.Thread(target=self._run_pywin32, name="tray-icon", daemon=True)
        except Exception:
            self._backend = "ctypes"
            self.thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=2)
        return self.hwnd is not None and self._icon_added

    def stop(self):
        if self._backend == "pywin32" and self.hwnd:
            try:
                import win32con
                import win32gui

                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        elif self.user32 is not None and self.hwnd:
            self.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1)

    def notify(self, title, message, warning=False):
        if self._backend == "pywin32":
            return self._notify_pywin32(title, message, warning)
        if not self.hwnd or not self.shell32 or not self.NOTIFYICONDATAW:
            return False
        nid = self._make_nid(self.NIF_INFO)
        nid.szInfo = str(message)[:255]
        nid.szInfoTitle = str(title)[:63]
        nid.dwInfoFlags = self.NIIF_WARNING if warning else self.NIIF_INFO
        return bool(self.shell32.Shell_NotifyIconW(self.NIM_MODIFY, self.ctypes.byref(nid)))

    def update_tooltip(self, text):
        if self._backend == "pywin32":
            return self._update_tooltip_pywin32(text)
        if not self.hwnd or not self.shell32 or not self.NOTIFYICONDATAW:
            return False
        nid = self._make_nid(self.NIF_TIP)
        nid.szTip = str(text)[:127]
        return bool(self.shell32.Shell_NotifyIconW(self.NIM_MODIFY, self.ctypes.byref(nid)))

    def _run_pywin32(self):
        try:
            import win32api
            import win32con
            import win32gui

            hinstance = win32api.GetModuleHandle(None)
            class_name = self._class_name

            message_map = {
                self.WM_TRAY: self._window_proc_pywin32,
                win32con.WM_COMMAND: self._window_proc_pywin32,
                win32con.WM_CLOSE: self._window_proc_pywin32,
                win32con.WM_DESTROY: self._window_proc_pywin32,
            }
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            wc.lpfnWndProc = message_map
            wc.hIcon = self._load_icon_pywin32()
            atom = win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindow(
                atom,
                self.app_name,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                hinstance,
                None,
            )
            self.hwnd = hwnd
            self._add_icon_pywin32()
            self.ready.set()
            win32gui.PumpMessages()
            self.hwnd = None
        except Exception as exc:
            self.last_error = repr(exc)
            self.ready.set()

    def _add_icon_pywin32(self):
        import win32con
        import win32gui

        hicon = self._load_icon_pywin32()
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        data = (self.hwnd, 0, flags, self.WM_TRAY, hicon, self.app_name[:127])
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, data)
        self._icon_added = True

    def _load_icon_pywin32(self):
        import win32con
        import win32gui

        if self._hicon:
            return self._hicon
        if ICON_PATH.exists():
            try:
                self._hicon = win32gui.LoadImage(
                    0,
                    str(ICON_PATH),
                    self.IMAGE_ICON,
                    0,
                    0,
                    self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
                )
                return self._hicon
            except Exception:
                pass
        self._hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        return self._hicon

    def _delete_icon_pywin32(self):
        if not self._icon_added or not self.hwnd:
            return
        try:
            import win32gui

            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
            self._icon_added = False
        except Exception:
            pass

    def _notify_pywin32(self, title, message, warning=False):
        if not self.hwnd or not self._icon_added:
            return False
        try:
            import win32con
            import win32gui

            hicon = self._load_icon_pywin32()
            flags = win32gui.NIF_INFO | win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            info_flags = win32gui.NIIF_WARNING if warning else win32gui.NIIF_INFO
            data = (
                self.hwnd,
                0,
                flags,
                self.WM_TRAY,
                hicon,
                self.app_name[:127],
                str(message)[:255],
                10,
                str(title)[:63],
                info_flags,
            )
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, data)
            return True
        except Exception:
            return False

    def _update_tooltip_pywin32(self, text):
        if not self.hwnd or not self._icon_added:
            return False
        try:
            import win32con
            import win32gui

            hicon = self._load_icon_pywin32()
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            data = (self.hwnd, 0, flags, self.WM_TRAY, hicon, str(text)[:127])
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, data)
            return True
        except Exception:
            return False

    def _window_proc_pywin32(self, hwnd, msg, wparam, lparam):
        import win32con
        import win32gui

        if msg == self.WM_TRAY:
            if lparam == win32con.WM_LBUTTONDBLCLK:
                self.events.put(("tray_show",))
            elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                self._show_menu_pywin32(hwnd)
            return 0

        if msg == win32con.WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            if command_id == self.CMD_SHOW:
                self.events.put(("tray_show",))
                return 0
            if command_id == self.CMD_EXIT:
                self.events.put(("tray_exit",))
                return 0

        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0

        if msg == win32con.WM_DESTROY:
            self._delete_icon_pywin32()
            win32gui.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _show_menu_pywin32(self, hwnd):
        import win32api
        import win32con
        import win32gui

        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.CMD_SHOW, "显示窗口")
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.CMD_EXIT, "退出")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
            win32api.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _run(self):
        try:
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.wintypes = wintypes
            self.user32 = ctypes.windll.user32
            self.shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

            LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
            HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)
            HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
            HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)
            HBRUSH = getattr(wintypes, "HBRUSH", wintypes.HANDLE)
            HMENU = getattr(wintypes, "HMENU", wintypes.HANDLE)
            LPVOID = getattr(wintypes, "LPVOID", ctypes.c_void_p)
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", HICON),
                    ("szTip", ctypes.c_wchar * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("uTimeoutOrVersion", wintypes.UINT),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", GUID),
                    ("hBalloonIcon", HICON),
                ]

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", HINSTANCE),
                    ("hIcon", HICON),
                    ("hCursor", HCURSOR),
                    ("hbrBackground", HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", HICON),
                ]

            self.NOTIFYICONDATAW = NOTIFYICONDATAW
            self._wndproc = WNDPROC(self._window_proc)
            self._configure_winapi_functions(LRESULT, HICON, HINSTANCE, HMENU, LPVOID)

            hinstance = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinstance
            wc.lpszClassName = self._class_name
            wc.hIcon = self._load_icon()
            wc.hIconSm = wc.hIcon

            if not self.user32.RegisterClassExW(ctypes.byref(wc)):
                self.last_error = f"RegisterClassExW failed: {kernel32.GetLastError()}"
                self.ready.set()
                return

            hwnd = self.user32.CreateWindowExW(
                0,
                self._class_name,
                self.app_name,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                self.last_error = f"CreateWindowExW failed: {kernel32.GetLastError()}"
                self.ready.set()
                return

            self.hwnd = hwnd
            self._add_icon()
            if not self._icon_added:
                self.last_error = f"Shell_NotifyIconW(NIM_ADD) failed: {kernel32.GetLastError()}"
            self.ready.set()

            msg = wintypes.MSG()
            while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))

            self._delete_icon()
            self.hwnd = None
        except Exception as exc:
            self.last_error = repr(exc)
            self.ready.set()

    def _make_nid(self, flags):
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = self.ctypes.sizeof(self.NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _configure_winapi_functions(self, lresult_type, hicon_type, hinstance_type, hmenu_type, lpvoid_type):
        self.user32.CreateWindowExW.restype = self.wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [
            self.wintypes.DWORD,
            self.wintypes.LPCWSTR,
            self.wintypes.LPCWSTR,
            self.wintypes.DWORD,
            self.ctypes.c_int,
            self.ctypes.c_int,
            self.ctypes.c_int,
            self.ctypes.c_int,
            self.wintypes.HWND,
            hmenu_type,
            hinstance_type,
            lpvoid_type,
        ]
        self.user32.DefWindowProcW.restype = lresult_type
        self.user32.DefWindowProcW.argtypes = [
            self.wintypes.HWND,
            self.wintypes.UINT,
            self.wintypes.WPARAM,
            self.wintypes.LPARAM,
        ]
        self.user32.LoadIconW.restype = hicon_type
        self.user32.LoadIconW.argtypes = [self.wintypes.HINSTANCE, self.ctypes.c_void_p]
        self.user32.LoadImageW.restype = hicon_type
        self.user32.LoadImageW.argtypes = [
            hinstance_type,
            self.wintypes.LPCWSTR,
            self.wintypes.UINT,
            self.ctypes.c_int,
            self.ctypes.c_int,
            self.wintypes.UINT,
        ]
        self.user32.PostMessageW.argtypes = [
            self.wintypes.HWND,
            self.wintypes.UINT,
            self.wintypes.WPARAM,
            self.wintypes.LPARAM,
        ]
        self.shell32.Shell_NotifyIconW.argtypes = [self.wintypes.DWORD, self.ctypes.c_void_p]

    def _add_icon(self):
        nid = self._make_nid(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self.app_name[:127]
        self._icon_added = bool(self.shell32.Shell_NotifyIconW(self.NIM_ADD, self.ctypes.byref(nid)))
        if self._icon_added:
            version_nid = self._make_nid(0)
            version_nid.uTimeoutOrVersion = self.NOTIFYICON_VERSION_4
            self.shell32.Shell_NotifyIconW(self.NIM_SETVERSION, self.ctypes.byref(version_nid))

    def _load_icon(self):
        if self._hicon:
            return self._hicon
        if ICON_PATH.exists():
            hicon = self.user32.LoadImageW(
                None,
                str(ICON_PATH),
                self.IMAGE_ICON,
                0,
                0,
                self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
            )
            if hicon:
                self._hicon = hicon
                return self._hicon
        self._hicon = self.user32.LoadIconW(None, self.ctypes.c_void_p(self.IDI_APPLICATION))
        return self._hicon

    def _delete_icon(self):
        if self._icon_added and self.shell32 is not None and self.hwnd:
            nid = self._make_nid(0)
            self.shell32.Shell_NotifyIconW(self.NIM_DELETE, self.ctypes.byref(nid))
            self._icon_added = False

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_TRAY:
            tray_msg = int(lparam)
            if tray_msg == self.WM_LBUTTONDBLCLK:
                self.events.put(("tray_show",))
            elif tray_msg in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                self._show_menu(hwnd)
            return 0

        if msg == self.WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            if command_id == self.CMD_SHOW:
                self.events.put(("tray_show",))
                return 0
            if command_id == self.CMD_EXIT:
                self.events.put(("tray_exit",))
                return 0

        if msg == self.WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0

        if msg == self.WM_DESTROY:
            self._delete_icon()
            self.user32.PostQuitMessage(0)
            return 0

        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd):
        menu = self.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self.user32.AppendMenuW(menu, self.MF_STRING, self.CMD_SHOW, "显示窗口")
            self.user32.AppendMenuW(menu, self.MF_STRING, self.CMD_EXIT, "退出")
            point = self.wintypes.POINT()
            self.user32.GetCursorPos(self.ctypes.byref(point))
            self.user32.SetForegroundWindow(hwnd)
            self.user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
            self.user32.PostMessageW(hwnd, self.WM_NULL, 0, 0)
        finally:
            self.user32.DestroyMenu(menu)


class SingleInstanceGuard:
    ERROR_ALREADY_EXISTS = 183
    HWND_MESSAGE = -3

    def __init__(self):
        self.mutex = None
        self.hwnd = None
        self._wndproc = None
        self.user32 = None
        self.kernel32 = None
        self.ctypes = None
        self.wintypes = None
        self._callback = None
        self.owns_mutex = False

    def acquire(self):
        if os.name != "nt":
            return True
        try:
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.wintypes = wintypes
            self.user32 = ctypes.windll.user32
            self.kernel32 = ctypes.windll.kernel32
            self.kernel32.CreateMutexW.restype = wintypes.HANDLE
            self.kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel32.GetLastError.restype = wintypes.DWORD
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]

            self.mutex = self.kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
            if not self.mutex:
                return True
            if self.kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
                self.notify_existing_instance()
                self.kernel32.CloseHandle(self.mutex)
                self.mutex = None
                return False
            self.owns_mutex = True
            return True
        except Exception:
            return True

    def notify_existing_instance(self):
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.FindWindowExW.restype = wintypes.HWND
            user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            message_window_parent = wintypes.HWND(self.HWND_MESSAGE)
            deadline = time.time() + 2
            while time.time() < deadline:
                hwnd = user32.FindWindowExW(message_window_parent, None, SINGLE_INSTANCE_WINDOW_CLASS, None)
                if hwnd:
                    user32.PostMessageW(hwnd, SHOW_MAIN_WINDOW_MESSAGE, 0, 0)
                    return
                time.sleep(0.05)
        except Exception:
            pass

    def start_listener(self, callback):
        if os.name != "nt" or self.user32 is None or self.kernel32 is None:
            return
        self._callback = callback
        try:
            import ctypes
            from ctypes import wintypes

            LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
            HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)
            HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
            HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)
            HBRUSH = getattr(wintypes, "HBRUSH", wintypes.HANDLE)
            HMENU = getattr(wintypes, "HMENU", wintypes.HANDLE)
            LPVOID = getattr(wintypes, "LPVOID", ctypes.c_void_p)
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", HINSTANCE),
                    ("hIcon", HICON),
                    ("hCursor", HCURSOR),
                    ("hbrBackground", HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", HICON),
                ]

            self._wndproc = WNDPROC(self._window_proc)
            self.user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
            self.user32.RegisterClassExW.restype = wintypes.ATOM
            self.user32.CreateWindowExW.restype = wintypes.HWND
            self.user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                HMENU,
                HINSTANCE,
                LPVOID,
            ]
            self.user32.DefWindowProcW.restype = LRESULT
            self.user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

            hinstance = self.kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinstance
            wc.lpszClassName = SINGLE_INSTANCE_WINDOW_CLASS
            self.user32.RegisterClassExW(ctypes.byref(wc))
            self.hwnd = self.user32.CreateWindowExW(
                0,
                SINGLE_INSTANCE_WINDOW_CLASS,
                APP_NAME,
                0,
                0,
                0,
                0,
                0,
                self.HWND_MESSAGE,
                None,
                hinstance,
                None,
            )
        except Exception:
            self.hwnd = None

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == SHOW_MAIN_WINDOW_MESSAGE:
            if self._callback is not None:
                self._callback()
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def close(self):
        if os.name != "nt":
            return
        try:
            if self.hwnd and self.user32 is not None:
                self.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            pass
        try:
            if self.mutex and self.kernel32 is not None:
                if self.owns_mutex:
                    self.kernel32.ReleaseMutex(self.mutex)
                    self.owns_mutex = False
                self.kernel32.CloseHandle(self.mutex)
                self.mutex = None
        except Exception:
            pass


class WindowsStartupManager:
    @staticmethod
    def is_supported():
        return os.name == "nt"

    @staticmethod
    def get_command():
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}" {START_HIDDEN_ARG}'
        executable = Path(sys.executable).resolve()
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
        script = Path(__file__).resolve()
        return f'"{executable}" "{script}" {START_HIDDEN_ARG}'

    @staticmethod
    def is_enabled():
        if not WindowsStartupManager.is_supported():
            return False
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
                value, _value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return value == WindowsStartupManager.get_command()
        except FileNotFoundError:
            return False
        except Exception:
            return False

    @staticmethod
    def set_enabled(enabled):
        if not WindowsStartupManager.is_supported():
            raise RuntimeError("开机自启动仅支持 Windows。")
        try:
            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                if enabled:
                    winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, WindowsStartupManager.get_command())
                else:
                    try:
                        winreg.DeleteValue(key, RUN_VALUE_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as exc:
            raise RuntimeError(f"更新开机自启动失败：{exc}") from exc


class SSHLHelperApp:
    def __init__(self, root, instance_guard=None):
        self.root = root
        self.instance_guard = instance_guard
        self.root.title(APP_NAME)
        set_tk_window_icon(self.root)
        self.root.minsize(920, 520)

        self.tunnels = []
        self.runtimes = {}
        self.log_windows = {}
        self.events = queue.Queue()
        self.closing = False
        self.hidden_to_tray = False
        self.exit_confirmed = False
        self.tray = None
        self.tray_error = ""
        self.startup_var = tk.BooleanVar(value=WindowsStartupManager.is_enabled())
        self.retry_refresh_after_id = None

        self.configure_fonts()
        self.configure_style()
        self.load_config()
        self.build_ui()
        self.refresh_rows()
        self.init_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_unmap)
        self.root.bind("<Map>", self.on_map)
        self.root.after(100, self.process_events)
        self.root.after(300, self.start_enabled_tunnels)
        if START_HIDDEN_ARG in sys.argv:
            self.root.after(50, self.hide_to_tray)

    def configure_fonts(self):
        families = set(tkfont.families(self.root))
        ui_family = "Microsoft YaHei UI" if "Microsoft YaHei UI" in families else "Segoe UI"
        mono_family = "Consolas" if "Consolas" in families else "Courier New"

        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=ui_family, size=10)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=10)
        except tk.TclError:
            pass

        self.title_font = tkfont.Font(family=ui_family, size=14, weight="bold")
        self.header_font = tkfont.Font(family=ui_family, size=10, weight="bold")

    def configure_style(self):
        style = ttk.Style(self.root)
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    pass
        style.configure("Header.TLabel", font=self.header_font)
        style.configure("Title.TLabel", font=self.title_font)
        style.configure("StatusRunning.TLabel", foreground="#087a2f")
        style.configure("StatusStopped.TLabel", foreground="#666666")
        style.configure("StatusBusy.TLabel", foreground="#8a5a00")
        style.configure("StatusError.TLabel", foreground="#b00020")

    def init_tray(self):
        if os.name != "nt":
            return
        self.tray = WindowsTrayIcon(APP_NAME, self.events)
        if self.tray.start():
            self.update_tray_tooltip()
        else:
            self.tray_error = self.tray.last_error or "unknown tray initialization error"
            self.tray = None

    def build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")

        toolbar = ttk.Frame(top)
        toolbar.grid(row=0, column=1, sticky="e")
        ttk.Button(toolbar, text="新增", command=self.add_tunnel).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(toolbar, text="保存配置", command=self.save_config_with_notice).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(toolbar, text="停止全部", command=self.stop_all).grid(row=0, column=2, padx=(0, 10))
        startup_check = ttk.Checkbutton(
            toolbar,
            text="开机自启动",
            variable=self.startup_var,
            command=self.toggle_startup,
        )
        startup_check.grid(row=0, column=3)
        if not WindowsStartupManager.is_supported():
            startup_check.state(["disabled"])

        body = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.rows_frame = ttk.Frame(self.canvas)
        self.rows_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.rows_frame.bind("<Configure>", self.on_rows_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)

        self.status_var = tk.StringVar()
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(12, 6))
        status_bar.grid(row=2, column=0, sticky="ew")

    def on_rows_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.rows_window, width=event.width)

    def on_mousewheel(self, event):
        if self.canvas.winfo_containing(event.x_root, event.y_root) is not None:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def on_unmap(self, _event):
        if self.closing or self.hidden_to_tray:
            return
        if self.root.state() == "iconic":
            self.hide_to_tray(show_notice=True)

    def on_map(self, _event):
        if not self.hidden_to_tray:
            return
        self.hidden_to_tray = False

    def hide_to_tray(self, show_notice=False):
        if self.tray is None:
            if show_notice:
                messagebox.showwarning(
                    "托盘不可用",
                    f"系统托盘没有初始化成功，窗口不会退出。\n\n原因：{self.tray_error or 'unknown'}",
                    parent=self.root,
                )
            try:
                self.root.deiconify()
                self.root.state("normal")
            except tk.TclError:
                pass
            return
        self.hidden_to_tray = True
        self.root.withdraw()
        if show_notice:
            self.notify_user(APP_NAME, "程序已隐藏到托盘，命令会继续在后台运行。")

    def show_from_tray(self):
        self.hidden_to_tray = False
        self.root.deiconify()
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        try:
            self.root.attributes("-topmost", True)
            self.root.after(250, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass
        self.root.lift()
        self.root.focus_force()

    def notify_user(self, title, message, warning=False):
        if self.tray is not None and self.tray.notify(title, message, warning=warning):
            return
        if not self.hidden_to_tray and self.root.winfo_exists():
            if warning:
                messagebox.showwarning(title, message, parent=self.root)
            else:
                messagebox.showinfo(title, message, parent=self.root)

    def update_tray_tooltip(self):
        if self.tray is None:
            return
        running = sum(1 for tunnel in self.tunnels if self.is_running(tunnel.id))
        self.tray.update_tooltip(f"{APP_NAME} - {running}/{len(self.tunnels)} running")

    def request_show_from_other_instance(self):
        self.events.put(("show_existing_instance",))

    def toggle_startup(self):
        desired = self.startup_var.get()
        try:
            WindowsStartupManager.set_enabled(desired)
            self.startup_var.set(WindowsStartupManager.is_enabled())
            self.set_status_text()
        except Exception as exc:
            self.startup_var.set(WindowsStartupManager.is_enabled())
            self.set_status_text()
            messagebox.showerror("开机自启动设置失败", str(exc), parent=self.root)

    def load_config(self):
        if not CONFIG_PATH.exists():
            self.tunnels = []
            return

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw_tunnels = data.get("tunnels", [])
            loaded = []
            seen_ids = set()
            for item in raw_tunnels:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                command = str(item.get("command", "")).strip()
                if not name or not command:
                    continue
                tunnel_id = str(item.get("id", "")).strip() or self.generate_id(name, seen_ids)
                if tunnel_id in seen_ids:
                    tunnel_id = self.generate_id(name, seen_ids)
                seen_ids.add(tunnel_id)
                loaded.append(
                    TunnelConfig(
                        id=tunnel_id,
                        name=name,
                        command=command,
                        enabled_on_start=bool(item.get("enabled_on_start", False)),
                        auto_retry=parse_auto_retry_config(item.get("auto_retry")),
                    )
                )
            self.tunnels = loaded
        except Exception as exc:
            self.tunnels = []
            messagebox.showerror(
                "配置读取失败",
                f"无法读取 {CONFIG_PATH.name}。\n\n{exc}\n\n程序将以空配置启动，不会自动覆盖原文件。",
            )

    def save_config(self):
        data = {"tunnels": [asdict(tunnel) for tunnel in self.tunnels]}
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def save_config_with_notice(self):
        try:
            self.save_config()
            self.set_status_text()
            messagebox.showinfo("已保存", f"配置已保存到 {CONFIG_PATH}", parent=self.root)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)

    def runtime_for(self, tunnel_id):
        if tunnel_id not in self.runtimes:
            self.runtimes[tunnel_id] = TunnelRuntime()
        return self.runtimes[tunnel_id]

    def find_tunnel(self, tunnel_id):
        for tunnel in self.tunnels:
            if tunnel.id == tunnel_id:
                return tunnel
        return None

    def generate_id(self, name, extra_existing=None):
        existing = {tunnel.id for tunnel in self.tunnels}
        if extra_existing:
            existing.update(extra_existing)
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip().lower()).strip("_") or "tunnel"
        while True:
            candidate = f"{base}_{uuid.uuid4().hex[:6]}"
            if candidate not in existing:
                return candidate

    def refresh_rows(self):
        for child in self.rows_frame.winfo_children():
            child.destroy()

        self.rows_frame.columnconfigure(0, weight=1)

        header = ttk.Frame(self.rows_frame, padding=(8, 8, 8, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="名称", width=18, style="Header.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(header, text="命令", style="Header.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Label(header, text="状态", width=16, style="Header.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 10))
        ttk.Label(header, text="PID", width=8, style="Header.TLabel").grid(row=0, column=3, sticky="w", padx=(0, 10))
        ttk.Label(header, text="操作", width=38, style="Header.TLabel").grid(row=0, column=4, sticky="w")

        if not self.tunnels:
            empty = ttk.Label(
                self.rows_frame,
                text="还没有命令配置。点击右上角“新增”添加一条 ssh -L 或其他长时间运行的命令。",
                anchor="center",
                padding=40,
            )
            empty.grid(row=1, column=0, sticky="ew")
            self.set_status_text()
            return

        for index, tunnel in enumerate(self.tunnels, start=1):
            self.render_tunnel_row(index, tunnel)

        self.set_status_text()

    def render_tunnel_row(self, row_index, tunnel):
        runtime = self.runtime_for(tunnel.id)
        row = ttk.Frame(self.rows_frame, padding=(8, 7, 8, 7))
        row.grid(row=row_index, column=0, sticky="ew")
        row.columnconfigure(1, weight=1)

        name_text = tunnel.name
        if tunnel.enabled_on_start:
            name_text += "  [自启]"
        if tunnel.auto_retry.enabled:
            name_text += "  [重试]"
        ttk.Label(row, text=name_text, width=18).grid(row=0, column=0, sticky="w", padx=(0, 10))
        command_label = ttk.Label(row, text=self.command_summary(tunnel.command), anchor="w")
        command_label.grid(row=0, column=1, sticky="ew", padx=(0, 10))

        status_text, status_style = self.status_display(runtime)
        ttk.Label(row, text=status_text, width=16, style=status_style).grid(row=0, column=2, sticky="w", padx=(0, 10))

        pid = "-"
        if runtime.process is not None and runtime.process.poll() is None:
            pid = str(runtime.process.pid)
        ttk.Label(row, text=pid, width=8).grid(row=0, column=3, sticky="w", padx=(0, 10))

        buttons = ttk.Frame(row)
        buttons.grid(row=0, column=4, sticky="w")

        if runtime.status == "waiting_retry":
            toggle_text = "重试"
        else:
            toggle_text = "关闭" if self.is_active(runtime) else "开启"
        toggle_state = "disabled" if runtime.status in ("starting", "stopping") else "normal"
        ttk.Button(
            buttons,
            text=toggle_text,
            width=6,
            state=toggle_state,
            command=lambda tunnel_id=tunnel.id: self.toggle_tunnel(tunnel_id),
        ).grid(row=0, column=0, padx=(0, 6))
        column_offset = 1
        if runtime.status == "waiting_retry":
            ttk.Button(
                buttons,
                text="取消",
                width=6,
                command=lambda tunnel_id=tunnel.id: self.stop_tunnel(tunnel_id),
            ).grid(row=0, column=1, padx=(0, 6))
            column_offset = 2
        ttk.Button(
            buttons,
            text="编辑",
            width=6,
            command=lambda tunnel_id=tunnel.id: self.edit_tunnel(tunnel_id),
        ).grid(row=0, column=column_offset, padx=(0, 6))
        ttk.Button(
            buttons,
            text="日志",
            width=6,
            command=lambda tunnel_id=tunnel.id: self.show_logs(tunnel_id),
        ).grid(row=0, column=column_offset + 1, padx=(0, 6))
        ttk.Button(
            buttons,
            text="删除",
            width=6,
            command=lambda tunnel_id=tunnel.id: self.delete_tunnel(tunnel_id),
        ).grid(row=0, column=column_offset + 2)

    def command_summary(self, command):
        command = " ".join(command.split())
        if len(command) <= 96:
            return command
        return command[:93] + "..."

    def status_display(self, runtime):
        if runtime.status == "running":
            return "运行中", "StatusRunning.TLabel"
        if runtime.status == "starting":
            return "启动中", "StatusBusy.TLabel"
        if runtime.status == "stopping":
            return "停止中", "StatusBusy.TLabel"
        if runtime.status == "waiting_retry":
            return f"等待重试 {self.retry_remaining_seconds(runtime)}s", "StatusBusy.TLabel"
        if runtime.status == "error":
            return "异常退出", "StatusError.TLabel"
        return "已停止", "StatusStopped.TLabel"

    def retry_remaining_seconds(self, runtime):
        if runtime.retry_due_at is None:
            return 0
        return max(0, int(runtime.retry_due_at - time.time() + 0.999))

    def set_status_text(self):
        running = sum(1 for tunnel in self.tunnels if self.is_running(tunnel.id))
        waiting_retry = sum(1 for tunnel in self.tunnels if self.runtime_for(tunnel.id).status == "waiting_retry")
        tray_text = "托盘已启用" if self.tray is not None else f"托盘不可用：{self.tray_error or '未启用'}"
        startup_text = "开机自启动已启用" if self.startup_var.get() else "开机自启动未启用"
        retry_text = f"{waiting_retry} 个等待重试，" if waiting_retry else ""
        self.status_var.set(
            f"{len(self.tunnels)} 个配置，{running} 个运行中，{retry_text}{tray_text}，{startup_text}，配置文件：{CONFIG_PATH}"
        )
        self.update_tray_tooltip()

    def is_active(self, runtime):
        return runtime.process is not None and runtime.process.poll() is None

    def is_running(self, tunnel_id):
        return self.is_active(self.runtime_for(tunnel_id))

    def add_tunnel(self):
        dialog = TunnelDialog(self.root, "新增命令")
        if not dialog.result:
            return
        tunnel = TunnelConfig(
            id=self.generate_id(dialog.result["name"]),
            name=dialog.result["name"],
            command=dialog.result["command"],
            enabled_on_start=dialog.result["enabled_on_start"],
            auto_retry=dialog.result["auto_retry"],
        )
        self.tunnels.append(tunnel)
        self.save_config()
        self.refresh_rows()

    def edit_tunnel(self, tunnel_id):
        tunnel = self.find_tunnel(tunnel_id)
        if tunnel is None:
            return
        command_readonly = self.is_running(tunnel_id)
        dialog = TunnelDialog(self.root, "编辑命令", tunnel, command_readonly=command_readonly)
        if not dialog.result:
            return
        tunnel.name = dialog.result["name"]
        tunnel.command = dialog.result["command"]
        tunnel.enabled_on_start = dialog.result["enabled_on_start"]
        tunnel.auto_retry = dialog.result["auto_retry"]
        self.save_config()
        self.refresh_rows()

    def delete_tunnel(self, tunnel_id):
        tunnel = self.find_tunnel(tunnel_id)
        if tunnel is None:
            return
        if self.is_running(tunnel_id):
            messagebox.showwarning("正在运行", "请先关闭该隧道，再删除配置。", parent=self.root)
            return
        if not messagebox.askyesno("确认删除", f"确定删除“{tunnel.name}”吗？", parent=self.root):
            return
        self.cancel_retry(tunnel_id)
        self.tunnels = [item for item in self.tunnels if item.id != tunnel_id]
        if tunnel_id in self.log_windows:
            self.log_windows[tunnel_id].close()
        self.runtimes.pop(tunnel_id, None)
        self.save_config()
        self.refresh_rows()

    def toggle_tunnel(self, tunnel_id):
        if self.is_running(tunnel_id):
            self.stop_tunnel(tunnel_id)
        else:
            self.start_tunnel(tunnel_id)

    def start_enabled_tunnels(self):
        for tunnel in self.tunnels:
            if tunnel.enabled_on_start:
                self.start_tunnel(tunnel.id)

    def start_tunnel(self, tunnel_id, from_retry=False):
        tunnel = self.find_tunnel(tunnel_id)
        if tunnel is None:
            return
        runtime = self.runtime_for(tunnel_id)
        if self.is_active(runtime):
            return

        self.cancel_retry(tunnel_id)
        if not from_retry:
            runtime.retry_attempts = 0
        runtime.status = "starting"
        runtime.returncode = None
        runtime.stop_requested = False
        runtime.started_at = time.time()
        self.add_log(tunnel_id, f"启动命令：{tunnel.command}")
        self.refresh_rows()

        try:
            popen_args, shell = self.build_popen_args(tunnel.command)
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(
                popen_args,
                shell=shell,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(APP_DIR),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            runtime.status = "error"
            runtime.process = None
            runtime.returncode = None
            self.add_log(tunnel_id, f"启动失败：{exc}")
            if self.schedule_retry(tunnel_id, "启动失败"):
                return
            self.refresh_rows()
            return

        runtime.process = process
        runtime.status = "running"
        self.add_log(tunnel_id, f"进程已启动，PID={process.pid}")
        threading.Thread(target=self.read_process_output, args=(tunnel_id, process), daemon=True).start()
        self.refresh_rows()

    def build_popen_args(self, command):
        if os.name == "nt":
            return command, False
        return shlex.split(command), False

    def read_process_output(self, tunnel_id, process):
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self.events.put(("log", tunnel_id, process.pid, line.rstrip("\r\n")))
        finally:
            try:
                returncode = process.wait()
            except Exception as exc:
                self.events.put(("log", tunnel_id, process.pid, f"等待进程退出时出错：{exc}"))
                returncode = None
            self.events.put(("exited", tunnel_id, process.pid, returncode))

    def retry_delay_seconds(self, retry_config, attempt):
        initial_delay = max(1, retry_config.initial_delay_seconds)
        max_delay = max(initial_delay, retry_config.max_delay_seconds)
        delay = initial_delay * (2 ** max(attempt - 1, 0))
        return min(delay, max_delay)

    def schedule_retry(self, tunnel_id, reason):
        if self.closing:
            return False
        tunnel = self.find_tunnel(tunnel_id)
        if tunnel is None or not tunnel.auto_retry.enabled:
            return False

        runtime = self.runtime_for(tunnel_id)
        retry_config = tunnel.auto_retry
        if retry_config.max_attempts > 0 and runtime.retry_attempts >= retry_config.max_attempts:
            runtime.status = "error"
            runtime.retry_due_at = None
            self.add_log(tunnel_id, f"自动重试已停止：已达到最多 {retry_config.max_attempts} 次。")
            self.notify_user(
                f"{APP_NAME}: 自动重试已停止",
                f"{tunnel.name} 已达到最多 {retry_config.max_attempts} 次重试。请打开日志查看原因。",
                warning=True,
            )
            self.refresh_rows()
            return True

        runtime.retry_attempts += 1
        delay = self.retry_delay_seconds(retry_config, runtime.retry_attempts)
        runtime.retry_due_at = time.time() + delay
        runtime.retry_generation += 1
        generation = runtime.retry_generation
        runtime.status = "waiting_retry"
        runtime.process = None
        runtime.retry_after_id = self.root.after(
            delay * 1000,
            lambda: self.run_scheduled_retry(tunnel_id, generation),
        )
        max_attempts_text = "无限" if retry_config.max_attempts == 0 else str(retry_config.max_attempts)
        self.add_log(
            tunnel_id,
            f"{reason}，自动重试已启用，{delay} 秒后进行第 {runtime.retry_attempts} 次重试（最多 {max_attempts_text} 次）。",
        )
        self.refresh_rows()
        self.ensure_retry_countdown_refresh()
        return True

    def run_scheduled_retry(self, tunnel_id, generation):
        runtime = self.runtime_for(tunnel_id)
        if runtime.retry_generation != generation or runtime.status != "waiting_retry":
            return
        runtime.retry_after_id = None
        runtime.retry_due_at = None
        self.add_log(tunnel_id, f"正在自动重试，第 {runtime.retry_attempts} 次。")
        self.start_tunnel(tunnel_id, from_retry=True)

    def cancel_retry(self, tunnel_id, log=False):
        runtime = self.runtime_for(tunnel_id)
        if runtime.retry_after_id is not None:
            try:
                self.root.after_cancel(runtime.retry_after_id)
            except tk.TclError:
                pass
        was_waiting = runtime.status == "waiting_retry"
        runtime.retry_after_id = None
        runtime.retry_due_at = None
        runtime.retry_generation += 1
        if was_waiting:
            runtime.status = "stopped"
            if log:
                self.add_log(tunnel_id, "已取消等待中的自动重试。")

    def ensure_retry_countdown_refresh(self):
        if self.retry_refresh_after_id is None and not self.closing:
            self.retry_refresh_after_id = self.root.after(1000, self.refresh_retry_countdowns)

    def refresh_retry_countdowns(self):
        self.retry_refresh_after_id = None
        if self.closing:
            return
        if any(self.runtime_for(tunnel.id).status == "waiting_retry" for tunnel in self.tunnels):
            self.refresh_rows()
            self.ensure_retry_countdown_refresh()

    def stop_tunnel(self, tunnel_id):
        runtime = self.runtime_for(tunnel_id)
        if runtime.status == "waiting_retry":
            self.cancel_retry(tunnel_id, log=True)
            self.refresh_rows()
            return
        process = runtime.process
        if process is None or process.poll() is not None:
            runtime.status = "stopped"
            self.cancel_retry(tunnel_id)
            self.refresh_rows()
            return

        self.cancel_retry(tunnel_id)
        runtime.stop_requested = True
        runtime.status = "stopping"
        self.add_log(tunnel_id, f"正在停止进程，PID={process.pid}")
        self.refresh_rows()

        try:
            process.terminate()
        except Exception as exc:
            self.add_log(tunnel_id, f"停止失败：{exc}")

        threading.Thread(target=self.wait_then_kill, args=(tunnel_id, process), daemon=True).start()

    def wait_then_kill(self, tunnel_id, process):
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            self.events.put(("log", tunnel_id, process.pid, "进程未及时退出，执行强制结束。"))
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    process.kill()
            except Exception as exc:
                self.events.put(("log", tunnel_id, process.pid, f"强制结束失败：{exc}"))
        except Exception as exc:
            self.events.put(("log", tunnel_id, process.pid, f"停止进程时出错：{exc}"))

    def stop_all(self):
        for tunnel in self.tunnels:
            runtime = self.runtime_for(tunnel.id)
            if self.is_running(tunnel.id) or runtime.status == "waiting_retry":
                self.stop_tunnel(tunnel.id)

    def any_running(self):
        return any(self.is_running(tunnel.id) for tunnel in self.tunnels)

    def show_logs(self, tunnel_id):
        tunnel = self.find_tunnel(tunnel_id)
        if tunnel is None:
            return
        if tunnel_id in self.log_windows:
            window = self.log_windows[tunnel_id].window
            window.lift()
            window.focus_force()
            return
        self.log_windows[tunnel_id] = LogWindow(self, tunnel_id, tunnel.name)

    def add_log(self, tunnel_id, message):
        runtime = self.runtime_for(tunnel_id)
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        runtime.logs.append(line)
        log_window = self.log_windows.get(tunnel_id)
        if log_window is not None:
            log_window.append(line)

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    _, tunnel_id, _pid, message = event
                    self.add_log(tunnel_id, message)
                elif kind == "exited":
                    _, tunnel_id, pid, returncode = event
                    self.handle_process_exit(tunnel_id, pid, returncode)
                elif kind == "tray_show":
                    self.show_from_tray()
                elif kind == "tray_exit":
                    self.request_exit()
                elif kind == "show_existing_instance":
                    self.show_from_tray()
        except queue.Empty:
            pass

        if not self.closing:
            self.root.after(100, self.process_events)

    def handle_process_exit(self, tunnel_id, pid, returncode):
        runtime = self.runtime_for(tunnel_id)
        if runtime.process is None or runtime.process.pid != pid:
            return
        runtime.returncode = returncode
        tunnel = self.find_tunnel(tunnel_id)

        if runtime.stop_requested:
            runtime.status = "stopped"
            runtime.retry_attempts = 0
            self.add_log(tunnel_id, f"进程已停止，退出码={returncode}")
        elif returncode == 0:
            runtime.status = "stopped"
            runtime.retry_attempts = 0
            self.add_log(tunnel_id, "进程已退出，退出码=0")
        else:
            runtime.status = "error"
            self.add_log(tunnel_id, f"进程异常退出，退出码={returncode}")
            if tunnel is not None and runtime.started_at is not None:
                ran_for = time.time() - runtime.started_at
                if ran_for >= tunnel.auto_retry.reset_after_seconds:
                    runtime.retry_attempts = 0
            runtime.process = None
            runtime.stop_requested = False
            if self.schedule_retry(tunnel_id, "进程异常退出"):
                return
            name = tunnel.name if tunnel is not None else tunnel_id
            self.notify_user(
                f"{APP_NAME}: 命令异常退出",
                f"{name} 已断开或异常退出，退出码={returncode}。请打开日志查看原因。",
                warning=True,
            )

        runtime.process = None
        runtime.stop_requested = False
        self.refresh_rows()

    def on_close(self):
        if not self.exit_confirmed:
            self.hide_to_tray(show_notice=True)
            return
        self.request_exit()

    def request_exit(self):
        running = [tunnel for tunnel in self.tunnels if self.is_running(tunnel.id)]
        if running:
            names = "、".join(tunnel.name for tunnel in running[:4])
            if len(running) > 4:
                names += f" 等 {len(running)} 个"
            if not messagebox.askyesno(
                "退出确认",
                f"仍有命令正在运行：{names}\n\n是否全部停止并退出？",
                parent=self.root,
            ):
                return
            self.exit_confirmed = True
            self.closing = True
            self.stop_all()
            self.root.after(200, lambda: self.finish_close(time.time() + STOP_TIMEOUT_SECONDS + 2))
        else:
            self.exit_confirmed = True
            self.closing = True
            self.destroy_app()

    def finish_close(self, deadline):
        self.drain_events_once()
        still_running = [tunnel for tunnel in self.tunnels if self.is_running(tunnel.id)]
        if still_running and time.time() < deadline:
            self.root.after(200, lambda: self.finish_close(deadline))
            return
        for tunnel in still_running:
            runtime = self.runtime_for(tunnel.id)
            if runtime.process is not None and runtime.process.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(runtime.process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    else:
                        runtime.process.kill()
                except Exception:
                    pass
        self.destroy_app()

    def destroy_app(self):
        for tunnel in self.tunnels:
            self.cancel_retry(tunnel.id)
        if self.retry_refresh_after_id is not None:
            try:
                self.root.after_cancel(self.retry_refresh_after_id)
            except tk.TclError:
                pass
            self.retry_refresh_after_id = None
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.instance_guard is not None:
            self.instance_guard.close()
            self.instance_guard = None
        self.root.destroy()

    def drain_events_once(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    _, tunnel_id, _pid, message = event
                    self.add_log(tunnel_id, message)
                elif event[0] == "exited":
                    _, tunnel_id, pid, returncode = event
                    self.handle_process_exit(tunnel_id, pid, returncode)
                elif event[0] == "tray_show":
                    self.show_from_tray()
                elif event[0] == "tray_exit":
                    self.request_exit()
                elif event[0] == "show_existing_instance":
                    self.show_from_tray()
        except queue.Empty:
            pass


def enable_high_dpi():
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def configure_windows_app_id():
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("cangjun.CommandTray")
    except Exception:
        pass


def main():
    enable_high_dpi()
    configure_windows_app_id()
    if "--tray-smoke-test" in sys.argv:
        events = queue.Queue()
        tray = WindowsTrayIcon(APP_NAME, events)
        ok = tray.start()
        print("tray_ok=" + str(ok))
        if not ok:
            print("tray_error=" + (tray.last_error or "unknown"))
        time.sleep(0.3)
        tray.stop()
        return
    instance_guard = SingleInstanceGuard()
    if not instance_guard.acquire():
        return
    root = tk.Tk()
    app = SSHLHelperApp(root, instance_guard=instance_guard)
    instance_guard.start_listener(app.request_show_from_other_instance)
    root.mainloop()


if __name__ == "__main__":
    main()
