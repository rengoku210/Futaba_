"""
Futaba Tool Registry — Unified tool execution framework.

All tools that the agent can use are registered here. Tools provide
capabilities like:
- Terminal execution (PowerShell, CMD, Python)
- Browser automation
- Computer use (CUA Driver)
- File system operations
- Application launching
- Web search
- Vision/screenshot analysis

The registry provides:
- Tool discovery and registration
- Unified execution interface
- Timeout management
- Permission checking
- Tool capability introspection

When Hermes integration is active, Hermes tools are bridged through
this registry so the agent controller has a single interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("futaba.tools")


# ---------------------------------------------------------------------------
# Tool Result
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Result of a tool execution."""
    success: bool
    output: Any = None       # Structured output
    error: str = ""
    duration_seconds: float = 0.0
    artifacts: list[str] = field(default_factory=list)  # File paths created
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output": str(self.output) if self.output is not None else None,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "artifacts": self.artifacts,
        }


# ---------------------------------------------------------------------------
# Tool Base Class
# ---------------------------------------------------------------------------

class Tool(ABC):
    """
    Base class for all Futaba tools.

    Each tool provides a set of actions that the agent can invoke.
    Tools handle their own timeout, error handling, and cleanup.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description."""
        ...

    @property
    def actions(self) -> list[str]:
        """List of available actions."""
        return []

    @property
    def capabilities(self) -> list[str]:
        """Capabilities this tool provides (for tool selection)."""
        return []

    @abstractmethod
    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        """Execute an action with the given parameters."""
        ...

    async def health_check(self) -> bool:
        """Check if the tool is operational."""
        return True

    def get_schema(self) -> dict[str, Any]:
        """Get the OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "The action to perform",
                            "enum": self.actions,
                        },
                        "parameters": {
                            "type": "object",
                            "description": "Action-specific parameters",
                        },
                    },
                    "required": ["action"],
                },
            },
        }


# ---------------------------------------------------------------------------
# Terminal Tool
# ---------------------------------------------------------------------------

class TerminalTool(Tool):
    """Execute commands in PowerShell, CMD, or other shells."""

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return "Execute shell commands (PowerShell, CMD, Python, etc.)"

    @property
    def actions(self) -> list[str]:
        return [
            "run_powershell", "run_cmd", "run_python",
            "run_command", "get_output", "list_processes",
        ]

    @property
    def capabilities(self) -> list[str]:
        return ["shell", "command_execution", "process_management"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()

        try:
            if action in ("run_powershell", "run_command"):
                return await self._run_shell(
                    parameters.get("command", ""),
                    shell="powershell",
                    cwd=parameters.get("cwd"),
                    timeout=parameters.get("timeout", 120),
                )
            elif action == "run_cmd":
                return await self._run_shell(
                    parameters.get("command", ""),
                    shell="cmd",
                    cwd=parameters.get("cwd"),
                    timeout=parameters.get("timeout", 120),
                )
            elif action == "run_python":
                return await self._run_python(
                    parameters.get("code", ""),
                    parameters.get("script_path", ""),
                    cwd=parameters.get("cwd"),
                    timeout=parameters.get("timeout", 120),
                )
            elif action == "list_processes":
                return await self._list_processes(parameters.get("filter", ""))
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                duration_seconds=time.monotonic() - start,
            )

    async def _run_shell(
        self,
        command: str,
        shell: str = "powershell",
        cwd: str | None = None,
        timeout: int = 120,
    ) -> ToolResult:
        """Execute a shell command and capture output."""
        if not command:
            return ToolResult(success=False, error="No command provided")

        start = time.monotonic()

        if shell == "powershell":
            args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            args = ["cmd", "/c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    success=False,
                    error=f"Command timed out after {timeout}s",
                    duration_seconds=time.monotonic() - start,
                )

            elapsed = time.monotonic() - start
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return ToolResult(
                    success=True,
                    output=stdout_text,
                    duration_seconds=elapsed,
                    metadata={"return_code": proc.returncode, "stderr": stderr_text},
                )
            else:
                return ToolResult(
                    success=False,
                    output=stdout_text,
                    error=stderr_text or f"Exit code: {proc.returncode}",
                    duration_seconds=elapsed,
                    metadata={"return_code": proc.returncode},
                )

        except FileNotFoundError:
            return ToolResult(
                success=False,
                error=f"Shell not found: {shell}",
                duration_seconds=time.monotonic() - start,
            )

    async def _run_python(
        self,
        code: str = "",
        script_path: str = "",
        cwd: str | None = None,
        timeout: int = 120,
    ) -> ToolResult:
        """Execute Python code or a script."""
        if code:
            return await self._run_shell(
                f'python -c "{code}"',
                shell="powershell",
                cwd=cwd,
                timeout=timeout,
            )
        elif script_path:
            return await self._run_shell(
                f"python {script_path}",
                shell="powershell",
                cwd=cwd,
                timeout=timeout,
            )
        return ToolResult(success=False, error="No code or script path provided")

    async def _list_processes(self, filter_str: str = "") -> ToolResult:
        """List running processes."""
        cmd = "Get-Process"
        if filter_str:
            cmd += f" | Where-Object {{$_.ProcessName -like '*{filter_str}*'}}"
        cmd += " | Select-Object ProcessName, Id, CPU, WorkingSet64 | ConvertTo-Json"
        return await self._run_shell(cmd)


# ---------------------------------------------------------------------------
# Filesystem Tool
# ---------------------------------------------------------------------------

class FilesystemTool(Tool):
    """File and directory operations."""

    @property
    def name(self) -> str:
        return "filesystem"

    @property
    def description(self) -> str:
        return "Create, read, modify, search, and manage files and directories"

    @property
    def actions(self) -> list[str]:
        return [
            "read_file", "write_file", "append_file", "delete_file",
            "list_directory", "create_directory", "move", "copy",
            "search_files", "file_info", "find_files",
        ]

    @property
    def capabilities(self) -> list[str]:
        return ["file_read", "file_write", "file_search", "directory_management"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()

        try:
            if action == "read_file":
                return await self._read_file(parameters.get("path", ""))
            elif action == "write_file":
                return await self._write_file(
                    parameters.get("path", ""),
                    parameters.get("content", ""),
                    parameters.get("overwrite", False),
                )
            elif action == "append_file":
                return await self._append_file(
                    parameters.get("path", ""),
                    parameters.get("content", ""),
                )
            elif action == "list_directory":
                return await self._list_dir(
                    parameters.get("path", "."),
                    parameters.get("recursive", False),
                )
            elif action == "create_directory":
                return await self._create_dir(parameters.get("path", ""))
            elif action == "delete_file":
                return await self._delete(parameters.get("path", ""))
            elif action == "move":
                return await self._move(
                    parameters.get("source", ""),
                    parameters.get("destination", ""),
                )
            elif action == "copy":
                return await self._copy(
                    parameters.get("source", ""),
                    parameters.get("destination", ""),
                )
            elif action == "search_files":
                return await self._search(
                    parameters.get("directory", "."),
                    parameters.get("pattern", "*"),
                    parameters.get("content_pattern", ""),
                )
            elif action == "file_info":
                return await self._file_info(parameters.get("path", ""))
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                duration_seconds=time.monotonic() - start,
            )

    async def _read_file(self, path: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=False, error=f"File not found: {path}")
        try:
            content = p.read_text(encoding="utf-8")
            return ToolResult(success=True, output=content)
        except UnicodeDecodeError:
            content = p.read_bytes()
            return ToolResult(
                success=True,
                output=f"[Binary file, {len(content)} bytes]",
                metadata={"binary": True, "size": len(content)},
            )

    async def _write_file(
        self, path: str, content: str, overwrite: bool = False
    ) -> ToolResult:
        p = Path(path)
        if p.exists() and not overwrite:
            return ToolResult(
                success=False,
                error=f"File exists: {path}. Set overwrite=true to replace.",
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"Written {len(content)} bytes to {path}",
            artifacts=[str(p.absolute())],
        )

    async def _append_file(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(success=True, output=f"Appended {len(content)} bytes to {path}")

    async def _list_dir(self, path: str, recursive: bool = False) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=False, error=f"Directory not found: {path}")
        if not p.is_dir():
            return ToolResult(success=False, error=f"Not a directory: {path}")

        entries = []
        iterator = p.rglob("*") if recursive else p.iterdir()
        for item in iterator:
            try:
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                })
            except PermissionError:
                continue

            if len(entries) > 500:
                break

        return ToolResult(
            success=True,
            output=json.dumps(entries, indent=2),
            metadata={"count": len(entries)},
        )

    async def _create_dir(self, path: str) -> ToolResult:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult(success=True, output=f"Created directory: {path}")

    async def _delete(self, path: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=True, output=f"Already deleted: {path}")
        if p.is_file():
            p.unlink()
            return ToolResult(success=True, output=f"Deleted file: {path}")
        elif p.is_dir():
            import shutil
            shutil.rmtree(p)
            return ToolResult(success=True, output=f"Deleted directory: {path}")
        return ToolResult(success=False, error=f"Cannot delete: {path}")

    async def _move(self, source: str, destination: str) -> ToolResult:
        import shutil
        src = Path(source)
        if not src.exists():
            return ToolResult(success=False, error=f"Source not found: {source}")
        dst = Path(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ToolResult(success=True, output=f"Moved {source} → {destination}")

    async def _copy(self, source: str, destination: str) -> ToolResult:
        import shutil
        src = Path(source)
        if not src.exists():
            return ToolResult(success=False, error=f"Source not found: {source}")
        dst = Path(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(str(src), str(dst))
        else:
            shutil.copytree(str(src), str(dst))
        return ToolResult(success=True, output=f"Copied {source} → {destination}")

    async def _search(
        self, directory: str, pattern: str, content_pattern: str = ""
    ) -> ToolResult:
        p = Path(directory)
        if not p.exists():
            return ToolResult(success=False, error=f"Directory not found: {directory}")

        matches = []
        for item in p.rglob(pattern):
            if content_pattern and item.is_file():
                try:
                    text = item.read_text(encoding="utf-8", errors="ignore")
                    if content_pattern.lower() not in text.lower():
                        continue
                except Exception:
                    continue
            matches.append(str(item))
            if len(matches) > 100:
                break

        return ToolResult(
            success=True,
            output=json.dumps(matches),
            metadata={"count": len(matches)},
        )

    async def _file_info(self, path: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=False, error=f"Not found: {path}")
        stat = p.stat()
        info = {
            "path": str(p.absolute()),
            "name": p.name,
            "type": "directory" if p.is_dir() else "file",
            "size": stat.st_size,
            "modified": time.ctime(stat.st_mtime),
            "created": time.ctime(stat.st_ctime),
            "extension": p.suffix,
        }
        return ToolResult(success=True, output=json.dumps(info, indent=2))


# ---------------------------------------------------------------------------
# Application Tool
# ---------------------------------------------------------------------------

class ApplicationTool(Tool):
    """Launch and manage Windows applications."""

    @property
    def name(self) -> str:
        return "application"

    @property
    def description(self) -> str:
        return "Discover, launch, and manage Windows applications"

    @property
    def actions(self) -> list[str]:
        return ["launch", "discover", "close", "focus", "list_running"]

    @property
    def capabilities(self) -> list[str]:
        return ["app_launch", "app_management", "app_discovery"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        name = parameters.get("name") or parameters.get("app_name") or parameters.get("app") or ""
        if action == "launch":
            return await self._launch(
                name,
                parameters.get("path", ""),
                parameters.get("args", ""),
            )
        elif action == "discover":
            return await self._discover(parameters.get("query", ""))
        elif action in ("close", "kill"):
            return await self._close(name)
        elif action == "list_running":
            return await self._list_running()
        return ToolResult(success=False, error=f"Unknown action: {action}")

    async def _launch(
        self, name: str = "", path: str = "", args: str = ""
    ) -> ToolResult:
        """Launch an application by name or path."""
        if path:
            try:
                proc = await asyncio.create_subprocess_exec(
                    path, *args.split() if args else [],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                return ToolResult(
                    success=True,
                    output=f"Launched {path} (PID: {proc.pid})",
                    metadata={"pid": proc.pid},
                )
            except Exception as e:
                return ToolResult(success=False, error=f"Failed to launch {path}: {e}")

        if name:
            try:
                from futaba.system.app_resolver import get_app_resolver
                resolver = get_app_resolver()
                res = resolver.open_or_focus(name)
                if res.success:
                    return ToolResult(
                        success=True,
                        output=res.message,
                        metadata=res.to_dict(),
                    )
                else:
                    return ToolResult(
                        success=False,
                        error=res.message,
                        metadata=res.to_dict(),
                    )
            except Exception as e:
                logger.debug("AppResolver in ApplicationTool error: %s, falling back to powershell", e)

            # Fallback to Windows Start-Process
            terminal = TerminalTool()
            result = await terminal.execute(
                "run_powershell",
                {"command": f"Start-Process '{name}' -PassThru | Select-Object Id | ConvertTo-Json"},
            )
            if result.success:
                return ToolResult(
                    success=True,
                    output=f"Launched {name}",
                    metadata=result.metadata,
                )
            return result

        return ToolResult(success=False, error="No application name or path provided")

    async def _discover(self, query: str) -> ToolResult:
        """Discover installed applications matching a query."""
        terminal = TerminalTool()
        # Search Start Menu shortcuts and registry
        cmd = f"""
$apps = @()
$paths = @(
    "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
    "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs"
)
foreach ($p in $paths) {{
    if (Test-Path $p) {{
        Get-ChildItem -Path $p -Recurse -Filter "*.lnk" |
        Where-Object {{ $_.Name -like "*{query}*" }} |
        ForEach-Object {{
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($_.FullName)
            $apps += @{{
                Name = $_.BaseName
                Path = $shortcut.TargetPath
                Arguments = $shortcut.Arguments
            }}
        }}
    }}
}}
$apps | ConvertTo-Json -Depth 3
"""
        return await terminal.execute("run_powershell", {"command": cmd, "timeout": 30})

    async def _close(self, name: str) -> ToolResult:
        terminal = TerminalTool()
        clean_name = name[:-4] if name.lower().endswith(".exe") else name
        return await terminal.execute(
            "run_powershell",
            {"command": f"Stop-Process -Name '{clean_name}' -Force -ErrorAction SilentlyContinue; Write-Output 'Closed {name}'"},
        )

    async def _list_running(self) -> ToolResult:
        terminal = TerminalTool()
        return await terminal.execute(
            "run_powershell",
            {"command": "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object ProcessName, Id, MainWindowTitle | ConvertTo-Json"},
        )


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Registry and dispatcher for all tools.

    Provides:
    - Tool registration
    - Unified execution interface with timeouts
    - Tool discovery for the agent
    - Health checking
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._default_timeout = 120  # seconds

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict[str, Any]]:
        """Get OpenAI function-calling schemas for all tools."""
        return [tool.get_schema() for tool in self._tools.values()]

    async def execute(
        self,
        tool_name: str,
        action: str,
        parameters: dict[str, Any],
        timeout: int | None = None,
    ) -> ToolResult:
        """
        Execute a tool action with timeout protection.
        """
        from futaba.agent.capabilities import get_capability_registry
        cap_registry = get_capability_registry()
        tool_name, action, parameters = cap_registry.canonicalize_step(tool_name, action, parameters)

        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}. Available: {list(self._tools.keys())}",
            )

        effective_timeout = timeout or parameters.pop("timeout", self._default_timeout)
        start = time.monotonic()

        try:
            result = await asyncio.wait_for(
                tool.execute(action, parameters),
                timeout=effective_timeout,
            )
            result.duration_seconds = time.monotonic() - start
            return result

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Tool {tool_name}.{action} timed out after {effective_timeout}s",
                duration_seconds=time.monotonic() - start,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Tool {tool_name}.{action} failed: {e}",
                duration_seconds=time.monotonic() - start,
            )

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks on all tools."""
        results = {}
        for name, tool in self._tools.items():
            try:
                results[name] = await tool.health_check()
            except Exception:
                results[name] = False
        return results

    @classmethod
    def create_default(cls) -> "ToolRegistry":
        """Create a registry with all built-in tools."""
        registry = cls()
        registry.register(TerminalTool())
        registry.register(FilesystemTool())
        registry.register(ApplicationTool())
        return registry
