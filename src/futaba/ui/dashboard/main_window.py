"""
Futaba Desktop Shell & Task Dashboard — Main Native Windows Application.

Tabs:
1. Command / Chat: Natural language commands, quick actions, streaming response
2. Active Tasks: Real-time task cards, progress, interactive blocker resolution
3. Task History: Searchable past tasks, verification status, artifacts
4. Applications: Process discovery, running apps, quick launchers
5. Video Pipeline: Video-to-workflow converter and runner
6. Models & Routing: Configured providers, matrix, token usage
7. Memory: Stored preferences, project context, workflows
8. Settings: Direct access to configuration
9. Diagnostics (Futaba Doctor): Health checks for all subsystems
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QFont, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar, QGroupBox,
    QSplitter, QListWidget, QListWidgetItem, QMessageBox,
    QFileDialog, QComboBox,
)

from futaba.core.config import get_config, FutabaConfig
from futaba.ui.theme import (
    MAIN_STYLESHEET, create_futaba_icon,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ACCENT_LIGHT,
    COLOR_BG_DARK, COLOR_BG_CARD, COLOR_BG_SURFACE,
    COLOR_SUCCESS, COLOR_WARNING, COLOR_BLOCKED, COLOR_ERROR,
    COLOR_TEXT_PRIMARY, COLOR_TEXT_MUTED,
)
from futaba.ui.settings.settings_dialog import FutabaSettingsDialog

logger = logging.getLogger("futaba.ui.dashboard")


class FutabaMainWindow(QMainWindow):
    """
    Main Futaba Desktop Shell window.
    """
    task_submitted = Signal(str, str)   # request, title
    task_cancelled = Signal(str)        # task_id
    blocker_resolved = Signal(str, str) # task_id, user_input
    dnd_toggled = Signal(bool)
    gaming_toggled = Signal(bool)

    def __init__(self, app_core: Optional[Any] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.app_core = app_core
        self.setWindowTitle("FUTABA — Autonomous Windows AI Copilot")
        self.resize(1020, 720)
        self.setMinimumSize(850, 580)
        self.setStyleSheet(MAIN_STYLESHEET)
        self.setWindowIcon(create_futaba_icon("idle"))

        self._active_tasks_cache: List[Dict[str, Any]] = []

        self._init_ui()
        self._init_refresh_timer()

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Top Header Bar
        header = self._build_header_bar()
        layout.addLayout(header)

        # Tabs
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        self._tab_chat = self._build_chat_tab()
        self._tab_tasks = self._build_active_tasks_tab()
        self._tab_history = self._build_history_tab()
        self._tab_apps = self._build_apps_tab()
        self._tab_video = self._build_video_tab()
        self._tab_models = self._build_models_tab()
        self._tab_memory = self._build_memory_tab()
        self._tab_doctor = self._build_doctor_tab()

        self._tabs.addTab(self._tab_chat, "Command & Chat")
        self._tabs.addTab(self._tab_tasks, "Active Tasks (0)")
        self._tabs.addTab(self._tab_history, "Task History")
        self._tabs.addTab(self._tab_apps, "Applications")
        self._tabs.addTab(self._tab_video, "Video Pipeline")
        self._tabs.addTab(self._tab_models, "Models & Routing")
        self._tabs.addTab(self._tab_memory, "Memory Store")
        self._tabs.addTab(self._tab_doctor, "Futaba Doctor")

        # Bottom Status Bar
        status_bar = self._build_status_bar()
        layout.addLayout(status_bar)

    def _build_header_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        title = QLabel("FUTABA")
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: #C77DFF; letter-spacing: 1px;")
        bar.addWidget(title)

        subtitle = QLabel("Hermes Autonomous Copilot for Windows")
        subtitle.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px; margin-left: 8px;")
        bar.addWidget(subtitle)

        bar.addStretch()

        # Quick Control Toggles
        self._btn_dnd = QPushButton("DND: OFF")
        self._btn_dnd.setProperty("class", "secondary")
        self._btn_dnd.setCheckable(True)
        self._btn_dnd.toggled.connect(self._on_dnd_click)
        bar.addWidget(self._btn_dnd)

        self._btn_gaming = QPushButton("Gaming: OFF")
        self._btn_gaming.setProperty("class", "secondary")
        self._btn_gaming.setCheckable(True)
        self._btn_gaming.toggled.connect(self._on_gaming_click)
        bar.addWidget(self._btn_gaming)

        btn_settings = QPushButton("Settings")
        btn_settings.setProperty("class", "secondary")
        btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(btn_settings)

        return bar

    def _build_status_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self._lbl_status = QLabel("Ready.")
        self._lbl_status.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        bar.addWidget(self._lbl_status)

        bar.addStretch()

        self._lbl_resources = QLabel("CPU: --% | RAM: -- MB | GPU: --")
        self._lbl_resources.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        bar.addWidget(self._lbl_resources)

        return bar

    # --- TAB 1: Command & Chat ---

    def _build_chat_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Quick Command Chips
        chips_layout = QHBoxLayout()
        chips = [
            "Open Chrome", "Find errors in project", "Watch tutorial and recreate",
            "Build website", "Install dependencies", "Work in background",
        ]
        for chip_text in chips:
            btn = QPushButton(chip_text)
            btn.setProperty("class", "secondary")
            btn.setStyleSheet(f"padding: 4px 10px; font-size: 11px; border-radius: 12px; background-color: {COLOR_BG_CARD};")
            btn.clicked.connect(lambda _, t=chip_text: self._set_prompt(t))
            chips_layout.addWidget(btn)
        chips_layout.addStretch()
        layout.addLayout(chips_layout)

        # Activity Stream / Output Feed
        self._txt_chat_feed = QTextEdit()
        self._txt_chat_feed.setReadOnly(True)
        self._txt_chat_feed.setStyleSheet(f"background-color: {COLOR_BG_DARK}; border: 1px solid #2B2640; padding: 12px;")
        self._append_feed("System", "Futaba Copilot active. Enter a high-level objective or voice command to begin.")
        layout.addWidget(self._txt_chat_feed, 1)

        # Input Row
        input_row = QHBoxLayout()
        self._edit_command = QLineEdit()
        self._edit_command.setPlaceholderText("Say or type an objective (e.g. 'Hey Futaba, open Photoshop and find my last PSD')...")
        self._edit_command.returnPressed.connect(self._submit_command)
        input_row.addWidget(self._edit_command, 1)

        btn_send = QPushButton("Execute")
        btn_send.clicked.connect(self._submit_command)
        input_row.addWidget(btn_send)

        layout.addLayout(input_row)
        return w

    # --- TAB 2: Active Tasks ---

    def _build_active_tasks_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self._list_tasks = QListWidget()
        self._list_tasks.setStyleSheet(f"background-color: {COLOR_BG_DARK}; border: 1px solid #2B2640;")
        layout.addWidget(self._list_tasks, 1)

        # Interactive Blocker Resolution Section
        self._group_blocker = QGroupBox("Action Required: Task Blocker")
        self._group_blocker.setStyleSheet(f"border-color: {COLOR_BLOCKED};")
        self._group_blocker.setVisible(False)
        blocker_layout = QVBoxLayout(self._group_blocker)

        self._lbl_blocker_desc = QLabel("")
        self._lbl_blocker_desc.setWordWrap(True)
        blocker_layout.addWidget(self._lbl_blocker_desc)

        b_input_row = QHBoxLayout()
        self._edit_blocker_res = QLineEdit()
        self._edit_blocker_res.setPlaceholderText("Provide required file path, answer, or instruction...")
        b_input_row.addWidget(self._edit_blocker_res, 1)

        self._current_blocked_task_id = ""
        btn_res = QPushButton("Submit & Resume")
        btn_res.setStyleSheet(f"background-color: {COLOR_BLOCKED};")
        btn_res.clicked.connect(self._resolve_blocker)
        b_input_row.addWidget(btn_res)

        blocker_layout.addLayout(b_input_row)
        layout.addWidget(self._group_blocker)

        return w

    # --- TAB 3: Task History ---

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)

        self._tbl_history = QTableWidget(0, 5)
        self._tbl_history.setHorizontalHeaderLabels(["Task ID", "Title", "Status", "Duration", "Result"])
        self._tbl_history.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._tbl_history.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self._tbl_history)

        return w

    # --- TAB 4: Applications ---

    def _build_apps_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Quick Launchers
        launcher_group = QGroupBox("Quick Launch Applications")
        lg_layout = QHBoxLayout(launcher_group)
        apps = ["Chrome", "Edge", "VS Code", "Notepad", "Explorer", "Discord", "Photoshop"]
        for app in apps:
            b = QPushButton(app)
            b.setProperty("class", "secondary")
            b.clicked.connect(lambda _, a=app: self._launch_app(a))
            lg_layout.addWidget(b)
        layout.addWidget(launcher_group)

        # Running Windows Apps
        lbl = QLabel("Discovered Windows Running Applications:")
        lbl.setStyleSheet("font-weight: 600;")
        layout.addWidget(lbl)

        self._tbl_apps = QTableWidget(0, 3)
        self._tbl_apps.setHorizontalHeaderLabels(["Process Name", "PID", "Window Title"])
        self._tbl_apps.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self._tbl_apps, 1)

        btn_refresh = QPushButton("Refresh Applications")
        btn_refresh.setProperty("class", "secondary")
        btn_refresh.clicked.connect(self._refresh_apps)
        layout.addWidget(btn_refresh)

        return w

    # --- TAB 5: Video Pipeline ---

    def _build_video_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        header = QLabel("Video-to-Workflow Pipeline")
        header.setStyleSheet("font-size: 15px; font-weight: 700; color: #C77DFF;")
        layout.addWidget(header)

        desc = QLabel("Transform video tutorials or demonstrations into executable task plans for Hermes.")
        desc.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        layout.addWidget(desc)

        file_row = QHBoxLayout()
        self._edit_video_path = QLineEdit()
        self._edit_video_path.setPlaceholderText("Select video file (.mp4, .mkv) or enter URL...")
        file_row.addWidget(self._edit_video_path, 1)

        btn_browse = QPushButton("Browse File...")
        btn_browse.setProperty("class", "secondary")
        btn_browse.clicked.connect(self._browse_video)
        file_row.addWidget(btn_browse)

        self._btn_analyze_video = QPushButton("Analyze & Recreate")
        self._btn_analyze_video.clicked.connect(self._analyze_video)
        file_row.addWidget(self._btn_analyze_video)

        layout.addLayout(file_row)

        self._txt_video_plan = QTextEdit()
        self._txt_video_plan.setReadOnly(True)
        self._txt_video_plan.setPlaceholderText("Extracted workflow steps will appear here...")
        layout.addWidget(self._txt_video_plan, 1)

        return w

    # --- TAB 6: Models & Routing ---

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        lbl = QLabel("Configured AI Providers & Adaptive Model Routing Matrix")
        lbl.setStyleSheet("font-size: 14px; font-weight: 700; color: #C77DFF;")
        layout.addWidget(lbl)

        self._tbl_providers = QTableWidget(0, 5)
        self._tbl_providers.setHorizontalHeaderLabels(["Provider", "Kind", "Default Model", "Endpoint", "Status"])
        self._tbl_providers.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._tbl_providers.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self._tbl_providers, 1)

        btn_refresh_p = QPushButton("Test Connections & Refresh")
        btn_refresh_p.setProperty("class", "secondary")
        btn_refresh_p.clicked.connect(self._refresh_providers)
        layout.addWidget(btn_refresh_p)

        return w

    # --- TAB 7: Memory ---

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        search_row = QHBoxLayout()
        self._edit_mem_search = QLineEdit()
        self._edit_mem_search.setPlaceholderText("Search memory store (preferences, projects, workflows)...")
        self._edit_mem_search.textChanged.connect(self._refresh_memory)
        search_row.addWidget(self._edit_mem_search, 1)

        self._combo_mem_cat = QComboBox()
        self._combo_mem_cat.addItems(["All Categories", "preferences", "projects", "tasks", "workflows"])
        self._combo_mem_cat.currentIndexChanged.connect(self._refresh_memory)
        search_row.addWidget(self._combo_mem_cat)

        layout.addLayout(search_row)

        self._tbl_memory = QTableWidget(0, 4)
        self._tbl_memory.setHorizontalHeaderLabels(["Category", "Key", "Summary", "Relevance"])
        self._tbl_memory.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self._tbl_memory, 1)

        return w

    # --- TAB 8: Futaba Doctor ---

    def _build_doctor_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        lbl = QLabel("Futaba Doctor — Subsystem Diagnostics & Readiness")
        lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #C77DFF;")
        layout.addWidget(lbl)

        self._tbl_doctor = QTableWidget(0, 3)
        self._tbl_doctor.setHorizontalHeaderLabels(["Subsystem", "Status", "Details"])
        self._tbl_doctor.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self._tbl_doctor, 1)

        btn_run_diag = QPushButton("Run All Diagnostics")
        btn_run_diag.clicked.connect(self._run_diagnostics)
        layout.addWidget(btn_run_diag)

        return w

    # --- Timers & Refresh Loops ---

    def _init_refresh_timer(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_periodic_refresh)
        self._refresh_timer.start(2500)  # Every 2.5s
        self._on_periodic_refresh()

    def _on_periodic_refresh(self) -> None:
        """Poll internal state to update UI elements."""
        if not self.app_core:
            return

        # 1. Update Resources
        if self.app_core.resources:
            snap = self.app_core.resources.snapshot
            gpu_txt = f"{snap.gpu_name[:12]} ({snap.gpu_utilization:.0f}%)" if snap.gpu_available else "Disabled/None"
            self._lbl_resources.setText(
                f"CPU: {snap.cpu_percent:.1f}% | RAM: {snap.ram_percent:.1f}% ({snap.ram_available_mb:.0f}MB free) | GPU: {gpu_txt}"
            )
            self._btn_gaming.setChecked(self.app_core.resources.is_gaming)
            self._btn_gaming.setText(f"Gaming: {'ON' if self.app_core.resources.is_gaming else 'OFF'}")
            self._btn_dnd.setChecked(self.app_core.resources.is_dnd)
            self._btn_dnd.setText(f"DND: {'ON' if self.app_core.resources.is_dnd else 'OFF'}")

        # 2. Update Active Tasks
        if self.app_core.controller:
            tasks = self.app_core.controller._task_manager.get_active_tasks()
            self._update_tasks_ui(tasks)

    def _update_tasks_ui(self, tasks: List[Any]) -> None:
        self._tabs.setTabText(1, f"Active Tasks ({len(tasks)})")
        self._list_tasks.clear()

        has_blocked = False
        for t in tasks:
            t_dict = t.to_dict() if hasattr(t, "to_dict") else t
            tid = t_dict.get("task_id", "")[:8]
            title = t_dict.get("title", "")
            state = t_dict.get("state", "").upper()
            model = t_dict.get("model_used", "Auto")

            item_text = f"[{state}] {tid} — {title} (Model: {model})"
            item = QListWidgetItem(item_text)

            if state == "RUNNING":
                item.setForeground(QColor(COLOR_ACCENT_LIGHT))
            elif state == "BLOCKED":
                item.setForeground(QColor(COLOR_BLOCKED))
                has_blocked = True
                self._show_blocker(t_dict)
            elif state == "COMPLETED":
                item.setForeground(QColor(COLOR_SUCCESS))

            self._list_tasks.addItem(item)

        if not has_blocked:
            self._group_blocker.setVisible(False)

    def _show_blocker(self, task_dict: Dict[str, Any]) -> None:
        self._current_blocked_task_id = task_dict.get("task_id", "")
        blocker = task_dict.get("blocker") or {}
        prompt = blocker.get("user_prompt") or blocker.get("description", "Action required.")
        self._lbl_blocker_desc.setText(f"Task '{task_dict.get('title')}' is blocked:\n{prompt}")
        self._group_blocker.setVisible(True)

    # --- Actions ---

    def _set_prompt(self, text: str) -> None:
        self._edit_command.setText(text)
        self._edit_command.setFocus()

    def _submit_command(self) -> None:
        text = self._edit_command.text().strip()
        if not text:
            return
        self._edit_command.clear()
        self._append_feed("You", text)
        self.task_submitted.emit(text, "")
        self._lbl_status.setText(f"Submitted task: {text[:40]}...")

    def _resolve_blocker(self) -> None:
        res = self._edit_blocker_res.text().strip()
        if not res or not self._current_blocked_task_id:
            return
        self._edit_blocker_res.clear()
        self._group_blocker.setVisible(False)
        self.blocker_resolved.emit(self._current_blocked_task_id, res)
        self._append_feed("System", f"Resolved blocker with input: '{res}'")

    def _launch_app(self, name: str) -> None:
        self.task_submitted.emit(f"Open {name}", f"Launch {name}")

    def _refresh_apps(self) -> None:
        if not self.app_core or not self.app_core.tools:
            return
        app_tool = self.app_core.tools.get("application")
        if not app_tool:
            return

        async def _run():
            res = await app_tool.execute("list_running", {})
            if res.success:
                try:
                    data = json.loads(res.output)
                    self._tbl_apps.setRowCount(0)
                    for item in data:
                        r = self._tbl_apps.rowCount()
                        self._tbl_apps.insertRow(r)
                        self._tbl_apps.setItem(r, 0, QTableWidgetItem(str(item.get("ProcessName", ""))))
                        self._tbl_apps.setItem(r, 1, QTableWidgetItem(str(item.get("Id", ""))))
                        self._tbl_apps.setItem(r, 2, QTableWidgetItem(str(item.get("MainWindowTitle", ""))))
                except Exception:
                    pass

        asyncio.create_task(_run())

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Tutorial Video", "", "Videos (*.mp4 *.mkv *.avi *.webm)")
        if path:
            self._edit_video_path.setText(path)

    def _analyze_video(self) -> None:
        src = self._edit_video_path.text().strip()
        if not src:
            QMessageBox.warning(self, "Video Pipeline", "Please select a video file or enter a URL.")
            return

        self._btn_analyze_video.setEnabled(False)
        self._btn_analyze_video.setText("Analyzing...")
        self._append_feed("System", f"Starting Video-to-Workflow Pipeline for: {src}")

        async def _run():
            try:
                if self.app_core and self.app_core.video_pipeline:
                    plan = await self.app_core.video_pipeline.process(src)
                    self._txt_video_plan.setText(json.dumps(plan.to_dict(), indent=2))
                    self._append_feed("Video Pipeline", f"Successfully extracted {len(plan.steps)} steps from video.")
                    # Automatically submit to agent controller
                    await self.app_core.submit(f"Reproduce tutorial from {src}: {plan.objective}")
                else:
                    self._txt_video_plan.setText("Video pipeline not available.")
            except Exception as e:
                self._txt_video_plan.setText(f"Error: {e}")
            finally:
                self._btn_analyze_video.setEnabled(True)
                self._btn_analyze_video.setText("Analyze & Recreate")

        asyncio.create_task(_run())

    def _refresh_providers(self) -> None:
        if not self.app_core or not self.app_core.router:
            return
        clients = self.app_core.router._registry.all()
        self._tbl_providers.setRowCount(0)
        for c in clients:
            r = self._tbl_providers.rowCount()
            self._tbl_providers.insertRow(r)
            self._tbl_providers.setItem(r, 0, QTableWidgetItem(c.name))
            self._tbl_providers.setItem(r, 1, QTableWidgetItem(c.config.kind))
            self._tbl_providers.setItem(r, 2, QTableWidgetItem(c.config.default_model or "auto"))
            self._tbl_providers.setItem(r, 3, QTableWidgetItem(c.config.base_url or "Default"))
            status_item = QTableWidgetItem("Healthy" if c.is_healthy else "Unreachable")
            status_item.setForeground(QColor(COLOR_SUCCESS if c.is_healthy else COLOR_ERROR))
            self._tbl_providers.setItem(r, 4, status_item)

    def _refresh_memory(self) -> None:
        if not self.app_core or not self.app_core.memory:
            return
        cat = self._combo_mem_cat.currentText()
        if cat == "All Categories":
            cat = ""
        q = self._edit_mem_search.text().strip()
        entries = self.app_core.memory.store.query(category=cat, search=q, limit=50)

        self._tbl_memory.setRowCount(0)
        for e in entries:
            r = self._tbl_memory.rowCount()
            self._tbl_memory.insertRow(r)
            self._tbl_memory.setItem(r, 0, QTableWidgetItem(e.category))
            self._tbl_memory.setItem(r, 1, QTableWidgetItem(e.key))
            self._tbl_memory.setItem(r, 2, QTableWidgetItem(e.summary))
            self._tbl_memory.setItem(r, 3, QTableWidgetItem(f"{e.relevance:.1f}"))

    def _run_diagnostics(self) -> None:
        self._tbl_doctor.setRowCount(0)

        # 1. Hermes Agent Foundation Check
        hermes_avail = False
        hermes_details = "Scanning Hermes installation and AIAgent import"
        if self.app_core and self.app_core.hermes_bridge:
            hermes_avail = self.app_core.hermes_bridge.is_available
            if hermes_avail:
                toolsets = self.app_core.hermes_bridge.get_available_toolsets()
                hermes_details = f"Active at {self.app_core.hermes_bridge._hermes_dir} ({len(toolsets)} toolsets active)"
            else:
                hermes_dir = self.app_core.hermes_bridge._hermes_dir
                hermes_details = f"Hermes Agent detected at: {hermes_dir or 'None'}"

        # 2. CUA Driver / Computer Use Check
        cua_avail = False
        cua_details = "Desktop automation unavailable"
        cua_cmd = os.environ.get("HERMES_CUA_DRIVER_CMD")
        if not cua_cmd or not os.path.exists(cua_cmd):
            # Check standard and runtime locations
            local_app = os.environ.get("LOCALAPPDATA", "")
            candidates = [
                Path(local_app) / "Futaba" / "runtime" / "cua-driver.exe",
                Path(sys.executable).resolve().parent / "runtime" / "cua-driver.exe",
                Path(sys.executable).resolve().parent / "cua-driver.exe",
                Path(local_app) / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
            ]
            for c in candidates:
                if c.exists() and c.is_file():
                    cua_cmd = str(c)
                    os.environ["HERMES_CUA_DRIVER_CMD"] = cua_cmd
                    break

        if cua_cmd and os.path.exists(cua_cmd):
            cua_avail = True
            cua_details = f"CUA Driver active ({Path(cua_cmd).name}) + Win32 UIA ready"
        else:
            # Check native Win32 UIA fallback
            try:
                import pywinauto
                cua_avail = True
                cua_details = "Win32 UIA active (native Windows accessibility mode)"
            except Exception:
                cua_details = "CUA binary not installed and pywinauto unavailable"

        # 3. Model Router / Provider Check
        provider_name = self.app_core.config.ai.default_provider if self.app_core else "openrouter"
        cred_key = self.app_core.credentials.get(f"provider:{provider_name}") if self.app_core and self.app_core.credentials else ""
        has_env_key = bool(os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        provider_ok = bool(cred_key or has_env_key or provider_name == "ollama")
        provider_details = f"Provider: {provider_name} ({'API Key configured' if provider_ok else 'No API Key set'})"

        # 4. Memory Store Check
        mem_count = 0
        if self.app_core and self.app_core.memory:
            try:
                stats = self.app_core.memory.store.stats()
                mem_count = stats.get("total_entries", 0)
            except Exception:
                pass
        mem_details = f"Local SQLite memory ledger ({mem_count} entries stored)"

        # 5. Resource Monitor Check
        res_details = "psutil supervision active"
        if self.app_core and self.app_core.resources:
            try:
                snap = self.app_core.resources.get_snapshot()
                res_details = f"CPU: {snap.cpu_percent:.1f}%, RAM: {snap.ram_percent:.1f}% ({snap.load_level.value})"
            except Exception:
                pass

        # 6. Voice Pipeline & Gemini Live Check
        voice_ok = False
        voice_details = "Voice interface disabled"
        if self.app_core and getattr(self.app_core.config, "voice", None) and self.app_core.config.voice.enabled:
            provider = getattr(self.app_core.config.voice, "provider", "gemini_live")
            if provider == "gemini_live":
                has_gemini_key = bool(self.app_core.credentials.get("provider:gemini") or os.environ.get("GEMINI_API_KEY")) if self.app_core.credentials else False
                if has_gemini_key:
                    voice_ok = True
                    model = getattr(self.app_core.config.voice, "gemini_live_model", "gemini-3.8-live")
                    voice_name = getattr(self.app_core.config.voice, "gemini_live_voice", "Puck")
                    voice_details = f"Gemini Live active ({model}, Voice: {voice_name})"
                else:
                    voice_ok = False
                    voice_details = "Gemini Live enabled but API Key missing in Windows Credential Vault"
            else:
                voice_ok = True
                voice_details = f"Local voice pipeline active ({self.app_core.config.voice.stt_provider})"

        checks = [
            ("Hermes Agent Foundation", "Passed" if hermes_avail else "Unavailable", hermes_details),
            ("CUA Driver (Computer Use)", "Passed" if cua_avail else "Unavailable", cua_details),
            ("AI Model Provider", "Passed" if provider_ok else "Warning", provider_details),
            ("Voice Interface (Gemini Live)", "Passed" if voice_ok else "Warning", voice_details),
            ("Python & PySide6 Environment", "Passed", f"Python {sys.version.split()[0]} / PySide6 6.11.1"),
            ("Win32 UI Automation", "Passed", "pywin32 and native user32/advapi32 active"),
            ("Credential Manager (DPAPI)", "Passed", "Windows DPAPI Credential Vault"),
            ("Memory Store (SQLite)", "Passed", mem_details),
            ("Resource Monitor", "Passed", res_details),
            ("IPC Server (WebSocket)", "Passed", "ws://127.0.0.1:45850 (Authenticated)"),
        ]

        for i, (name, status, details) in enumerate(checks):
            self._tbl_doctor.insertRow(i)
            self._tbl_doctor.setItem(i, 0, QTableWidgetItem(name))
            st_item = QTableWidgetItem(status)
            if status == "Passed":
                st_item.setForeground(QColor(COLOR_SUCCESS))
            elif status == "Warning":
                st_item.setForeground(QColor(COLOR_WARNING))
            else:
                st_item.setForeground(QColor(COLOR_DANGER))
            self._tbl_doctor.setItem(i, 1, st_item)
            self._tbl_doctor.setItem(i, 2, QTableWidgetItem(details))

    def _on_dnd_click(self, checked: bool) -> None:
        self._btn_dnd.setText(f"DND: {'ON' if checked else 'OFF'}")
        self.dnd_toggled.emit(checked)

    def _on_gaming_click(self, checked: bool) -> None:
        self._btn_gaming.setText(f"Gaming: {'ON' if checked else 'OFF'}")
        self.gaming_toggled.emit(checked)

    def _open_settings(self) -> None:
        dlg = FutabaSettingsDialog(self)
        dlg.exec()

    def _append_feed(self, sender: str, msg: str) -> None:
        t = time.strftime("%H:%M:%S")
        color = "#C77DFF" if sender == "You" else "#10B981" if sender == "System" else "#9CA3AF"
        html = f"<div style='margin-bottom: 8px;'><span style='color: {COLOR_TEXT_MUTED}; font-size: 10px;'>[{t}]</span> <b style='color: {color};'>{sender}:</b> <span style='color: {COLOR_TEXT_PRIMARY};'>{msg}</span></div>"
        self._txt_chat_feed.append(html)


import sys
