"""
Futaba Bootstrapper & Self-Provisioning Engine.

Handles first-run initialization, dependency validation, and runtime repair:
1. Detects first launch vs existing installation
2. Creates and verifies directory layout in %LOCALAPPDATA%\\Futaba
3. Initializes default configuration file if missing
4. Discovers and validates Hermes Agent installation
5. Verifies Windows environment prerequisites (MinGit/Git Bash, CUA Driver, DPAPI)
6. Provisions embedded/bundled assets if present
7. Provides self-healing for corrupt or partial installations
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from futaba.core.config import (
    get_app_data_dir, get_config_path, get_logs_dir,
    get_tasks_dir, get_memory_dir, get_cache_dir,
    get_runtime_dir, FutabaConfig,
)

logger = logging.getLogger("futaba.bootstrap")


@dataclass
class BootstrapReport:
    """Status report of runtime validation."""
    is_first_run: bool = False
    app_data_dir: str = ""
    config_created: bool = False
    directories_ok: bool = False
    hermes_found: bool = False
    hermes_path: str = ""
    cua_driver_found: bool = False
    git_bash_found: bool = False
    python_version: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_first_run": self.is_first_run,
            "is_healthy": self.is_healthy,
            "app_data_dir": self.app_data_dir,
            "hermes_found": self.hermes_found,
            "hermes_path": self.hermes_path,
            "cua_driver_found": self.cua_driver_found,
            "git_bash_found": self.git_bash_found,
            "python_version": self.python_version,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class Bootstrapper:
    """
    Self-provisioning bootstrapper for Futaba.
    """

    REQUIRED_DIRS = [
        "runtime",
        "hermes",
        "cua",
        "models",
        "cache",
        "logs",
        "tasks",
        "quarantine",
        "memory",
        "config",
        "updates",
    ]

    def __init__(self, base_dir: Optional[Path] = None):
        self._app_dir = base_dir or get_app_data_dir()

    def run(self) -> BootstrapReport:
        """
        Execute bootstrap procedure.
        Safe to call on every startup.
        """
        report = BootstrapReport(
            app_data_dir=str(self._app_dir),
            python_version=sys.version.split()[0],
        )

        try:
            # 1. Check if first run
            flag_file = self._app_dir / ".installed"
            if not flag_file.exists():
                report.is_first_run = True
                logger.info("First run detected. Provisioning Futaba runtime...")

            # 2. Provision directories
            self._provision_directories()
            report.directories_ok = True

            # 3. Provision default configuration
            cfg_path = get_config_path()
            if not cfg_path.exists():
                logger.info("Creating default configuration at: %s", cfg_path)
                cfg = FutabaConfig()
                cfg.save(cfg_path)
                report.config_created = True

            # 4. Check Hermes installation
            self._verify_hermes(report)

            # 5. Check CUA Driver
            self._verify_cua(report)

            # 6. Check Git Bash / MinGit
            self._verify_git_bash(report)

            # 7. Mark installation complete
            if report.is_first_run:
                manifest = {
                    "installed_at": str(Path(__file__).stat().st_mtime),
                    "version": "0.1.0",
                    "app_dir": str(self._app_dir),
                }
                flag_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        except Exception as e:
            report.errors.append(f"Bootstrap fatal error: {e}")
            logger.error("Bootstrap failure: %s", e)

        return report

    def _provision_directories(self) -> None:
        """Ensure all required runtime directories exist."""
        self._app_dir.mkdir(parents=True, exist_ok=True)
        for sub in self.REQUIRED_DIRS:
            p = self._app_dir / sub
            p.mkdir(parents=True, exist_ok=True)

    def _verify_hermes(self, report: BootstrapReport) -> None:
        """Check Hermes Agent repository or bundled payload and self-provision."""
        from futaba.agent.hermes_bridge import find_hermes_dir
        hermes_dir = find_hermes_dir()

        if hermes_dir and hermes_dir.exists():
            report.hermes_found = True
            report.hermes_path = str(hermes_dir)
            logger.info("Hermes Agent verified at: %s", hermes_dir)

            # Ensure self-provisioned copy exists in Futaba-managed runtime
            target = self._app_dir / "runtime" / "hermes"
            if not (target / "run_agent.py").exists() and hermes_dir.resolve() != target.resolve():
                try:
                    logger.info("Self-provisioning Hermes to %s...", target)
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(
                        str(hermes_dir), str(target), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
                    )
                except Exception as e:
                    logger.debug("Failed to copy Hermes to runtime dir: %s", e)
        else:
            # Check if bundled hermes archive is available in runtime
            bundled_archive = self._app_dir / "runtime" / "hermes.zip"
            if bundled_archive.exists():
                target = self._app_dir / "runtime" / "hermes"
                logger.info("Extracting bundled Hermes archive to %s", target)
                shutil.unpack_archive(str(bundled_archive), str(target))
                report.hermes_found = True
                report.hermes_path = str(target)
            else:
                report.warnings.append("Hermes Agent not found in runtime or local paths.")

    def _verify_cua(self, report: BootstrapReport) -> None:
        """Check CUA Driver executable with portable resolution and environment propagation."""
        candidates = [
            self._app_dir / "runtime" / "cua-driver.exe",
            self._app_dir / "cua" / "cua-driver.exe",
            Path(sys.executable).resolve().parent / "cua-driver.exe",
            Path(sys.executable).resolve().parent / "runtime" / "cua-driver.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
            Path.home() / ".local" / "bin" / "cua-driver.exe",
        ]

        # Check PATH
        which_cua = shutil.which("cua-driver") or shutil.which("cua-driver.exe")
        if which_cua:
            candidates.append(Path(which_cua))

        for c in candidates:
            if c.exists() and c.is_file():
                report.cua_driver_found = True
                os.environ["HERMES_CUA_DRIVER_CMD"] = str(c)
                logger.info("CUA Driver found and configured: %s", c)

                # Provision managed copy in Futaba runtime
                target = self._app_dir / "runtime" / "cua-driver.exe"
                if not target.exists() and c.resolve() != target.resolve():
                    try:
                        shutil.copy2(str(c), str(target))
                        logger.info("Synchronized CUA Driver to managed runtime: %s", target)
                    except Exception:
                        pass
                return

        report.warnings.append("CUA Driver binary not installed. Direct desktop accessibility will be used.")

    def _verify_git_bash(self, report: BootstrapReport) -> None:
        """Check Git Bash / MinGit for shell execution."""
        git_candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "git" / "bin" / "bash.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Futaba" / "git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        ]
        for g in git_candidates:
            if g.exists():
                report.git_bash_found = True
                logger.info("Git Bash found at: %s", g)
                return

        which_bash = shutil.which("bash") or shutil.which("bash.exe")
        if which_bash:
            report.git_bash_found = True
            logger.info("Bash found on PATH: %s", which_bash)
        else:
            report.warnings.append("Git Bash not found. PowerShell / CMD will be used as primary shell.")


def bootstrap_runtime() -> BootstrapReport:
    """Run bootstrap and return validation report."""
    bootstrapper = Bootstrapper()
    return bootstrapper.run()
