"""
Futaba System Tray Integration — QSystemTrayIcon with complete status and control menu.

Specification menu items:
- Open Futaba
- Pause
- Resume
- Do Not Disturb
- Gaming Mode
- Active Tasks (submenu listing active tasks)
- Settings
- Diagnostics (Futaba Doctor)
- Restart Runtime
- Exit
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, List, Dict, Any

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QWidget

from futaba.ui.theme import (
    create_futaba_icon, COLOR_BG_CARD, COLOR_TEXT_PRIMARY, COLOR_ACCENT,
)

logger = logging.getLogger("futaba.ui.tray")


class FutabaTray(QObject):
    """
    Manages the Windows System Tray Icon, context menu, and notifications.
    """
    open_requested = Signal()
    settings_requested = Signal()
    diagnostics_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    dnd_toggled = Signal(bool)
    gaming_toggled = Signal(bool)
    restart_requested = Signal()
    exit_requested = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._parent = parent
        self._is_dnd = False
        self._is_gaming = False
        self._active_tasks: List[Dict[str, Any]] = []

        self._tray_icon = QSystemTrayIcon(parent)
        self._tray_icon.setIcon(create_futaba_icon("idle"))
        self._tray_icon.setToolTip("Futaba — Autonomous Windows AI Copilot")

        self._menu = QMenu()
        self._build_menu()

        self._tray_icon.setContextMenu(self._menu)
        self._tray_icon.activated.connect(self._on_tray_activated)

    def show(self) -> None:
        """Show the tray icon in Windows notification area."""
        self._tray_icon.show()
        logger.info("Futaba System Tray icon shown.")

    def hide(self) -> None:
        self._tray_icon.hide()

    def set_state(self, state: str) -> None:
        """Update the icon to match agent state."""
        self._tray_icon.setIcon(create_futaba_icon(state))

    def update_active_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """Update the dynamic Active Tasks submenu."""
        self._active_tasks = tasks
        self._build_menu()

    def set_dnd(self, enabled: bool) -> None:
        self._is_dnd = enabled
        self._act_dnd.setChecked(enabled)

    def set_gaming(self, enabled: bool) -> None:
        self._is_gaming = enabled
        self._act_gaming.setChecked(enabled)

    def notify(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.Information,
        force: bool = False,
    ) -> None:
        """
        Show a desktop balloon notification.
        Silenced automatically if Do Not Disturb or Gaming Mode is active unless force=True.
        """
        if (self._is_dnd or self._is_gaming) and not force:
            logger.debug("Notification suppressed by DND/Gaming mode: %s - %s", title, message)
            return

        self._tray_icon.showMessage(title, message, icon, 5000)

    # --- Menu construction ---

    def _build_menu(self) -> None:
        self._menu.clear()
        self._menu.setStyleSheet(f"""
            QMenu {{
                background-color: {COLOR_BG_CARD};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_ACCENT};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 22px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: {COLOR_ACCENT};
            }}
        """)

        # 1. Open Futaba
        act_open = self._menu.addAction("Open Futaba")
        act_open.triggered.connect(self.open_requested.emit)

        self._menu.addSeparator()

        # 2. Pause & Resume
        act_pause = self._menu.addAction("Pause")
        act_pause.triggered.connect(self.pause_requested.emit)

        act_resume = self._menu.addAction("Resume")
        act_resume.triggered.connect(self.resume_requested.emit)

        # 3. Do Not Disturb
        self._act_dnd = self._menu.addAction("Do Not Disturb")
        self._act_dnd.setCheckable(True)
        self._act_dnd.setChecked(self._is_dnd)
        self._act_dnd.toggled.connect(self.dnd_toggled.emit)

        # 4. Gaming Mode
        self._act_gaming = self._menu.addAction("Gaming Mode")
        self._act_gaming.setCheckable(True)
        self._act_gaming.setChecked(self._is_gaming)
        self._act_gaming.toggled.connect(self.gaming_toggled.emit)

        self._menu.addSeparator()

        # 5. Active Tasks Submenu
        sub_tasks = self._menu.addMenu(f"Active Tasks ({len(self._active_tasks)})")
        if not self._active_tasks:
            act_none = sub_tasks.addAction("No active tasks")
            act_none.setEnabled(False)
        else:
            for task in self._active_tasks:
                tid = task.get("task_id", "")[:8]
                title = task.get("title", "Untitled")[:25]
                state = task.get("state", "unknown")
                item = sub_tasks.addAction(f"[{state.upper()}] {tid}: {title}")
                item.triggered.connect(self.open_requested.emit)

        # 6. Settings
        act_settings = self._menu.addAction("Settings")
        act_settings.triggered.connect(self.settings_requested.emit)

        # 7. Diagnostics (Futaba Doctor)
        act_diag = self._menu.addAction("Diagnostics (Futaba Doctor)")
        act_diag.triggered.connect(self.diagnostics_requested.emit)

        # 8. Restart Runtime
        act_restart = self._menu.addAction("Restart Runtime")
        act_restart.triggered.connect(self.restart_requested.emit)

        self._menu.addSeparator()

        # 9. Exit
        act_exit = self._menu.addAction("Exit")
        act_exit.triggered.connect(self.exit_requested.emit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick or reason == QSystemTrayIcon.Trigger:
            self.open_requested.emit()
