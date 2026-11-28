from __future__ import annotations

import ctypes
import gc
import os
import re
import subprocess
from pathlib import Path
from typing import Any

HIGH_PERF_POWER_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
POWER_GUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def is_laptop_on_battery() -> bool:
    """Returns True when running on battery power (prevents forcing High Performance power plan on laptops)."""
    if os.name == "nt":
        try:
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            st = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                # ACLineStatus: 0 = Offline (battery), 1 = Online (AC), 255 = Unknown
                return st.ACLineStatus == 0 and st.BatteryFlag != 128
        except Exception:
            return False
        return False

    # Linux /sys/class/power_supply check
    ps_dir = Path("/sys/class/power_supply")
    if ps_dir.is_dir():
        try:
            for child in ps_dir.iterdir():
                status_file = child / "status"
                if status_file.is_file() and status_file.read_text(encoding="utf-8", errors="ignore").strip().lower() == "discharging":
                    return True
        except OSError:
            pass
    return False


def set_windows_gpu_preference(exe_path: str | Path, *, multi_gpu: bool = True) -> bool:
    """Sets Windows DirectX UserGpuPreferences to High-Performance GPU (GpuPreference=2;) on hybrid/multi-GPU PCs."""
    if os.name != "nt" or not multi_gpu:
        return False
    p = Path(str(exe_path))
    if not p.is_file():
        return False
    try:
        import winreg  # type: ignore[import-not-found]

        key_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, str(p.resolve()), 0, winreg.REG_SZ, "GpuPreference=2;")
        return True
    except Exception:
        return False


def wrap_command_for_linux_compat(
    cmd: list[str],
    env: dict[str, str],
    *,
    compat_runner: dict[str, str] | None,
    compat_home: Path,
) -> tuple[list[str], dict[str, str], str]:
    """Wraps a Windows .exe command via umu-run, Proton, or Wine on Linux/Steam Deck with DX12/NVAPI env vars."""
    if os.name == "nt" or not cmd or not cmd[0].lower().endswith(".exe"):
        return cmd, env, "native"
    if not compat_runner or not compat_runner.get("path"):
        return cmd, env, "missing_proton"

    out_env = dict(env)
    out_env.setdefault("PROTON_ENABLE_NVAPI", "1")
    out_env.setdefault("DXVK_ENABLE_NVAPI", "1")
    out_env.setdefault("VKD3D_CONFIG", "dxr11")
    out_env.setdefault("VKD3D_FEATURE_LEVEL", "12_2")

    kind = str(compat_runner.get("kind") or "wine")
    runner_bin = str(compat_runner["path"])
    if kind == "umu":
        return [runner_bin, *cmd], out_env, "umu"
    if kind == "proton":
        prefix = compat_home / "proton-prefix"
        prefix.mkdir(parents=True, exist_ok=True)
        out_env.setdefault("STEAM_COMPAT_DATA_PATH", str(prefix))
        out_env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(Path.home() / ".local/share/Steam"))
        return [runner_bin, "run", *cmd], out_env, "proton"
    prefix = compat_home / "wine-prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    out_env.setdefault("WINEPREFIX", str(prefix))
    return [runner_bin, *cmd], out_env, "wine"


def compute_affinity_mask(logical_cpus: int, mode: str) -> list[int]:
    """Computes target CPU core indices for hybrid Intel P-Cores or AMD Ryzen X3D CCD0 pinning."""
    n = max(1, int(logical_cpus or 1))
    m = (mode or "auto").strip().lower()
    if m in {"all", "auto", "off", ""}:
        return list(range(n))
    if m == "p_cores":
        # On hybrid Intel CPUs (e.g. 12/16/20/24/32 logical threads), P-Cores occupy the first half
        if n >= 8:
            count = max(4, n // 2)
            return list(range(count))
        return list(range(n))
    if m == "ccd0":
        # On dual-CCD AMD Ryzen 9 X3D CPUs (16/24/32 threads), 3D V-Cache sits on CCD0 (first half)
        if n >= 16:
            return list(range(n // 2))
        return list(range(n))
    return list(range(n))


def cores_to_bitmask(cores: list[int]) -> int:
    mask = 0
    for c in cores:
        if 0 <= c < 64:
            mask |= 1 << c
    return mask or 1


def get_active_windows_power_plan() -> str:
    if os.name != "nt":
        return ""
    try:
        proc = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        m = POWER_GUID_RE.search(proc.stdout or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def set_windows_power_plan(guid: str) -> bool:
    if os.name != "nt" or not guid:
        return False
    try:
        proc = subprocess.run(
            ["powercfg", "/setactive", guid],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False


def trim_process_memory() -> dict[str, Any]:
    """Runs Python GC and trims SixDeck's own OS working set before launching a heavy 3D runtime."""
    collected = gc.collect()
    os_trimmed = False
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            hproc = k32.GetCurrentProcess()
            os_trimmed = bool(k32.SetProcessWorkingSetSize(hproc, ctypes.c_size_t(-1), ctypes.c_size_t(-1)))
        except Exception:
            os_trimmed = False
    return {"gc_collected": collected, "os_working_set_trimmed": os_trimmed}


def apply_runtime_process_tuning(
    pid: int,
    *,
    priority: str = "normal",
    affinity_mode: str = "auto",
) -> dict[str, Any]:
    """Applies CPU priority and P-Core / CCD0 affinity to a launched runtime process safely."""
    prio = (priority or "normal").strip().lower()
    aff = (affinity_mode or "auto").strip().lower()
    logical = os.cpu_count() or 1
    target_cores = compute_affinity_mask(logical, aff)
    prio_applied = False
    aff_applied = False

    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            PROCESS_SET_INFORMATION = 0x0200
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, int(pid))
            if handle:
                try:
                    prio_map = {
                        "above_normal": 0x00008000,
                        "high": 0x00000080,
                    }
                    if prio in prio_map:
                        prio_applied = bool(k32.SetPriorityClass(handle, prio_map[prio]))
                    if aff in {"p_cores", "ccd0"} and len(target_cores) < logical:
                        mask = cores_to_bitmask(target_cores)
                        aff_applied = bool(k32.SetProcessAffinityMask(handle, ctypes.c_size_t(mask)))
                finally:
                    k32.CloseHandle(handle)
        except Exception:
            pass
    else:
        if prio in {"above_normal", "high"}:
            try:
                nice_val = -5 if prio == "above_normal" else -10
                os.setpriority(os.PRIO_PROCESS, int(pid), nice_val)
                prio_applied = True
            except OSError:
                prio_applied = False
        if aff in {"p_cores", "ccd0"} and hasattr(os, "sched_setaffinity") and len(target_cores) < logical:
            try:
                os.sched_setaffinity(int(pid), set(target_cores))
                aff_applied = True
            except OSError:
                aff_applied = False

    return {
        "pid": pid,
        "priority": prio,
        "priority_applied": prio_applied,
        "affinity_mode": aff,
        "target_cores": target_cores,
        "affinity_applied": aff_applied,
    }


def prepare_system_for_launch(
    *,
    high_perf_power: bool = False,
    trim_memory: bool = False,
    low_memory: bool = False,
) -> dict[str, Any]:
    """Executes opt-in pre-launch system optimizations and returns restoration context."""
    prev_power = ""
    power_switched = False
    power_skipped_battery = False
    if high_perf_power and os.name == "nt":
        if is_laptop_on_battery():
            power_skipped_battery = True
        else:
            prev_power = get_active_windows_power_plan()
            if prev_power and prev_power.lower() != HIGH_PERF_POWER_GUID:
                power_switched = set_windows_power_plan(HIGH_PERF_POWER_GUID)

    mem_info = None
    if trim_memory or low_memory:
        mem_info = trim_process_memory()

    return {
        "previous_power_guid": prev_power,
        "power_switched": power_switched,
        "power_skipped_battery": power_skipped_battery,
        "memory_trimmed": mem_info,
    }


def restore_system_after_launch(ctx: dict[str, Any] | None) -> bool:
    """Restores the user's previous Windows power plan once the runtime exits."""
    if not ctx or not ctx.get("power_switched"):
        return False
    prev = str(ctx.get("previous_power_guid") or "").strip()
    if prev:
        return set_windows_power_plan(prev)
    return False
