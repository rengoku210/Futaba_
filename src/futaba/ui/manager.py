"""
Futaba UI Manager — Orchestrates Qt Application, HUD, System Tray, and Main Dashboard.

Integrates asyncio and PySide6 using qasync so asynchronous agent tasks,
WebSocket IPC events, and native UI events run seamlessly together.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer, QObject, Signal, Slot
import qasync

from futaba.ui.hud.hud_window import FutabaHUD
from futaba.ui.tray.tray_icon import FutabaTray
from futaba.ui.dashboard.main_window import FutabaMainWindow
from futaba.ui.settings.settings_dialog import FutabaSettingsDialog
from futaba.core.config import get_config

logger = logging.getLogger("futaba.ui.manager")


class QtDispatcher(QObject):
    """
    Safely dispatches callables to the Qt main GUI thread from any worker thread,
    asyncio task, or external callback.
    """
    _dispatch_signal = Signal(object, tuple, dict)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._dispatch_signal.connect(self._execute, Qt.QueuedConnection)

    @Slot(object, tuple, dict)
    def _execute(self, func: Any, args: tuple, kwargs: dict) -> None:
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error("QtDispatcher execution error for %s: %s", getattr(func, "__name__", str(func)), e)

    def dispatch(self, func: Any, *args: Any, **kwargs: Any) -> None:
        """Enqueue function execution onto the Qt GUI main thread."""
        self._dispatch_signal.emit(func, args, kwargs)


class UIManager:
    """
    Coordinates all native UI components of Futaba:
    - 64x64 Floating HUD
    - Windows Notification Area Tray Icon
    - Main Desktop Shell & Task Dashboard
    """

    def __init__(self, app_core: Any):
        self.app_core = app_core
        self.qapp: Optional[QApplication] = None
        self.hud: Optional[FutabaHUD] = None
        self.tray: Optional[FutabaTray] = None
        self.main_window: Optional[FutabaMainWindow] = None
        self.dispatcher = QtDispatcher()
        self._settings_dialog: Optional[FutabaSettingsDialog] = None

    def initialize(self) -> None:
        """Create Qt widgets and bind signals."""
        if QApplication.instance() is None:
            self.qapp = QApplication(sys.argv)
            self.qapp.setApplicationName("Futaba")
            self.qapp.setQuitOnLastWindowClosed(False)  # Keep running in tray / HUD
        else:
            self.qapp = QApplication.instance()

        # 1. Main Dashboard Window
        self.main_window = FutabaMainWindow(app_core=self.app_core)

        # 2. System Tray
        self.tray = FutabaTray()
        self.tray.open_requested.connect(self.show_main_window)
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.diagnostics_requested.connect(self.show_diagnostics)
        self.tray.pause_requested.connect(self._pause_tasks)
        self.tray.resume_requested.connect(self._resume_tasks)
        self.tray.dnd_toggled.connect(self._toggle_dnd)
        self.tray.gaming_toggled.connect(self._toggle_gaming)
        self.tray.exit_requested.connect(self._exit_app)
        self.tray.show()

        # 3. 64x64 Floating HUD
        self.hud = FutabaHUD(
            on_open_dashboard=self.toggle_main_window,
            on_pause_resume=self._toggle_pause,
            on_toggle_dnd=lambda: self._toggle_dnd(not self.app_core.resources.is_dnd),
            on_toggle_gaming=lambda: self._toggle_gaming(not self.app_core.resources.is_gaming),
            on_talk_to_futaba=self._on_hud_talk,
            on_exit=self._exit_app,
        )
        self.hud.show()

        # 4. Wire Dashboard signals to app_core
        self.main_window.task_submitted.connect(self._on_task_submitted)
        self.main_window.task_cancelled.connect(self._on_task_cancelled)
        self.main_window.blocker_resolved.connect(self._on_blocker_resolved)
        self.main_window.dnd_toggled.connect(self._toggle_dnd)
        self.main_window.gaming_toggled.connect(self._toggle_gaming)

        # 5. Wire App Core events to UI updates via QtDispatcher
        self._wire_app_events()

        logger.info("Futaba UI Manager initialized (HUD, Tray, Dashboard active).")

    def _wire_app_events(self) -> None:
        """Wire FutabaApp and controller callbacks to UI state changes using main-thread dispatcher."""
        orig_status = self.app_core.controller._on_status_update
        orig_blocker = self.app_core.controller._on_blocker
        orig_complete = self.app_core.controller._on_complete

        async def _ui_status(task_id: str, status: str):
            if orig_status:
                await orig_status(task_id, status)
            self.dispatcher.dispatch(self._apply_status_update, status)

        async def _ui_blocker(task_id: str, blocker: Any):
            if orig_blocker:
                await orig_blocker(task_id, blocker)
            desc = blocker.user_prompt if hasattr(blocker, "user_prompt") else str(blocker)
            self.dispatcher.dispatch(self._apply_blocker_update, desc)

        async def _ui_complete(task_id: str, result: str):
            if orig_complete:
                await orig_complete(task_id, result)
            self.dispatcher.dispatch(self._apply_complete_update, result)

        self.app_core.controller._on_status_update = _ui_status
        self.app_core.controller._on_blocker = _ui_blocker
        self.app_core.controller._on_complete = _ui_complete

        # Wire voice pipeline state updates to HUD via dispatcher
        if hasattr(self.app_core, "voice_pipeline") and self.app_core.voice_pipeline:
            def _on_voice_state(state: str, detail: str):
                self.dispatcher.dispatch(self._apply_voice_state, state, detail)

            self.app_core.voice_pipeline._on_state = _on_voice_state

    # --- Main-thread safe UI update implementations ---

    def _apply_status_update(self, status: str) -> None:
        if self.hud:
            self.hud.set_state("working", status)
        if self.tray:
            self.tray.set_state("working")

    def _apply_blocker_update(self, desc: str) -> None:
        if self.hud:
            self.hud.set_state("blocked", "Action required")
        if self.tray:
            self.tray.set_state("blocked")
            self.tray.notify("Futaba Task Blocked", f"Input needed: {desc[:100]}")

    def _apply_complete_update(self, result: str) -> None:
        if self.hud:
            self.hud.set_state("completed", "Task Complete")
        if self.tray:
            self.tray.set_state("completed")
            self.tray.notify("Task Completed", result[:120])
        # Reset to idle after 4 seconds on main thread
        QTimer.singleShot(4000, self._reset_idle)

    def _apply_voice_state(self, state: str, detail: str) -> None:
        if self.hud:
            self.hud.set_state(state, detail)
        if self.tray and state in ("listening", "speaking", "idle", "error"):
            self.tray.set_state(state if state != "speaking" else "working")

    def _reset_idle(self) -> None:
        if self.hud:
            self.hud.set_state("idle", "Ready")
        if self.tray:
            self.tray.set_state("idle")

    # --- UI Controls ---

    def show_main_window(self) -> None:
        if self.main_window:
            self.main_window.show()
            self.main_window.raise_()
            self.main_window.activateWindow()

    def toggle_main_window(self) -> None:
        if not self.main_window:
            return
        if self.main_window.isVisible():
            self.main_window.hide()
        else:
            self.show_main_window()

    def show_settings(self) -> None:
        if not self._settings_dialog or not self._settings_dialog.isVisible():
            self._settings_dialog = FutabaSettingsDialog(self.main_window)
            self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def show_diagnostics(self) -> None:
        self.show_main_window()
        if self.main_window:
            self.main_window._tabs.setCurrentIndex(7)  # Doctor tab
            self.main_window._run_diagnostics()

    # --- Task dispatching ---

    def _on_task_submitted(self, request: str, title: str) -> None:
        if self.hud:
            self.hud.set_state("working", f"Planning: {request[:30]}...")
        asyncio.create_task(self.app_core.submit(request, title))

    def _on_task_cancelled(self, task_id: str) -> None:
        asyncio.create_task(self.app_core.controller._task_manager.cancel_task(task_id))

    def _on_blocker_resolved(self, task_id: str, user_input: str) -> None:
        if self.hud:
            self.hud.set_state("working", "Resuming task...")
        asyncio.create_task(self.app_core.controller.provide_input(task_id, user_input))

    def _toggle_pause(self) -> None:
        q = self.app_core.controller._task_manager._queue
        if q._paused:
            self._resume_tasks()
        else:
            self._pause_tasks()

    def _pause_tasks(self) -> None:
        self.app_core.controller._task_manager._queue.pause()
        if self.hud:
            self.hud.set_state("waiting", "Paused")
        if self.tray:
            self.tray.notify("Futaba Paused", "Task execution paused.")

    def _resume_tasks(self) -> None:
        self.app_core.controller._task_manager._queue.resume()
        if self.hud:
            self.hud.set_state("idle", "Resumed")
        if self.tray:
            self.tray.notify("Futaba Resumed", "Task execution resumed.")

    def _toggle_dnd(self, enabled: bool) -> None:
        self.app_core.resources.set_dnd(enabled)
        if self.tray:
            self.tray.set_dnd(enabled)

    def _toggle_gaming(self, enabled: bool) -> None:
        self.app_core.resources.force_gaming_mode(enabled)
        if self.tray:
            self.tray.set_gaming(enabled)

    def _on_hud_talk(self) -> None:
        """Trigger immediate voice turn with Futaba."""
        logger.info("Voice conversation triggered from HUD.")
        if self.hud:
            self.hud.set_state("listening", "Listening to you...")
        if self.app_core and getattr(self.app_core, "voice_pipeline", None):
            asyncio.create_task(
                self.app_core.voice_pipeline.respond("Hey! I'm listening. What can I do for you?")
            )

    def _exit_app(self) -> None:
        logger.info("Exit requested from UI.")
        self.cleanup()
        self.app_core._shutdown_event.set()
        asyncio.create_task(self._async_shutdown())

    async def _async_shutdown(self) -> None:
        try:
            await self.app_core.stop()
        except Exception as e:
            logger.warning("Error during app core stop: %s", e)
        finally:
            if self.qapp:
                self.qapp.quit()

    def cleanup(self) -> None:
        """Immediately hide and clean up UI elements to prevent ghost tray icons."""
        if self.tray:
            try:
                self.tray.hide()
            except Exception:
                pass
        if self.hud:
            try:
                self.hud.hide()
                self.hud.close()
            except Exception:
                pass
        if self.main_window:
            try:
                self.main_window.hide()
                self.main_window.close()
            except Exception:
                pass


async def run_ui_loop(app_core: Any) -> None:
    """
    Launch the native Windows UI on the asyncio event loop using qasync.
    """
    ui_mgr = UIManager(app_core)
    ui_mgr.initialize()

    # Show dashboard on initial launch if configured
    config = get_config()
    if not config.general.minimize_to_tray:
        ui_mgr.show_main_window()

    try:
        # Run until application shutdown
        await app_core._shutdown_event.wait()
    finally:
        ui_mgr.cleanup()
