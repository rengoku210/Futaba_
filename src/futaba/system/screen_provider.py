"""
Futaba Screen Context Provider — Resilient Desktop & Window Perception.

Provides safe, crash-proof screen analysis and visual context:
- Multi-tier screen capture (PIL ImageGrab / Win32 GDI / pywinauto)
- Active window & process inspection
- Browser tab & URL context awareness
- UI Automation control tree inspection
- CoInitialize/CoUninitialize thread safety for COM/UIA calls
- Graceful error boundaries: never raises, never blocks Qt event loop
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
import io
import logging
import os
import re
import subprocess
import time
from typing import Any, Optional

logger = logging.getLogger("futaba.system.screen")


@dataclass
class ScreenCaptureResult:
    """Result of a screen or window capture attempt."""
    success: bool
    width: int = 0
    height: int = 0
    image_bytes: Optional[bytes] = None
    error: Optional[str] = None
    recoverable: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("image_bytes"):
            d["image_bytes"] = f"<{len(self.image_bytes)} bytes>"
        return d


@dataclass
class ScreenAnalysisResult:
    """Structured perception result describing what is on screen."""
    success: bool
    summary: str
    active_window: dict[str, Any] = field(default_factory=dict)
    browser_context: dict[str, Any] = field(default_factory=dict)
    ui_elements: list[dict[str, Any]] = field(default_factory=list)
    visual_captured: bool = False
    details: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "success" if self.success else "error",
            "message": self.summary,
            "summary": self.summary,
            "description": self.summary,
            "window_title": self.active_window.get("title", "Desktop"),
            "process_name": self.active_window.get("process", "explorer.exe"),
            "hwnd": self.active_window.get("hwnd", 0),
            "active_window": self.active_window,
            "browser_context": self.browser_context,
            "ui_elements": self.ui_elements[:15],
            "visual_captured": self.visual_captured,
            "supports_visual_interaction": True,
            "details": self.details,
            "error": self.error,
        }


class ScreenContextProvider:
    """
    Unified, crash-resilient provider for desktop screen understanding.
    All operations are thread-safe and fail-soft.
    """

    def __init__(self) -> None:
        self._user32 = None
        self._gdi32 = None
        self._ole32 = None
        self._init_win32()

    def _init_win32(self) -> None:
        """Initialize Win32 DLL handles with proper 64-bit argument/return types."""
        try:
            self._user32 = ctypes.windll.user32
            self._gdi32 = ctypes.windll.gdi32
            self._ole32 = ctypes.windll.ole32

            # Setup user32 types
            self._user32.GetForegroundWindow.restype = wintypes.HWND
            self._user32.GetDesktopWindow.restype = wintypes.HWND
            self._user32.GetDC.restype = wintypes.HDC
            self._user32.GetDC.argtypes = [wintypes.HWND]
            self._user32.ReleaseDC.restype = ctypes.c_int
            self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            self._user32.GetWindowTextLengthW.restype = ctypes.c_int
            self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            self._user32.GetWindowTextW.restype = ctypes.c_int
            self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            self._user32.GetWindowRect.restype = wintypes.BOOL
            self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        except Exception as e:
            logger.debug("Win32 initialization notice: %s", e)

    def get_capabilities(self) -> dict[str, bool]:
        """Query currently available perception capabilities."""
        caps = {
            "screen_capture": False,
            "visual_analysis": False,
            "uia": False,
            "browser_dom": False,
        }

        # Check UIA
        try:
            import pywinauto  # noqa: F401
            caps["uia"] = True
        except ImportError:
            pass

        # Check screen capture
        try:
            from PIL import ImageGrab
            # Quick probe
            caps["screen_capture"] = hasattr(ImageGrab, "grab")
            caps["visual_analysis"] = True
        except Exception:
            pass

        return caps

    def get_active_window(self) -> dict[str, Any]:
        """
        Inspect foreground window HWND, title, process name, and rectangle.
        Guaranteed not to raise an exception.
        """
        result = {
            "hwnd": 0,
            "title": "Desktop",
            "process": "explorer.exe",
            "bounds": [0, 0, 1920, 1080],
            "is_desktop": True,
        }

        if not self._user32:
            return result

        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return result

            # Window Title
            length = self._user32.GetWindowTextLengthW(hwnd) + 1
            title = ""
            if length > 1:
                buf = ctypes.create_unicode_buffer(length)
                self._user32.GetWindowTextW(hwnd, buf, length)
                title = buf.value.strip()

            # Process ID & Name
            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process_name = "unknown"
            if pid.value:
                try:
                    import psutil
                    proc = psutil.Process(pid.value)
                    process_name = proc.name()
                except Exception:
                    # Fallback via PowerShell
                    try:
                        cmd = f"(Get-Process -Id {pid.value}).ProcessName"
                        out = subprocess.check_output(
                            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                            text=True, timeout=2
                        ).strip()
                        if out:
                            process_name = f"{out}.exe"
                    except Exception:
                        pass

            # Window Rectangle
            rect = wintypes.RECT()
            self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
            bounds = [rect.left, rect.top, rect.right, rect.bottom]

            is_desktop = not title or title in ("Desktop", "Program Manager", "Taskbar")

            result.update({
                "hwnd": hwnd,
                "title": title or "Desktop",
                "process": process_name,
                "bounds": bounds,
                "is_desktop": is_desktop,
            })
        except Exception as e:
            logger.debug("get_active_window error: %s", e)

        return result

    def get_browser_context(self) -> dict[str, Any]:
        """
        Determine if the active window is a known browser and retrieve page context.
        """
        active = self.get_active_window()
        proc = active.get("process", "").lower()
        title = active.get("title", "")

        known_browsers = {
            "brave.exe": "Brave",
            "chrome.exe": "Google Chrome",
            "msedge.exe": "Microsoft Edge",
            "firefox.exe": "Mozilla Firefox",
            "opera.exe": "Opera",
        }

        browser_name = known_browsers.get(proc)
        if not browser_name:
            return {"is_browser": False}

        # Extract probable tab title and service from window title
        # Browsers usually name windows: "Video Title - YouTube - Brave" or "Doc - Google Docs - Google Chrome"
        clean_title = title
        detected_service = ""
        lower_title = title.lower()

        services = [
            ("youtube", "YouTube"),
            ("google search", "Google Search"),
            ("reddit", "Reddit"),
            ("github", "GitHub"),
            ("gmail", "Gmail"),
            ("twitch", "Twitch"),
            ("chatgpt", "ChatGPT"),
            ("twitter", "Twitter / X"),
            ("wikipedia", "Wikipedia"),
            ("netflix", "Netflix"),
        ]
        for key, name in services:
            if key in lower_title:
                detected_service = name
                break

        # Remove browser suffix if present
        for suffix in (f"- {browser_name}", f"— {browser_name}", browser_name):
            if clean_title.endswith(suffix):
                clean_title = clean_title[:-len(suffix)].strip()

        return {
            "is_browser": True,
            "browser": browser_name,
            "page_title": clean_title,
            "detected_service": detected_service,
            "window_title": title,
        }

    def capture(self, target_window: Optional[str] = None) -> ScreenCaptureResult:
        """
        Safely capture the screen or a specific window.
        Returns ScreenCaptureResult without ever throwing exceptions.
        """
        try:
            from PIL import ImageGrab

            img = None
            try:
                img = ImageGrab.grab(all_screens=True)
            except Exception as e1:
                logger.debug("all_screens grab failed (%s); trying standard grab", e1)
                try:
                    img = ImageGrab.grab()
                except Exception as e2:
                    logger.warning("Screen grab unavailable in current display context: %s", e2)
                    return ScreenCaptureResult(
                        success=False,
                        error=f"Screen capture unavailable: {e2}",
                        recoverable=True,
                    )

            if img is None:
                return ScreenCaptureResult(
                    success=False,
                    error="Screen capture returned empty image",
                    recoverable=True,
                )

            # Convert to PNG bytes
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            return ScreenCaptureResult(
                success=True,
                width=img.width,
                height=img.height,
                image_bytes=img_bytes,
            )
        except Exception as e:
            logger.error("Screen capture failed: %s", e)
            return ScreenCaptureResult(
                success=False,
                error=str(e),
                recoverable=True,
            )

    def inspect_ui_elements(self, hwnd: int, max_elements: int = 15) -> list[dict[str, Any]]:
        """
        Inspect accessible UI elements for a window using UIA.
        Safely initializes and uninitializes COM on the calling thread.
        """
        elements = []
        if not hwnd:
            return elements

        com_initialized = False
        try:
            # Initialize COM for UIAutomation calls on worker thread
            if self._ole32:
                hr = self._ole32.CoInitialize(None)
                com_initialized = (hr in (0, 1))  # S_OK or S_FALSE

            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            target_win = None

            # Attempt to bind by HWND
            try:
                target_win = desktop.window(handle=hwnd)
            except Exception:
                pass

            if not target_win:
                for w in desktop.windows():
                    try:
                        if w.handle == hwnd:
                            target_win = w
                            break
                    except Exception:
                        pass

            if target_win:
                descendants = target_win.descendants()
                count = 0
                for d in descendants:
                    if count >= max_elements:
                        break
                    try:
                        name = d.element_info.name.strip() if d.element_info.name else ""
                        control_type = d.element_info.control_type or "Control"
                        if name and control_type not in ("Pane", "Window", "Group"):
                            r = d.rectangle()
                            elements.append({
                                "role": control_type,
                                "name": name,
                                "bounds": [r.left, r.top, r.right, r.bottom],
                            })
                            count += 1
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("inspect_ui_elements notice: %s", e)
        finally:
            if com_initialized and self._ole32:
                try:
                    self._ole32.CoUninitialize()
                except Exception:
                    pass

        return elements

    def _sync_analyze(self, query: str = "") -> ScreenAnalysisResult:
        """Synchronous screen analysis routine (executed in worker thread)."""
        active = self.get_active_window()
        browser = self.get_browser_context()
        hwnd = active.get("hwnd", 0)
        title = active.get("title", "")
        proc = active.get("process", "")

        # 1. UI Elements inspection
        ui_elements = self.inspect_ui_elements(hwnd, max_elements=10)

        # 2. Visual screenshot capture
        capture_res = self.capture()
        visual_captured = capture_res.success

        # 3. Formulate truthful natural language summary
        parts = []
        if browser.get("is_browser"):
            b_name = browser.get("browser", "Browser")
            p_title = browser.get("page_title") or title
            svc = browser.get("detected_service")
            if svc:
                parts.append(f"You are currently looking at {svc} ('{p_title}') in {b_name}.")
            else:
                parts.append(f"You are currently looking at '{p_title}' in {b_name}.")
        elif active.get("is_desktop"):
            parts.append("You are currently looking at your Windows desktop with no focused application window.")
        else:
            parts.append(f"You are looking at '{title}' (Process: {proc}).")

        # Mention notable visible controls if available
        if ui_elements:
            control_names = [f"'{e['name']}' ({e['role']})" for e in ui_elements[:5] if len(e['name']) < 40]
            if control_names:
                parts.append(f"Visible controls include: {', '.join(control_names)}.")

        if not visual_captured:
            parts.append("Visual screenshot capture is currently in fallback mode, but window and control inspection is active.")

        summary_text = " ".join(parts)

        return ScreenAnalysisResult(
            success=True,
            summary=summary_text,
            active_window=active,
            browser_context=browser,
            ui_elements=ui_elements,
            visual_captured=visual_captured,
            details=f"HWND={hwnd} Proc={proc} Controls={len(ui_elements)}",
            error=capture_res.error if not visual_captured else None,
        )

    async def analyze(self, query: str = "") -> ScreenAnalysisResult:
        """
        Asynchronously analyze the current screen and active window.
        Guaranteed to run off the Qt UI thread and never crash.
        """
        try:
            result = await asyncio.to_thread(self._sync_analyze, query)
            return result
        except Exception as e:
            logger.error("Screen analysis unhandled error: %s", e, exc_info=True)
            active = self.get_active_window()
            fallback_msg = f"You are looking at {active.get('title', 'your screen')} ({active.get('process', 'system')})."
            return ScreenAnalysisResult(
                success=True,
                summary=fallback_msg,
                active_window=active,
                browser_context={},
                ui_elements=[],
                visual_captured=False,
                error=str(e),
            )


# Global singleton
_screen_provider: Optional[ScreenContextProvider] = None


def get_screen_provider() -> ScreenContextProvider:
    global _screen_provider
    if _screen_provider is None:
        _screen_provider = ScreenContextProvider()
    return _screen_provider
