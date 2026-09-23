# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\futaba cop\\src\\futaba\\core\\app.py'],
    pathex=['D:\\futaba cop\\src', 'D:\\futaba cop\\hermes'],
    binaries=[],
    datas=[],
    hiddenimports=['futaba', 'futaba.core', 'futaba.core.config', 'futaba.core.app', 'futaba.tasks', 'futaba.tasks.task_manager', 'futaba.routing', 'futaba.routing.model_router', 'futaba.agent', 'futaba.agent.controller', 'futaba.agent.tools', 'futaba.agent.hermes_bridge', 'futaba.agent.hermes_tools', 'futaba.memory', 'futaba.memory.memory_manager', 'futaba.performance', 'futaba.performance.resource_manager', 'futaba.security', 'futaba.security.credentials', 'futaba.ipc', 'futaba.ipc.server', 'futaba.voice', 'futaba.voice.pipeline', 'futaba.voice.gemini_live', 'futaba.voice.command_router', 'google', 'google.genai', 'google.genai.types', 'google.auth', 'sounddevice', 'numpy', 'futaba.video', 'futaba.video.pipeline', 'futaba.ui', 'futaba.ui.theme', 'futaba.ui.manager', 'futaba.ui.hud.hud_window', 'futaba.ui.tray.tray_icon', 'futaba.ui.settings.settings_dialog', 'futaba.ui.dashboard.main_window', 'futaba.system', 'futaba.system.context_tracker', 'futaba.bootstrap', 'futaba.bootstrap.bootstrapper', 'pydantic', 'pydantic_core', 'pydantic_settings', 'yaml', 'openai', 'httpx', 'websockets', 'psutil', 'PySide6', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'qasync', 'sqlite3', 'win32gui', 'win32con', 'win32api', 'win32process', 'win32ts', 'win32service', 'pywinauto', 'pywinauto.mouse', 'pywinauto.keyboard', 'pywinauto.controls', 'pywinauto.backend', 'PIL', 'PIL.Image'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Futaba',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Futaba',
)
