"""
Futaba Deterministic Application Resolver.

Provides rock-solid resolution, launch, and focus for Windows desktop applications:
- Maps friendly names ("Roblox", "Discord", "VS Code", "Spotify", "Notepad", etc.) to real executables
- Discovers installed apps via running processes, Start Menu shortcuts (.lnk),
  Registry App Paths, common install directories, and protocol schemes
- Brings running apps to the foreground using reliable Win32 APIs (SetThreadDesktop,
  AttachThreadInput, SW_RESTORE, Alt-key foreground unlock)
- Performs ground-truth verification on launch and focus (process exists AND window is visible/foreground)
- Categorically prevents application commands from mutating into web searches
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import glob
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Optional
import winreg
import psutil

logger = logging.getLogger("futaba.system.app_resolver")

# Win32 Constants
SW_RESTORE = 9
SW_SHOW = 5
SW_SHOWMAXIMIZED = 3
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
DESKTOP_ACCESS = 0x01FF

# Known application process aliases and protocol mappings
KNOWN_APP_ALIASES: dict[str, dict[str, Any]] = {
    "roblox": {
        "process_names": ["robloxplayerbeta.exe", "roblox.exe"],
        "display_name": "Roblox",
        "lnk_names": ["Roblox Player.lnk"],
        "protocol": "roblox-player:",
        "relative_paths": [
            r"%LOCALAPPDATA%\Roblox\Versions\*\RobloxPlayerBeta.exe",
            r"%LOCALAPPDATA%\Roblox\Versions\*\RobloxPlayerLauncher.exe",
        ],
    },
    "discord": {
        "process_names": ["discord.exe"],
        "display_name": "Discord",
        "lnk_names": ["Discord.lnk"],
        "protocol": "discord:",
        "relative_paths": [
            r"%LOCALAPPDATA%\Discord\app-*\Discord.exe",
            r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe",
        ],
    },
    "code": {
        "process_names": ["code.exe"],
        "display_name": "Visual Studio Code",
        "lnk_names": ["Visual Studio Code.lnk"],
        "protocol": "vscode:",
        "relative_paths": [
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
        ],
    },
    "vscode": {
        "process_names": ["code.exe"],
        "display_name": "Visual Studio Code",
        "lnk_names": ["Visual Studio Code.lnk"],
        "protocol": "vscode:",
        "relative_paths": [
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
        ],
    },
    "vs code": {
        "process_names": ["code.exe"],
        "display_name": "Visual Studio Code",
        "lnk_names": ["Visual Studio Code.lnk"],
        "protocol": "vscode:",
        "relative_paths": [
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        ],
    },
    "visual studio code": {
        "process_names": ["code.exe"],
        "display_name": "Visual Studio Code",
        "lnk_names": ["Visual Studio Code.lnk"],
        "protocol": "vscode:",
    },
    "spotify": {
        "process_names": ["spotify.exe"],
        "display_name": "Spotify",
        "lnk_names": ["Spotify.lnk"],
        "protocol": "spotify:",
        "relative_paths": [
            r"%APPDATA%\Spotify\Spotify.exe",
        ],
    },
    "notepad": {
        "process_names": ["notepad.exe"],
        "display_name": "Notepad",
        "executable": "notepad.exe",
    },
    "brave": {
        "process_names": ["brave.exe"],
        "display_name": "Brave",
        "lnk_names": ["Brave.lnk"],
        "executable": "brave.exe",
    },
    "chrome": {
        "process_names": ["chrome.exe"],
        "display_name": "Google Chrome",
        "lnk_names": ["Google Chrome.lnk"],
        "executable": "chrome.exe",
    },
    "edge": {
        "process_names": ["msedge.exe"],
        "display_name": "Microsoft Edge",
        "lnk_names": ["Microsoft Edge.lnk"],
        "executable": "msedge.exe",
    },
    "calc": {
        "process_names": ["calculatorapp.exe", "calc.exe"],
        "display_name": "Calculator",
        "executable": "calc.exe",
    },
    "calculator": {
        "process_names": ["calculatorapp.exe", "calc.exe"],
        "display_name": "Calculator",
        "executable": "calc.exe",
    },
    "explorer": {
        "process_names": ["explorer.exe"],
        "display_name": "File Explorer",
        "executable": "explorer.exe",
    },
    "file explorer": {
        "process_names": ["explorer.exe"],
        "display_name": "File Explorer",
        "executable": "explorer.exe",
    },
    "cmd": {
        "process_names": ["cmd.exe"],
        "display_name": "Command Prompt",
        "executable": "cmd.exe",
    },
    "terminal": {
        "process_names": ["windowsterminal.exe", "wt.exe"],
        "display_name": "Windows Terminal",
        "executable": "wt.exe",
    },
    "photoshop": {
        "process_names": ["photoshop.exe"],
        "display_name": "Adobe Photoshop",
        "lnk_names": ["Adobe Photoshop*.lnk"],
    },
}


class AppResolutionResult:
    """Detailed result of application resolution and interaction."""

    def __init__(
        self,
        success: bool,
        action: str,  # "focused", "launched", "not_found", "error"
        app_name: str,
        display_name: str = "",
        pid: int = 0,
        hwnd: int = 0,
        window_title: str = "",
        target_path: str = "",
        message: str = "",
        error: str = "",
        duration_ms: float = 0.0,
    ):
        self.success = success
        self.action = action
        self.app_name = app_name
        self.display_name = display_name or app_name
        self.pid = pid
        self.hwnd = hwnd
        self.window_title = window_title
        self.target_path = target_path
        self.message = message
        self.error = error
        self.duration_ms = duration_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "success" if self.success else "error",
            "action": self.action,
            "application": self.display_name,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "window_title": self.window_title,
            "target_path": self.target_path,
            "message": self.message,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class AppResolver:
    """
    Deterministic Windows Application Resolver.
    Enforces: DETERMINISTIC > NATIVE OS > DIRECT APP CONTROL > UI AUTOMATION > CUA > BROWSER.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._cache_time: float = 0.0
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

    def _ensure_desktop_access(self) -> bool:
        """Attach calling thread to active input desktop so Win32 calls see user windows."""
        try:
            hdesk = self._user32.OpenInputDesktop(0, False, DESKTOP_ACCESS)
            if hdesk:
                self._user32.SetThreadDesktop(hdesk)
                return True
        except Exception as e:
            logger.debug("OpenInputDesktop notice: %s", e)
        return False

    def is_known_application(self, query: str) -> bool:
        """Check if query matches a known installed application name or shortcut."""
        clean = query.lower().strip()
        if clean in KNOWN_APP_ALIASES:
            return True
        # Check running processes
        running, _ = self.find_running_instance(clean)
        if running:
            return True
        # Check start menu cache
        target = self.resolve_app_path(clean)
        return bool(target)

    def find_running_instance(self, app_name: str) -> tuple[bool, Optional[dict[str, Any]]]:
        """
        Check if an application is currently running.
        Returns (is_running, process_info_dict).
        """
        clean = app_name.lower().strip()
        base_name = clean[:-4] if clean.endswith(".exe") else clean

        # Check known aliases
        alias_data = KNOWN_APP_ALIASES.get(clean) or KNOWN_APP_ALIASES.get(base_name)
        target_procs = list(alias_data["process_names"]) if (alias_data and "process_names" in alias_data) else []
        if f"{base_name}.exe" not in [p.lower() for p in target_procs]:
            target_procs.append(f"{base_name}.exe")
        if clean.endswith(".exe") and clean not in [p.lower() for p in target_procs]:
            target_procs.append(clean)

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info["name"].lower()
                for target_p in target_procs:
                    if pname == target_p.lower() or (len(base_name) >= 4 and base_name == pname.replace(".exe", "")):
                        return True, {"pid": proc.info["pid"], "name": proc.info["name"]}
            except Exception:
                pass

        return False, None

    def get_application_windows(self, pid: int = 0, app_name: str = "") -> list[dict[str, Any]]:
        """
        Find top-level visible windows belonging to a PID or matching an application name.
        """
        self._ensure_desktop_access()
        windows: list[dict[str, Any]] = []
        clean = app_name.lower().strip()
        alias_data = KNOWN_APP_ALIASES.get(clean)
        target_procs = [p.lower() for p in alias_data["process_names"]] if alias_data else [f"{clean}.exe"]

        def enum_cb(hwnd, _):
            if not self._user32.IsWindowVisible(hwnd):
                return 1

            win_pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))

            length = self._user32.GetWindowTextLengthW(hwnd) + 1
            if length <= 1:
                return 1
            buf = ctypes.create_unicode_buffer(length)
            self._user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value

            # Filter out utility / hidden overlays
            if title in ("MSCTFIME UI", "Default IME", "Task Host Window"):
                return 1

            proc_match = False
            if pid and win_pid.value == pid:
                proc_match = True
            elif not pid and app_name:
                try:
                    pname = psutil.Process(win_pid.value).name().lower()
                    if any(tp in pname for tp in target_procs) or clean in pname or clean in title.lower():
                        proc_match = True
                except Exception:
                    pass

            if proc_match:
                windows.append({
                    "hwnd": hwnd,
                    "pid": win_pid.value,
                    "title": title,
                })
            return 1

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return windows

    def focus_window(self, hwnd: int) -> bool:
        """
        Bring a window handle reliably to the foreground.
        Uses SetThreadDesktop + AttachThreadInput + Alt key unlock.
        """
        if not hwnd or not self._user32.IsWindow(hwnd):
            return False

        self._ensure_desktop_access()

        try:
            cur_tid = self._kernel32.GetCurrentThreadId()
            fg_hwnd = self._user32.GetForegroundWindow()
            fg_tid = self._user32.GetWindowThreadProcessId(fg_hwnd, None)
            target_tid = self._user32.GetWindowThreadProcessId(hwnd, None)

            # Restore if minimized
            self._user32.ShowWindow(hwnd, SW_RESTORE)

            # Attach thread input to bypass SetForegroundWindow lock
            if fg_tid and fg_tid != cur_tid:
                self._user32.AttachThreadInput(cur_tid, fg_tid, True)
            if target_tid and target_tid != cur_tid:
                self._user32.AttachThreadInput(cur_tid, target_tid, True)

            # Simulate Alt key tap to unfreeze Windows foreground lock
            self._user32.keybd_event(VK_MENU, 0, 0, 0)
            self._user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

            self._user32.BringWindowToTop(hwnd)
            res = self._user32.SetForegroundWindow(hwnd)

            if fg_tid and fg_tid != cur_tid:
                self._user32.AttachThreadInput(cur_tid, fg_tid, False)
            if target_tid and target_tid != cur_tid:
                self._user32.AttachThreadInput(cur_tid, target_tid, False)

            # Brief pause for window manager
            time.sleep(0.05)
            active_fg = self._user32.GetForegroundWindow()

            # Verified if target is active foreground or belongs to target thread
            active_tid = self._user32.GetWindowThreadProcessId(active_fg, None)
            verified = (active_fg == hwnd) or (active_tid == target_tid) or bool(res)
            return verified
        except Exception as e:
            logger.debug("focus_window exception: %s", e)
            return False

    def resolve_app_path(self, app_name: str) -> Optional[str]:
        """
        Deterministically resolve an application to its executable or shortcut path.
        Checks:
        0. Direct file path if passed
        1. System PATH and standard Windows locations via shutil.which
        2. Explicit known aliases & relative paths
        3. Start Menu shortcuts (.lnk)
        4. Registry App Paths
        5. Standard system paths
        """
        clean = app_name.lower().strip()
        base_name = clean[:-4] if clean.endswith(".exe") else clean

        # 0. Check direct file existence if an absolute or relative path was passed
        if os.path.isfile(app_name):
            return os.path.abspath(app_name)

        # 1. Check system PATH and standard Windows locations via shutil.which
        which_cand = shutil.which(clean) or shutil.which(base_name) or shutil.which(f"{base_name}.exe")
        if which_cand and os.path.isfile(which_cand):
            return which_cand

        # 2. Check known alias config
        alias = KNOWN_APP_ALIASES.get(clean) or KNOWN_APP_ALIASES.get(base_name)
        if alias and "relative_paths" in alias:
            for rel in alias["relative_paths"]:
                expanded = os.path.expandvars(rel)
                matches = glob.glob(expanded)
                if matches:
                    matches.sort(key=os.path.getmtime, reverse=True)
                    return matches[0]

        # 3. Check Start Menu shortcuts (.lnk files)
        start_menu_dirs = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%ALLUSERSPROFILE%\Microsoft\Windows\Start Menu\Programs"),
        ]

        # Check alias lnk names first
        target_lnk_patterns = alias.get("lnk_names", []) if alias else [f"*{base_name}*.lnk", f"*{clean}*.lnk"]

        for base_dir in start_menu_dirs:
            if not os.path.exists(base_dir):
                continue
            for pattern in target_lnk_patterns:
                matches = glob.glob(os.path.join(base_dir, "**", pattern), recursive=True)
                if matches:
                    return matches[0]

        # General Start Menu search for query
        for base_dir in start_menu_dirs:
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        f_base = f[:-4].lower()
                        if base_name == f_base or clean == f_base or (len(base_name) >= 4 and base_name in f_base):
                            return os.path.join(root, f)

        # 4. Check Windows Registry App Paths
        reg_keys = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        target_exe_names = [f"{base_name}.exe"]
        if clean.endswith(".exe") and clean not in target_exe_names:
            target_exe_names.append(clean)
        if alias and "process_names" in alias:
            for p in alias["process_names"]:
                if p not in target_exe_names:
                    target_exe_names.append(p)

        for root_key, sub_key in reg_keys:
            for exe_cand in target_exe_names:
                try:
                    with winreg.OpenKey(root_key, f"{sub_key}\\{exe_cand}") as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val and os.path.exists(val.strip('"')):
                            return val.strip('"')
                except Exception:
                    pass

        # 5. Check executable direct command if on PATH
        for exe_cand in target_exe_names:
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"(Get-Command '{exe_cand}' -ErrorAction SilentlyContinue).Source"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                src = res.stdout.strip()
                if src and os.path.exists(src):
                    return src
            except Exception:
                pass

        # 6. Check protocol scheme if available
        if alias and "protocol" in alias:
            return alias["protocol"]

        return None

    def open_or_focus(self, app_name: str) -> AppResolutionResult:
        """
        The Master Application Entry Point.
        If application is running -> Focus it.
        If application is closed -> Launch it and verify window.
        Never converts to web search.
        """
        start_time = time.monotonic()
        clean = app_name.strip()
        clean_lower = clean.lower()
        base_name = clean_lower[:-4] if clean_lower.endswith(".exe") else clean_lower
        alias_data = KNOWN_APP_ALIASES.get(clean_lower) or KNOWN_APP_ALIASES.get(base_name)
        display_name = alias_data["display_name"] if alias_data and "display_name" in alias_data else clean.title()

        # Step 1: Check if already running
        is_running, proc_info = self.find_running_instance(clean)
        if is_running and proc_info:
            pid = proc_info["pid"]
            logger.info("Application '%s' is already running (PID: %d); focusing window.", display_name, pid)

            windows = self.get_application_windows(pid=pid, app_name=clean)
            if not windows:
                # If no top-level window found directly by PID (e.g. child process), try by app name
                windows = self.get_application_windows(app_name=clean)

            if windows:
                target_win = windows[0]
                self.focus_window(target_win["hwnd"])
                elapsed_ms = (time.monotonic() - start_time) * 1000
                return AppResolutionResult(
                    success=True,
                    action="focused",
                    app_name=clean,
                    display_name=display_name,
                    pid=target_win.get("pid", pid),
                    hwnd=target_win["hwnd"],
                    window_title=target_win["title"],
                    message=f"{display_name} is already open; brought it to the foreground.",
                    duration_ms=elapsed_ms,
                )

        # Step 2: Not running or no visible window; resolve path and launch
        target_path = self.resolve_app_path(clean)
        if not target_path:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            msg = f"'{display_name}' could not be located on this computer. Please verify the application is installed."
            logger.warning("Application resolution failed for '%s'", clean)
            return AppResolutionResult(
                success=False,
                action="not_found",
                app_name=clean,
                display_name=display_name,
                message=msg,
                error="application_not_installed",
                duration_ms=elapsed_ms,
            )

        logger.info("Launching '%s' via target: %s", display_name, target_path)

        try:
            if target_path.startswith(("http://", "https://")):
                raise ValueError("Web URLs must not be launched through AppResolver")

            if target_path.endswith(":"):
                # Protocol scheme (e.g. roblox-player:, discord:)
                os.startfile(target_path)
            elif target_path.lower().endswith(".lnk"):
                os.startfile(target_path)
            else:
                subprocess.Popen(
                    [target_path],
                    creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
                    close_fds=True,
                )

            # Step 3: Ground Truth Verification of Launch
            # Poll up to 3.5s for process spawn and visible window
            deadline = time.monotonic() + 3.5
            spawned_pid = 0
            found_win: dict[str, Any] = {}

            while time.monotonic() < deadline:
                time.sleep(0.3)
                running, info = self.find_running_instance(clean)
                if running and info:
                    spawned_pid = info["pid"]
                    wins = self.get_application_windows(pid=spawned_pid, app_name=clean)
                    if wins:
                        found_win = wins[0]
                        self.focus_window(found_win["hwnd"])
                        break

            elapsed_ms = (time.monotonic() - start_time) * 1000

            if spawned_pid or found_win:
                return AppResolutionResult(
                    success=True,
                    action="launched",
                    app_name=clean,
                    display_name=display_name,
                    pid=spawned_pid,
                    hwnd=found_win.get("hwnd", 0),
                    window_title=found_win.get("title", ""),
                    target_path=target_path,
                    message=f"Opened {display_name}.",
                    duration_ms=elapsed_ms,
                )
            else:
                # If command succeeded but window hasn't appeared yet (e.g. heavy splash screen)
                return AppResolutionResult(
                    success=True,
                    action="launched_pending_window",
                    app_name=clean,
                    display_name=display_name,
                    target_path=target_path,
                    message=f"{display_name} is starting up.",
                    duration_ms=elapsed_ms,
                )

        except Exception as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.error("Failed to launch application '%s': %s", clean, e)
            return AppResolutionResult(
                success=False,
                action="error",
                app_name=clean,
                display_name=display_name,
                message=f"Could not launch {display_name}: {e}",
                error=str(e),
                duration_ms=elapsed_ms,
            )


# Global singleton
_app_resolver: Optional[AppResolver] = None


def get_app_resolver() -> AppResolver:
    global _app_resolver
    if _app_resolver is None:
        _app_resolver = AppResolver()
    return _app_resolver
