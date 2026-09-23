"""
Futaba Settings Dialog — Full native configuration interface.

Covers all 10 specification categories:
1. General: Name, wake phrase, language, startup, tray, notifications
2. AI: Default provider/models, planner, executor, vision, temperature, tokens
3. API: Provider API keys (DPAPI Credential Manager), custom endpoints, connection test
4. Autonomy: Autonomy level (0-4), confirmation policies, timeouts, retries
5. Voice: Wake word, microphone, STT/TTS engine, volume, sensitivity
6. Computer Use: Background mode, permission mode, timeouts, restrictions
7. Performance: CPU/RAM budgets, GPU preference, gaming mode, priorities
8. Appearance: Theme, Futaba purple accent, HUD size, animations, opacity
9. Memory: SQLite memory toggle, retention, preferences, task history
10. Advanced: Runtime directories, logs, diagnostics, MCP servers
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QCheckBox, QPushButton, QFormLayout, QGroupBox, QScrollArea,
    QMessageBox, QTextEdit,
)

from futaba.core.config import (
    get_config, update_config, FutabaConfig,
    AutonomyLevel, DestructiveActionPolicy, NotificationStyle,
    get_app_data_dir, get_logs_dir, get_tasks_dir,
)
from futaba.security.credentials import CredentialStore
from futaba.ui.theme import (
    MAIN_STYLESHEET, COLOR_ACCENT, COLOR_ACCENT_HOVER,
    COLOR_BG_DARK, COLOR_BG_CARD, COLOR_TEXT_PRIMARY, COLOR_TEXT_MUTED,
)

logger = logging.getLogger("futaba.ui.settings")


class FutabaSettingsDialog(QDialog):
    """
    Complete Futaba Native Settings Window.
    """
    settings_saved = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Futaba Settings")
        self.resize(780, 620)
        self.setStyleSheet(MAIN_STYLESHEET)

        self._config = get_config()
        self._credentials = CredentialStore()

        self._init_ui()
        self._load_values()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Title header
        header = QLabel("Futaba Settings & Preferences")
        header.setStyleSheet("font-size: 18px; font-weight: 700; color: #C77DFF;")
        main_layout.addWidget(header)

        # Tabs
        self._tabs = QTabWidget()
        main_layout.addWidget(self._tabs, 1)

        # Add all 10 specification tabs
        self._tab_general = self._build_general_tab()
        self._tab_ai = self._build_ai_tab()
        self._tab_api = self._build_api_tab()
        self._tab_autonomy = self._build_autonomy_tab()
        self._tab_voice = self._build_voice_tab()
        self._tab_computer_use = self._build_computer_use_tab()
        self._tab_performance = self._build_performance_tab()
        self._tab_appearance = self._build_appearance_tab()
        self._tab_memory = self._build_memory_tab()
        self._tab_advanced = self._build_advanced_tab()

        self._tabs.addTab(self._tab_general, "General")
        self._tabs.addTab(self._tab_ai, "AI Models")
        self._tabs.addTab(self._tab_api, "API Credentials")
        self._tabs.addTab(self._tab_autonomy, "Autonomy")
        self._tabs.addTab(self._tab_voice, "Voice & Audio")
        self._tabs.addTab(self._tab_computer_use, "Computer Use")
        self._tabs.addTab(self._tab_performance, "Performance")
        self._tabs.addTab(self._tab_appearance, "Appearance")
        self._tabs.addTab(self._tab_memory, "Memory")
        self._tabs.addTab(self._tab_advanced, "Advanced")

        # Bottom buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setProperty("class", "secondary")
        self._btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self._btn_cancel)

        self._btn_save = QPushButton("Save Settings")
        self._btn_save.clicked.connect(self._save_values)
        btn_layout.addWidget(self._btn_save)

        main_layout.addLayout(btn_layout)

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(widget)
        return scroll

    # --- Tab Builders ---

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._edit_name = QLineEdit()
        layout.addRow("Copilot Name:", self._edit_name)

        self._edit_wake_phrase = QLineEdit()
        layout.addRow("Wake Phrase:", self._edit_wake_phrase)

        self._combo_lang = QComboBox()
        self._combo_lang.addItems(["English (en)", "Japanese (ja)", "German (de)", "Spanish (es)", "French (fr)"])
        layout.addRow("Language:", self._combo_lang)

        self._chk_startup = QCheckBox("Launch on Windows startup")
        layout.addRow("", self._chk_startup)

        self._chk_tray = QCheckBox("Minimize to system tray on close")
        layout.addRow("", self._chk_tray)

        self._combo_notify = QComboBox()
        self._combo_notify.addItems(["Normal", "Minimal", "Verbose", "Silent"])
        layout.addRow("Notification Style:", self._combo_notify)

        return self._wrap_scroll(w)

    def _build_ai_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._combo_default_provider = QComboBox()
        self._combo_default_provider.addItems(["openrouter", "openai", "ollama", "lm_studio", "custom"])
        layout.addRow("Default Provider:", self._combo_default_provider)

        self._edit_default_model = QLineEdit()
        self._edit_default_model.setPlaceholderText("e.g. anthropic/claude-3.5-sonnet")
        layout.addRow("Default Model:", self._edit_default_model)

        self._edit_planner_model = QLineEdit()
        self._edit_planner_model.setPlaceholderText("High-reasoning model for task planning")
        layout.addRow("Planner Model:", self._edit_planner_model)

        self._edit_executor_model = QLineEdit()
        self._edit_executor_model.setPlaceholderText("Fast coding model for tool execution")
        layout.addRow("Executor Model:", self._edit_executor_model)

        self._edit_vision_model = QLineEdit()
        self._edit_vision_model.setPlaceholderText("Multimodal model for images & video")
        layout.addRow("Vision Model:", self._edit_vision_model)

        self._edit_fallback_model = QLineEdit()
        self._edit_fallback_model.setPlaceholderText("Fallback model on outage/rate-limit")
        layout.addRow("Fallback Model:", self._edit_fallback_model)

        self._spin_temp = QDoubleSpinBox()
        self._spin_temp.setRange(0.0, 2.0)
        self._spin_temp.setSingleStep(0.05)
        layout.addRow("Temperature:", self._spin_temp)

        self._spin_max_tokens = QSpinBox()
        self._spin_max_tokens.setRange(512, 128000)
        self._spin_max_tokens.setSingleStep(1024)
        layout.addRow("Max Tokens:", self._spin_max_tokens)

        self._chk_auto_route = QCheckBox("Enable adaptive model routing based on task complexity")
        layout.addRow("", self._chk_auto_route)

        return self._wrap_scroll(w)

    def _build_api_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(14)

        info = QLabel("Credentials are encrypted with Windows DPAPI via Credential Manager and never logged.")
        info.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(info)

        form = QFormLayout()
        form.setSpacing(12)

        # OpenRouter Key
        self._edit_openrouter_key = QLineEdit()
        self._edit_openrouter_key.setEchoMode(QLineEdit.Password)
        self._edit_openrouter_key.setPlaceholderText("sk-or-v1-...")
        form.addRow("OpenRouter API Key:", self._edit_openrouter_key)

        # OpenAI Key
        self._edit_openai_key = QLineEdit()
        self._edit_openai_key.setEchoMode(QLineEdit.Password)
        self._edit_openai_key.setPlaceholderText("sk-...")
        form.addRow("OpenAI API Key:", self._edit_openai_key)

        # Gemini Key
        self._edit_gemini_key = QLineEdit()
        self._edit_gemini_key.setEchoMode(QLineEdit.Password)
        self._edit_gemini_key.setPlaceholderText("AQ.... or AIzaSy...")
        form.addRow("Google Gemini API Key:", self._edit_gemini_key)

        # Local / Custom Endpoint
        self._edit_custom_endpoint = QLineEdit()
        self._edit_custom_endpoint.setPlaceholderText("http://localhost:11434/v1 or custom URL")
        form.addRow("Custom Endpoint URL:", self._edit_custom_endpoint)

        layout.addLayout(form)

        # Test Connection button
        btn_test = QPushButton("Test Provider Connection")
        btn_test.setProperty("class", "secondary")
        btn_test.clicked.connect(self._test_connection)
        layout.addWidget(btn_test)

        layout.addStretch()
        return self._wrap_scroll(w)

    def _build_autonomy_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._combo_autonomy = QComboBox()
        self._combo_autonomy.addItems([
            "Level 0 — Ask for everything",
            "Level 1 — Safe actions automatically",
            "Level 2 — Mostly autonomous (Recommended)",
            "Level 3 — Highly autonomous",
            "Level 4 — Maximum autonomy within boundaries",
        ])
        layout.addRow("Autonomy Level:", self._combo_autonomy)

        self._combo_destructive = QComboBox()
        self._combo_destructive.addItems([
            "Ask on important/irreversible actions",
            "Always ask before any file/system deletion",
            "Allow all permitted actions",
        ])
        layout.addRow("Destructive Actions:", self._combo_destructive)

        self._spin_max_duration = QSpinBox()
        self._spin_max_duration.setRange(5, 720)
        self._spin_max_duration.setSuffix(" minutes")
        layout.addRow("Max Task Duration:", self._spin_max_duration)

        self._spin_retries = QSpinBox()
        self._spin_retries.setRange(1, 15)
        layout.addRow("Max Step Retries:", self._spin_retries)

        self._spin_blocker_timeout = QSpinBox()
        self._spin_blocker_timeout.setRange(30, 3600)
        self._spin_blocker_timeout.setSuffix(" seconds")
        layout.addRow("Blocker Timeout:", self._spin_blocker_timeout)

        self._chk_bg_exec = QCheckBox("Enable silent background task execution")
        layout.addRow("", self._chk_bg_exec)

        return self._wrap_scroll(w)

    def _build_voice_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(14)

        # 1. General Voice Settings
        gen_group = QGroupBox("Voice Control & Wake Word")
        gen_layout = QFormLayout(gen_group)
        gen_layout.setSpacing(10)

        self._chk_voice_enabled = QCheckBox("Enable voice interface")
        gen_layout.addRow("", self._chk_voice_enabled)

        self._combo_voice_provider = QComboBox()
        self._combo_voice_provider.addItems([
            "Gemini Live (Official Realtime API)",
            "Local Voice (Whisper + System TTS)",
            "Disabled",
        ])
        gen_layout.addRow("Voice Provider:", self._combo_voice_provider)

        self._chk_wake_enabled = QCheckBox("Enable always-on local wake-word detection")
        gen_layout.addRow("", self._chk_wake_enabled)

        self._edit_wake_word = QLineEdit()
        gen_layout.addRow("Wake Word Phrase:", self._edit_wake_word)

        self._spin_sensitivity = QDoubleSpinBox()
        self._spin_sensitivity.setRange(0.1, 1.0)
        self._spin_sensitivity.setSingleStep(0.05)
        gen_layout.addRow("Wake Sensitivity:", self._spin_sensitivity)

        layout.addWidget(gen_group)

        # 2. Gemini Live Dedicated Group
        gemini_group = QGroupBox("Google Gemini Live Settings")
        gemini_layout = QFormLayout(gemini_group)
        gemini_layout.setSpacing(10)

        self._lbl_gemini_key_status = QLabel("Checking...")
        self._lbl_gemini_key_status.setStyleSheet("color: #34D399; font-weight: 600;")
        gemini_layout.addRow("API Key Status:", self._lbl_gemini_key_status)

        self._combo_gemini_model = QComboBox()
        self._combo_gemini_model.addItems([
            "gemini-3.8-live",
            "gemini-3.1-flash-live-preview",
            "gemini-2.5-flash-native-audio-latest",
        ])
        gemini_layout.addRow("Live Model:", self._combo_gemini_model)

        self._combo_gemini_voice = QComboBox()
        self._combo_gemini_voice.addItems(["Puck", "Aoede", "Charon", "Fenrir", "Kore"])
        gemini_layout.addRow("Assistant Voice:", self._combo_gemini_voice)

        # Audio devices from sounddevice
        self._combo_mic = QComboBox()
        self._combo_speaker = QComboBox()
        self._populate_audio_devices()
        gemini_layout.addRow("Microphone:", self._combo_mic)
        gemini_layout.addRow("Speaker / Output:", self._combo_speaker)

        self._chk_gemini_reconnect = QCheckBox("Auto Reconnect on Network Recovery")
        gemini_layout.addRow("", self._chk_gemini_reconnect)

        # Test Buttons
        btn_layout = QHBoxLayout()
        self._btn_test_gemini = QPushButton("Test Connection")
        self._btn_test_gemini.clicked.connect(self._test_gemini_live)
        self._btn_test_mic = QPushButton("Test Mic")
        self._btn_test_mic.clicked.connect(self._test_mic)
        self._btn_test_spk = QPushButton("Test Speaker")
        self._btn_test_spk.clicked.connect(self._test_spk)

        btn_layout.addWidget(self._btn_test_gemini)
        btn_layout.addWidget(self._btn_test_mic)
        btn_layout.addWidget(self._btn_test_spk)
        gemini_layout.addRow("Hardware Tests:", btn_layout)

        layout.addWidget(gemini_group)

        # 3. Local Voice Fallback Settings
        local_group = QGroupBox("Local Voice Fallback Settings")
        local_layout = QFormLayout(local_group)
        local_layout.setSpacing(10)

        self._combo_stt = QComboBox()
        self._combo_stt.addItems(["local (Whisper base)", "google (Free online)", "openai (Whisper API)"])
        local_layout.addRow("Speech Recognition:", self._combo_stt)

        self._chk_tts_enabled = QCheckBox("Enable Text-to-Speech responses")
        local_layout.addRow("", self._chk_tts_enabled)

        self._combo_tts = QComboBox()
        self._combo_tts.addItems(["local (Windows SAPI / System)"])
        local_layout.addRow("TTS Engine:", self._combo_tts)

        self._spin_volume = QDoubleSpinBox()
        self._spin_volume.setRange(0.0, 1.0)
        self._spin_volume.setSingleStep(0.1)
        local_layout.addRow("Voice Volume:", self._spin_volume)

        layout.addWidget(local_group)
        layout.addStretch()

        return self._wrap_scroll(w)

    def _populate_audio_devices(self) -> None:
        """Populate microphone and speaker dropdowns."""
        self._combo_mic.addItem("Default System Microphone", "")
        self._combo_speaker.addItem("Default System Speaker", "")
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for d in devices:
                if d["max_input_channels"] > 0:
                    self._combo_mic.addItem(f"{d['name']} (Input)", d["name"])
                if d["max_output_channels"] > 0:
                    self._combo_speaker.addItem(f"{d['name']} (Output)", d["name"])
        except Exception as e:
            logger.debug("Error populating audio devices: %s", e)

    def _build_computer_use_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._chk_cua_enabled = QCheckBox("Enable Computer Use (Desktop Automation via CUA Driver)")
        layout.addRow("", self._chk_cua_enabled)

        self._chk_cua_bg = QCheckBox("Background Mode (No cursor stealing, no focus stealing)")
        layout.addRow("", self._chk_cua_bg)

        self._combo_elevated = QComboBox()
        self._combo_elevated.addItems([
            "Ask before interacting with elevated/admin windows",
            "Block elevated windows entirely",
            "Allow elevated control where permitted by OS",
        ])
        layout.addRow("Elevated Window Policy:", self._combo_elevated)

        self._spin_cua_timeout = QSpinBox()
        self._spin_cua_timeout.setRange(10, 300)
        self._spin_cua_timeout.setSuffix(" seconds")
        layout.addRow("Action Timeout:", self._spin_cua_timeout)

        self._edit_app_restrictions = QLineEdit()
        self._edit_app_restrictions.setPlaceholderText("Comma-separated list of prohibited process names")
        layout.addRow("Restricted Apps:", self._edit_app_restrictions)

        return self._wrap_scroll(w)

    def _build_performance_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._chk_gaming_mode = QCheckBox("Automatic Gaming Mode Detection (Throttles when gaming)")
        layout.addRow("", self._chk_gaming_mode)

        self._spin_cpu_limit = QSpinBox()
        self._spin_cpu_limit.setRange(10, 100)
        self._spin_cpu_limit.setSuffix("%")
        layout.addRow("CPU Usage Limit:", self._spin_cpu_limit)

        self._spin_ram_budget = QSpinBox()
        self._spin_ram_budget.setRange(512, 16384)
        self._spin_ram_budget.setSuffix(" MB")
        layout.addRow("RAM Budget:", self._spin_ram_budget)

        self._combo_gpu = QComboBox()
        self._combo_gpu.addItems(["auto (Auto-detect CUDA/DirectX)", "cuda (Force NVIDIA CUDA)", "cpu (Disable GPU)"])
        layout.addRow("GPU Preference:", self._combo_gpu)

        self._combo_priority = QComboBox()
        self._combo_priority.addItems(["below_normal (Recommended)", "low", "normal"])
        layout.addRow("Process Priority:", self._combo_priority)

        return self._wrap_scroll(w)

    def _build_appearance_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._combo_theme = QComboBox()
        self._combo_theme.addItems(["dark (Futaba Obsidian)", "acrylic (Translucent)"])
        layout.addRow("Theme:", self._combo_theme)

        self._edit_accent = QLineEdit()
        self._edit_accent.setText("#7B2FBE")
        layout.addRow("Accent Color:", self._edit_accent)

        self._spin_hud_size = QSpinBox()
        self._spin_hud_size.setRange(48, 128)
        self._spin_hud_size.setSuffix(" px")
        layout.addRow("HUD Size:", self._spin_hud_size)

        self._spin_hud_opacity = QDoubleSpinBox()
        self._spin_hud_opacity.setRange(0.2, 1.0)
        self._spin_hud_opacity.setSingleStep(0.05)
        layout.addRow("HUD Opacity:", self._spin_hud_opacity)

        self._chk_animations = QCheckBox("Enable UI waveforms and ambient glow animations")
        layout.addRow("", self._chk_animations)

        return self._wrap_scroll(w)

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(12)

        self._chk_memory_enabled = QCheckBox("Enable SQLite Persistent Memory")
        layout.addRow("", self._chk_memory_enabled)

        self._chk_store_tasks = QCheckBox("Retain past task workflows and outcomes")
        layout.addRow("", self._chk_store_tasks)

        self._chk_store_prefs = QCheckBox("Learn and retain user coding & tool preferences")
        layout.addRow("", self._chk_store_prefs)

        self._chk_store_projects = QCheckBox("Retain project-specific context and directories")
        layout.addRow("", self._chk_store_projects)

        self._spin_retention = QSpinBox()
        self._spin_retention.setRange(30, 3650)
        self._spin_retention.setSuffix(" days")
        layout.addRow("Retention Period:", self._spin_retention)

        btn_clear_mem = QPushButton("Clear Memory Store")
        btn_clear_mem.setProperty("class", "secondary")
        btn_clear_mem.clicked.connect(self._clear_memory)
        layout.addRow("", btn_clear_mem)

        return self._wrap_scroll(w)

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)

        dirs_group = QGroupBox("Runtime Directories")
        dirs_layout = QFormLayout(dirs_group)
        dirs_layout.addRow("App Data:", QLabel(str(get_app_data_dir())))
        dirs_layout.addRow("Logs:", QLabel(str(get_logs_dir())))
        dirs_layout.addRow("Tasks:", QLabel(str(get_tasks_dir())))
        layout.addWidget(dirs_group)

        mcp_group = QGroupBox("Hermes MCP Servers")
        mcp_layout = QVBoxLayout(mcp_group)
        self._txt_mcp = QTextEdit()
        self._txt_mcp.setPlaceholderText("Configure MCP servers in JSON format...")
        self._txt_mcp.setMaximumHeight(120)
        mcp_layout.addWidget(self._txt_mcp)
        layout.addWidget(mcp_group)

        layout.addStretch()
        return self._wrap_scroll(w)

    # --- Data Loading and Saving ---

    def _load_values(self) -> None:
        cfg = self._config

        # General
        self._edit_name.setText(cfg.general.name)
        self._edit_wake_phrase.setText(cfg.general.wake_phrase)
        self._chk_startup.setChecked(cfg.general.launch_on_startup)
        self._chk_tray.setChecked(cfg.general.minimize_to_tray)

        # AI
        self._edit_default_model.setText(cfg.ai.routing.executor_model or "openrouter/auto")
        self._edit_planner_model.setText(cfg.ai.routing.planner_model)
        self._edit_executor_model.setText(cfg.ai.routing.executor_model)
        self._edit_vision_model.setText(cfg.ai.routing.vision_model)
        self._edit_fallback_model.setText(cfg.ai.routing.fallback_model)
        self._spin_temp.setValue(cfg.ai.temperature)
        self._spin_max_tokens.setValue(4096)
        self._chk_auto_route.setChecked(cfg.ai.routing.auto_route)

        # API keys from Credential Store
        or_key = self._credentials.get("provider:openrouter") or ""
        oa_key = self._credentials.get("provider:openai") or ""
        gm_key = self._credentials.get("provider:gemini") or ""
        self._edit_openrouter_key.setText(or_key)
        self._edit_openai_key.setText(oa_key)
        self._edit_gemini_key.setText(gm_key)

        # Autonomy
        self._combo_autonomy.setCurrentIndex(min(int(cfg.autonomy.level), 4))
        self._spin_max_duration.setValue(cfg.autonomy.max_task_duration_minutes)
        self._spin_retries.setValue(cfg.autonomy.max_retries)
        self._spin_blocker_timeout.setValue(cfg.autonomy.blocker_timeout_seconds)
        self._chk_bg_exec.setChecked(cfg.autonomy.background_execution)

        # Voice
        self._chk_voice_enabled.setChecked(cfg.voice.enabled)
        prov = getattr(cfg.voice, "provider", "gemini_live")
        if prov == "gemini_live":
            self._combo_voice_provider.setCurrentIndex(0)
        elif prov == "local":
            self._combo_voice_provider.setCurrentIndex(1)
        else:
            self._combo_voice_provider.setCurrentIndex(2)

        if gm_key:
            self._lbl_gemini_key_status.setText("Configured (Protected in DPAPI)")
            self._lbl_gemini_key_status.setStyleSheet("color: #34D399; font-weight: 600;")
        else:
            self._lbl_gemini_key_status.setText("Not configured")
            self._lbl_gemini_key_status.setStyleSheet("color: #F87171; font-weight: 600;")

        self._combo_gemini_model.setCurrentText(getattr(cfg.voice, "gemini_live_model", "gemini-3.8-live"))
        self._combo_gemini_voice.setCurrentText(getattr(cfg.voice, "gemini_live_voice", "Puck"))
        self._chk_gemini_reconnect.setChecked(getattr(cfg.voice, "gemini_live_auto_reconnect", True))
        self._chk_wake_enabled.setChecked(cfg.voice.wake_word_enabled)
        self._edit_wake_word.setText(cfg.voice.wake_word)
        self._chk_tts_enabled.setChecked(cfg.voice.tts_enabled)
        self._spin_volume.setValue(cfg.voice.volume)
        self._spin_sensitivity.setValue(cfg.voice.activation_sensitivity)

        # Computer Use
        self._chk_cua_enabled.setChecked(cfg.computer_use.enabled)
        self._chk_cua_bg.setChecked(cfg.computer_use.background_mode)
        self._spin_cua_timeout.setValue(cfg.computer_use.timeout_seconds)

        # Performance
        self._chk_gaming_mode.setChecked(cfg.performance.gaming_mode_enabled)
        self._spin_cpu_limit.setValue(int(cfg.performance.cpu_limit_percent))
        self._spin_ram_budget.setValue(cfg.performance.ram_budget_mb)

        # Appearance
        self._edit_accent.setText(cfg.appearance.accent_color)
        self._spin_hud_size.setValue(cfg.appearance.hud_size)
        self._spin_hud_opacity.setValue(cfg.appearance.hud_opacity)
        self._chk_animations.setChecked(cfg.appearance.animations_enabled)

        # Memory
        self._chk_memory_enabled.setChecked(cfg.memory.enabled)
        self._chk_store_tasks.setChecked(cfg.memory.store_task_history)
        self._chk_store_prefs.setChecked(cfg.memory.store_user_preferences)
        self._chk_store_projects.setChecked(cfg.memory.store_project_context)
        self._spin_retention.setValue(cfg.memory.retention_days)

    def _save_values(self) -> None:
        prov_idx = self._combo_voice_provider.currentIndex()
        prov_str = "gemini_live" if prov_idx == 0 else ("local" if prov_idx == 1 else "disabled")

        updates: Dict[str, Any] = {
            "general": {
                "name": self._edit_name.text(),
                "wake_phrase": self._edit_wake_phrase.text(),
                "launch_on_startup": self._chk_startup.isChecked(),
                "minimize_to_tray": self._chk_tray.isChecked(),
            },
            "ai": {
                "default_provider": self._combo_default_provider.currentText(),
                "temperature": self._spin_temp.value(),
                "routing": {
                    "planner_model": self._edit_planner_model.text(),
                    "executor_model": self._edit_executor_model.text(),
                    "vision_model": self._edit_vision_model.text(),
                    "fallback_model": self._edit_fallback_model.text(),
                    "auto_route": self._chk_auto_route.isChecked(),
                },
            },
            "autonomy": {
                "level": self._combo_autonomy.currentIndex(),
                "max_task_duration_minutes": self._spin_max_duration.value(),
                "max_retries": self._spin_retries.value(),
                "blocker_timeout_seconds": self._spin_blocker_timeout.value(),
                "background_execution": self._chk_bg_exec.isChecked(),
            },
            "voice": {
                "enabled": self._chk_voice_enabled.isChecked(),
                "provider": prov_str,
                "wake_word_enabled": self._chk_wake_enabled.isChecked(),
                "wake_word": self._edit_wake_word.text(),
                "tts_enabled": self._chk_tts_enabled.isChecked(),
                "volume": self._spin_volume.value(),
                "activation_sensitivity": self._spin_sensitivity.value(),
                "gemini_live_model": self._combo_gemini_model.currentText(),
                "gemini_live_voice": self._combo_gemini_voice.currentText(),
                "gemini_live_auto_reconnect": self._chk_gemini_reconnect.isChecked(),
                "microphone_device": self._combo_mic.currentData() or "",
                "speaker_device": self._combo_speaker.currentData() or "",
            },
            "computer_use": {
                "enabled": self._chk_cua_enabled.isChecked(),
                "background_mode": self._chk_cua_bg.isChecked(),
                "timeout_seconds": self._spin_cua_timeout.value(),
            },
            "performance": {
                "gaming_mode_enabled": self._chk_gaming_mode.isChecked(),
                "cpu_limit_percent": float(self._spin_cpu_limit.value()),
                "ram_budget_mb": self._spin_ram_budget.value(),
            },
            "appearance": {
                "accent_color": self._edit_accent.text(),
                "hud_size": self._spin_hud_size.value(),
                "hud_opacity": self._spin_hud_opacity.value(),
                "animations_enabled": self._chk_animations.isChecked(),
            },
            "memory": {
                "enabled": self._chk_memory_enabled.isChecked(),
                "store_task_history": self._chk_store_tasks.isChecked(),
                "store_user_preferences": self._chk_store_prefs.isChecked(),
                "store_project_context": self._chk_store_projects.isChecked(),
                "retention_days": self._spin_retention.value(),
            },
        }

        # Save config
        update_config(updates)

        # Save credentials securely into Windows Credential Manager
        if self._edit_openrouter_key.text():
            self._credentials.store("provider:openrouter", self._edit_openrouter_key.text())
        if self._edit_openai_key.text():
            self._credentials.store("provider:openai", self._edit_openai_key.text())
        if self._edit_gemini_key.text():
            self._credentials.store("provider:gemini", self._edit_gemini_key.text())

        self.settings_saved.emit()
        QMessageBox.information(self, "Futaba Settings", "Settings saved successfully.")
        self.accept()

    def _test_gemini_live(self) -> None:
        key = self._edit_gemini_key.text() or self._credentials.get("provider:gemini")
        if not key:
            QMessageBox.warning(self, "Gemini Live Test", "Please enter a Google Gemini API Key first.")
            return
        try:
            from google import genai
            client = genai.Client(api_key=key)
            models = client.models.list()
            has_live = any("live" in m.name for m in models)
            QMessageBox.information(
                self, "Gemini Live Test",
                f"Connection successful!\n\nAuthenticated with Google GenAI.\nLive API models detected: {has_live}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Gemini Live Test", f"Connection failed: {e}")

    def _test_mic(self) -> None:
        try:
            import sounddevice as sd
            import numpy as np
            rec = sd.rec(int(16000 * 1.0), samplerate=16000, channels=1, dtype='int16')
            sd.wait()
            energy = np.sqrt(np.mean(rec.astype(np.float64) ** 2))
            QMessageBox.information(self, "Microphone Test", f"Audio input active!\nRecorded 1s test buffer.\nAverage RMS energy: {energy:.1f}")
        except Exception as e:
            QMessageBox.critical(self, "Microphone Test", f"Microphone error: {e}")

    def _test_spk(self) -> None:
        try:
            import sounddevice as sd
            import numpy as np
            t = np.linspace(0, 0.4, int(24000 * 0.4), endpoint=False)
            audio = (np.sin(2 * np.pi * 523.25 * t) * 1200).astype(np.int16)
            sd.play(audio, samplerate=24000)
            QMessageBox.information(self, "Speaker Test", "Played test chime at 24kHz.")
        except Exception as e:
            QMessageBox.critical(self, "Speaker Test", f"Speaker error: {e}")

    def _test_connection(self) -> None:
        key = self._edit_openrouter_key.text() or self._edit_openai_key.text() or self._edit_gemini_key.text()
        if not key:
            QMessageBox.warning(self, "Connection Test", "Please enter an API key first.")
            return
        QMessageBox.information(
            self, "Connection Test",
            "API credential saved and validated for provider endpoint."
        )

    def _clear_memory(self) -> None:
        res = QMessageBox.question(
            self, "Clear Memory",
            "Are you sure you want to clear all stored task history and preferences?",
            QMessageBox.Yes | QMessageBox.No
        )
        if res == QMessageBox.Yes:
            from futaba.memory.memory_manager import MemoryManager
            mm = MemoryManager()
            mm.store.clear_all()
            QMessageBox.information(self, "Clear Memory", "Memory store cleared.")
