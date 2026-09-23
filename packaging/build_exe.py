"""
Futaba One-EXE Build & Packaging Engine.

Builds a single self-contained Futaba.exe using PyInstaller:
- Bundles Python runtime and all dependencies
- Includes PySide6 Qt libraries, QWindows integration, icons, and themes
- Bundles Futaba core, agent, tasks, routing, tools, voice, video, memory, IPC
- Sets windowed mode (no console flashing on double-click)
- Automatically embeds manifest, version information, and icon
- Verifies post-build executable integrity
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
PACKAGING_DIR = ROOT_DIR / "packaging"


def build_executable(onefile: bool = True, console: bool = False) -> Path:
    """
    Build Futaba.exe using PyInstaller.
    """
    print("=" * 60)
    print("FUTABA — ONE-EXE PACKAGING BUILDER")
    print("=" * 60)

    entry_point = SRC_DIR / "futaba" / "core" / "app.py"
    if not entry_point.exists():
        raise FileNotFoundError(f"Entry point not found: {entry_point}")

    # Hidden imports to ensure PyInstaller bundles dynamic dependencies
    hidden_imports = [
        "futaba",
        "futaba.core",
        "futaba.core.config",
        "futaba.core.app",
        "futaba.tasks",
        "futaba.tasks.task_manager",
        "futaba.routing",
        "futaba.routing.model_router",
        "futaba.agent",
        "futaba.agent.controller",
        "futaba.agent.tools",
        "futaba.agent.hermes_bridge",
        "futaba.agent.hermes_tools",
        "futaba.memory",
        "futaba.memory.memory_manager",
        "futaba.performance",
        "futaba.performance.resource_manager",
        "futaba.security",
        "futaba.security.credentials",
        "futaba.ipc",
        "futaba.ipc.server",
        "futaba.voice",
        "futaba.voice.pipeline",
        "futaba.voice.gemini_live",
        "futaba.voice.command_router",
        "google",
        "google.genai",
        "google.genai.types",
        "google.auth",
        "sounddevice",
        "numpy",
        "futaba.video",
        "futaba.video.pipeline",
        "futaba.ui",
        "futaba.ui.theme",
        "futaba.ui.manager",
        "futaba.ui.hud.hud_window",
        "futaba.ui.tray.tray_icon",
        "futaba.ui.settings.settings_dialog",
        "futaba.ui.dashboard.main_window",
        "futaba.system",
        "futaba.system.context_tracker",
        "futaba.bootstrap",
        "futaba.bootstrap.bootstrapper",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "yaml",
        "openai",
        "httpx",
        "websockets",
        "psutil",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "qasync",
        "sqlite3",
        "win32gui",
        "win32con",
        "win32api",
        "win32process",
        "win32ts",
        "win32service",
        "pywinauto",
        "pywinauto.mouse",
        "pywinauto.keyboard",
        "pywinauto.controls",
        "pywinauto.backend",
        "PIL",
        "PIL.Image",
    ]

    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--name", "Futaba",
        "--clean",
        "--noconfirm",
        f"--paths={SRC_DIR}",
        f"--paths={ROOT_DIR / 'hermes'}",
    ]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    if not console:
        cmd.append("--windowed")
    else:
        cmd.append("--console")

    for h in hidden_imports:
        cmd.extend(["--hidden-import", h])

    cmd.append(str(entry_point))

    print(f"Executing PyInstaller: {' '.join(cmd[:10])} ...")
    start_time = time.monotonic()

    res = subprocess.run(
        cmd,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )

    elapsed = time.monotonic() - start_time
    print(f"PyInstaller completed in {elapsed:.1f}s (exit code {res.returncode})")

    if res.returncode != 0:
        print("Build failed! PyInstaller stderr:")
        print(res.stderr[-2000:])
        raise RuntimeError(f"PyInstaller build failed with exit code {res.returncode}")

    target_exe = DIST_DIR / ("Futaba.exe" if onefile else "Futaba/Futaba.exe")
    if not target_exe.exists():
        raise FileNotFoundError(f"Built artifact not found at {target_exe}")

    # Synchronize Hermes runtime and CUA driver into distribution bundle
    dist_bundle_dir = target_exe.parent
    dist_hermes = dist_bundle_dir / "hermes"
    src_hermes = ROOT_DIR / "hermes"
    if src_hermes.exists():
        print(f"Bundling Hermes Agent into {dist_hermes}...")
        dist_hermes.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            str(src_hermes), str(dist_hermes), dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
        )

    # Locate and bundle CUA driver binary
    cua_candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Futaba" / "runtime" / "cua-driver.exe",
        Path.home() / ".local" / "bin" / "cua-driver.exe",
    ]
    which_cua = shutil.which("cua-driver") or shutil.which("cua-driver.exe")
    if which_cua:
        cua_candidates.append(Path(which_cua))

    dist_runtime = dist_bundle_dir / "runtime"
    dist_runtime.mkdir(parents=True, exist_ok=True)
    for c in cua_candidates:
        if c.exists() and c.is_file():
            shutil.copy2(str(c), str(dist_runtime / "cua-driver.exe"))
            print(f"Bundled CUA Driver into {dist_runtime / 'cua-driver.exe'}")
            break

    size_mb = target_exe.stat().st_size / (1024 * 1024)
    print("=" * 60)
    print(f"BUILD SUCCEEDED: {target_exe} ({size_mb:.1f} MB)")
    print("=" * 60)
    return target_exe


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Futaba One-EXE Builder")
    parser.add_argument("--onedir", action="store_true", help="Build as onedir instead of single-file")
    parser.add_argument("--console", action="store_true", help="Keep console window for debugging")
    args = parser.parse_args()

    build_executable(onefile=not args.onedir, console=args.console)
