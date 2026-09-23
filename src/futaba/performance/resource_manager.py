"""
Futaba Performance & Resource Manager.

Monitors system resources and adapts Futaba's behavior:
- CPU usage monitoring
- GPU usage monitoring (CUDA/DirectX)
- RAM tracking
- Gaming mode detection (fullscreen apps, known games, GPU load)
- Adaptive task throttling
- Process priority management
- Power state awareness

The resource manager runs as a background service, periodically sampling
system metrics and adjusting Futaba's behavior accordingly.

Architecture:
    ResourceMonitor (background sampler)
         ↓
    ResourceState (current snapshot)
         ↓
    AdaptivePolicy (decisions)
         ↓
    ThrottleController (enforcement)
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("futaba.performance")

# ---------------------------------------------------------------------------
# Win32 API bindings for resource monitoring
# ---------------------------------------------------------------------------

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False


# ---------------------------------------------------------------------------
# System State
# ---------------------------------------------------------------------------

class SystemLoad(str, enum.Enum):
    """Overall system load level."""
    IDLE = "idle"             # System is mostly idle
    LIGHT = "light"           # Light usage
    MODERATE = "moderate"     # Moderate usage
    HEAVY = "heavy"           # Heavy usage
    CRITICAL = "critical"     # System overloaded
    GAMING = "gaming"         # Gaming detected


@dataclass
class ResourceSnapshot:
    """A point-in-time snapshot of system resources."""
    timestamp: float = 0.0

    # CPU
    cpu_percent: float = 0.0
    cpu_count: int = 0

    # Memory
    ram_total_mb: float = 0.0
    ram_available_mb: float = 0.0
    ram_percent: float = 0.0

    # GPU
    gpu_available: bool = False
    gpu_name: str = ""
    gpu_utilization: float = 0.0
    gpu_memory_used_mb: float = 0.0
    gpu_memory_total_mb: float = 0.0

    # Process
    futaba_cpu_percent: float = 0.0
    futaba_ram_mb: float = 0.0

    # System
    is_on_battery: bool = False
    foreground_process: str = ""
    foreground_window_title: str = ""
    is_fullscreen: bool = False

    # Derived
    load_level: SystemLoad = SystemLoad.IDLE
    is_gaming: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_percent": self.cpu_percent,
            "ram_available_mb": round(self.ram_available_mb),
            "ram_percent": self.ram_percent,
            "gpu_utilization": self.gpu_utilization,
            "load_level": self.load_level.value,
            "is_gaming": self.is_gaming,
            "is_on_battery": self.is_on_battery,
            "foreground_process": self.foreground_process,
        }


# ---------------------------------------------------------------------------
# Known Games Database (expandable)
# ---------------------------------------------------------------------------

KNOWN_GAME_PROCESSES = {
    # These are process name prefixes/patterns
    "steam", "steamwebhelper",
    "minecraft", "javaw",  # Minecraft Java
    "FortniteClient", "FortniteLauncher",
    "csgo", "cs2",
    "valorant", "VALORANT-Win64-Shipping",
    "LeagueofLegends", "LeagueClient",
    "GTA5", "GTAVLauncher",
    "RocketLeague",
    "Overwatch",
    "PUBG", "TslGame",
    "EpicGamesLauncher",
    "Genshin Impact", "GenshinImpact",
    "Cyberpunk2077",
    "eldenring",
    "Hogwarts", "HogwartsLegacy",
    "Destiny2",
    "ApexLegends",
    "DaVinciResolve",  # Not a game but GPU-heavy
    "obs64", "obs32",   # Streaming
    "baldursgate3", "bg3",
}


# ---------------------------------------------------------------------------
# Resource Monitor
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """
    Background system resource monitor.

    Samples system metrics at configurable intervals and maintains
    a current ResourceSnapshot. Detects gaming, heavy load, battery,
    and other conditions that affect Futaba's behavior.
    """

    def __init__(self, sample_interval: float = 5.0):
        self._interval = sample_interval
        self._current = ResourceSnapshot()
        self._running = False
        self._task: asyncio.Task | None = None
        self._listeners: list[Any] = []
        self._process = None

        if HAS_PSUTIL:
            self._process = psutil.Process(os.getpid())

    @property
    def current(self) -> ResourceSnapshot:
        return self._current

    async def start(self) -> None:
        """Start background monitoring."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Resource monitor started (interval=%.1fs)", self._interval)

    async def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Resource monitor stopped")

    def add_listener(self, callback: Any) -> None:
        """Add a listener for resource state changes."""
        self._listeners.append(callback)

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                snapshot = await self._sample()
                old_level = self._current.load_level
                old_gaming = self._current.is_gaming
                self._current = snapshot

                # Notify on significant changes
                if snapshot.load_level != old_level or snapshot.is_gaming != old_gaming:
                    for listener in self._listeners:
                        try:
                            await listener(snapshot)
                        except Exception as e:
                            logger.warning("Resource listener error: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Resource monitoring error: %s", e)

            await asyncio.sleep(self._interval)

    async def _sample(self) -> ResourceSnapshot:
        """Take a resource snapshot."""
        snap = ResourceSnapshot(timestamp=time.time())

        # Run CPU-intensive sampling in executor to not block event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sample_sync, snap)

        # Classify load level
        snap.load_level = self._classify_load(snap)
        snap.is_gaming = self._detect_gaming(snap)

        if snap.is_gaming:
            snap.load_level = SystemLoad.GAMING

        return snap

    def _sample_sync(self, snap: ResourceSnapshot) -> None:
        """Synchronous resource sampling (runs in thread pool)."""

        # CPU
        if HAS_PSUTIL:
            snap.cpu_percent = psutil.cpu_percent(interval=0.1)
            snap.cpu_count = psutil.cpu_count() or 1

            # RAM
            mem = psutil.virtual_memory()
            snap.ram_total_mb = mem.total / (1024 * 1024)
            snap.ram_available_mb = mem.available / (1024 * 1024)
            snap.ram_percent = mem.percent

            # Our process
            if self._process:
                try:
                    snap.futaba_cpu_percent = self._process.cpu_percent(interval=0)
                    snap.futaba_ram_mb = self._process.memory_info().rss / (1024 * 1024)
                except Exception:
                    pass

            # Battery
            try:
                battery = psutil.sensors_battery()
                if battery:
                    snap.is_on_battery = not battery.power_plugged
            except Exception:
                pass

        elif HAS_WIN32:
            # Fallback: Win32 memory info
            mem_status = MEMORYSTATUSEX()
            mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status))
            snap.ram_total_mb = mem_status.ullTotalPhys / (1024 * 1024)
            snap.ram_available_mb = mem_status.ullAvailPhys / (1024 * 1024)
            snap.ram_percent = mem_status.dwMemoryLoad

        # Foreground window
        self._sample_foreground(snap)

        # GPU (best effort)
        self._sample_gpu(snap)

    def _sample_foreground(self, snap: ResourceSnapshot) -> None:
        """Detect the foreground application."""
        if not HAS_WIN32:
            return

        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                # Get window title
                length = user32.GetWindowTextLengthW(hwnd) + 1
                buf = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(hwnd, buf, length)
                snap.foreground_window_title = buf.value

                # Get process name
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

                if HAS_PSUTIL and pid.value:
                    try:
                        proc = psutil.Process(pid.value)
                        snap.foreground_process = proc.name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                # Check fullscreen
                snap.is_fullscreen = self._check_fullscreen(hwnd)

        except Exception as e:
            logger.debug("Foreground detection error: %s", e)

    def _check_fullscreen(self, hwnd: int) -> bool:
        """Check if a window is fullscreen."""
        if not HAS_WIN32:
            return False

        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            # Get window rect
            win_rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(win_rect))

            # Get monitor rect
            monitor = user32.MonitorFromWindow(hwnd, 1)  # MONITOR_DEFAULTTOPRIMARY
            if monitor:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", ctypes.wintypes.DWORD),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", ctypes.wintypes.DWORD),
                    ]

                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                user32.GetMonitorInfoW(monitor, ctypes.byref(mi))

                # Window covers entire monitor = fullscreen
                mon = mi.rcMonitor
                return (
                    win_rect.left <= mon.left
                    and win_rect.top <= mon.top
                    and win_rect.right >= mon.right
                    and win_rect.bottom >= mon.bottom
                )
        except Exception:
            pass
        return False

    def _sample_gpu(self, snap: ResourceSnapshot) -> None:
        """Sample GPU utilization (NVIDIA via nvidia-smi or PyTorch)."""
        try:
            import torch
            if torch.cuda.is_available():
                snap.gpu_available = True
                snap.gpu_name = torch.cuda.get_device_name(0)
                snap.gpu_memory_total_mb = torch.cuda.get_device_properties(0).total_mem / (1024 * 1024)
                # Memory usage
                snap.gpu_memory_used_mb = torch.cuda.memory_allocated(0) / (1024 * 1024)
                # Utilization requires nvidia-smi or pynvml
                self._sample_gpu_utilization_nvml(snap)
        except Exception:
            pass

    def _sample_gpu_utilization_nvml(self, snap: ResourceSnapshot) -> None:
        """Try to get GPU utilization via pynvml."""
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            snap.gpu_utilization = util.gpu
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            snap.gpu_memory_used_mb = mem_info.used / (1024 * 1024)
            snap.gpu_memory_total_mb = mem_info.total / (1024 * 1024)
            pynvml.nvmlShutdown()
        except Exception:
            # Fallback: nvidia-smi CLI
            try:
                import subprocess
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode == 0:
                    parts = result.stdout.strip().split(",")
                    if len(parts) >= 3:
                        snap.gpu_utilization = float(parts[0].strip())
                        snap.gpu_memory_used_mb = float(parts[1].strip())
                        snap.gpu_memory_total_mb = float(parts[2].strip())
            except Exception:
                pass

    def _classify_load(self, snap: ResourceSnapshot) -> SystemLoad:
        """Classify the overall system load."""
        if snap.cpu_percent > 90 or snap.ram_percent > 95:
            return SystemLoad.CRITICAL
        if snap.cpu_percent > 70 or snap.ram_percent > 85 or snap.gpu_utilization > 80:
            return SystemLoad.HEAVY
        if snap.cpu_percent > 40 or snap.ram_percent > 60:
            return SystemLoad.MODERATE
        if snap.cpu_percent > 10:
            return SystemLoad.LIGHT
        return SystemLoad.IDLE

    def _detect_gaming(self, snap: ResourceSnapshot) -> bool:
        """Detect if the user is gaming."""
        # Check 1: Known game process in foreground
        fg = snap.foreground_process.lower().replace(".exe", "")
        if any(game.lower() in fg for game in KNOWN_GAME_PROCESSES):
            return True

        # Check 2: Fullscreen application with high GPU usage
        if snap.is_fullscreen and snap.gpu_utilization > 60:
            return True

        # Check 3: Fullscreen non-browser, non-desktop app
        if snap.is_fullscreen:
            non_game_fullscreen = {
                "explorer", "chrome", "msedge", "firefox",
                "code", "devenv", "powershell", "windowsterminal",
            }
            if fg and fg not in non_game_fullscreen:
                # Fullscreen app not in our known non-game list + high GPU
                if snap.gpu_utilization > 40:
                    return True

        return False


# ---------------------------------------------------------------------------
# Adaptive Throttle Controller
# ---------------------------------------------------------------------------

class ThrottlePolicy:
    """Defines how Futaba should throttle based on system load."""

    @staticmethod
    def get_policy(load: SystemLoad) -> dict[str, Any]:
        """Get throttle parameters for a given load level."""
        policies = {
            SystemLoad.IDLE: {
                "task_concurrency": 3,
                "model_max_tokens": 8192,
                "sample_interval": 10.0,
                "delay_between_steps": 0.0,
                "allow_local_models": True,
                "process_priority": "normal",
                "description": "Full speed",
            },
            SystemLoad.LIGHT: {
                "task_concurrency": 3,
                "model_max_tokens": 8192,
                "sample_interval": 10.0,
                "delay_between_steps": 0.0,
                "allow_local_models": True,
                "process_priority": "normal",
                "description": "Normal operation",
            },
            SystemLoad.MODERATE: {
                "task_concurrency": 2,
                "model_max_tokens": 4096,
                "sample_interval": 15.0,
                "delay_between_steps": 0.5,
                "allow_local_models": True,
                "process_priority": "below_normal",
                "description": "Reduced activity",
            },
            SystemLoad.HEAVY: {
                "task_concurrency": 1,
                "model_max_tokens": 4096,
                "sample_interval": 30.0,
                "delay_between_steps": 2.0,
                "allow_local_models": False,
                "process_priority": "below_normal",
                "description": "Minimal activity",
            },
            SystemLoad.CRITICAL: {
                "task_concurrency": 0,  # Pause non-urgent tasks
                "model_max_tokens": 2048,
                "sample_interval": 60.0,
                "delay_between_steps": 5.0,
                "allow_local_models": False,
                "process_priority": "idle",
                "description": "Paused — system overloaded",
            },
            SystemLoad.GAMING: {
                "task_concurrency": 1,
                "model_max_tokens": 2048,
                "sample_interval": 60.0,
                "delay_between_steps": 5.0,
                "allow_local_models": False,
                "process_priority": "idle",
                "description": "Gaming mode — minimal footprint",
            },
        }
        return policies.get(load, policies[SystemLoad.MODERATE])


class ResourceManager:
    """
    High-level resource manager that ties monitoring to behavior.

    Used by the agent controller to:
    - Check if it's OK to start a new task
    - Adjust processing speed
    - Enable/disable local models
    - Set process priority
    """

    def __init__(self, monitor: ResourceMonitor | None = None):
        self._monitor = monitor or ResourceMonitor()
        self._gaming_mode_forced = False
        self._dnd_mode = False  # Do Not Disturb

    @property
    def monitor(self) -> ResourceMonitor:
        return self._monitor

    @property
    def snapshot(self) -> ResourceSnapshot:
        return self._monitor.current

    @property
    def is_gaming(self) -> bool:
        return self._gaming_mode_forced or self._monitor.current.is_gaming

    @property
    def is_dnd(self) -> bool:
        return self._dnd_mode

    @property
    def policy(self) -> dict[str, Any]:
        if self.is_gaming:
            return ThrottlePolicy.get_policy(SystemLoad.GAMING)
        return ThrottlePolicy.get_policy(self._monitor.current.load_level)

    async def start(self) -> None:
        await self._monitor.start()

    async def stop(self) -> None:
        await self._monitor.stop()

    def force_gaming_mode(self, enabled: bool) -> None:
        self._gaming_mode_forced = enabled
        logger.info("Gaming mode %s", "forced ON" if enabled else "forced OFF")

    def set_dnd(self, enabled: bool) -> None:
        self._dnd_mode = enabled
        logger.info("Do Not Disturb %s", "ON" if enabled else "OFF")

    def should_pause_tasks(self) -> bool:
        """Check if tasks should be paused."""
        return self.policy.get("task_concurrency", 1) == 0

    def get_max_concurrency(self) -> int:
        return self.policy.get("task_concurrency", 1)

    def should_use_local_models(self) -> bool:
        return self.policy.get("allow_local_models", True)

    def get_step_delay(self) -> float:
        return self.policy.get("delay_between_steps", 0.0)

    def set_process_priority(self) -> None:
        """Set Futaba's process priority based on current policy."""
        priority_str = self.policy.get("process_priority", "normal")

        if not HAS_PSUTIL:
            return

        try:
            import psutil
            proc = psutil.Process(os.getpid())
            priority_map = {
                "idle": psutil.IDLE_PRIORITY_CLASS,
                "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                "normal": psutil.NORMAL_PRIORITY_CLASS,
            }
            priority = priority_map.get(priority_str, psutil.NORMAL_PRIORITY_CLASS)
            proc.nice(priority)
        except Exception as e:
            logger.debug("Failed to set process priority: %s", e)
