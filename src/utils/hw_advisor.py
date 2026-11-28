from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Windows NTSTATUS exit code catalog (inspired by RedSyl/Lumen diagnostics)
NTSTATUS_CATALOG: dict[int, tuple[str, str]] = {
    0xC0000005: (
        "STATUS_ACCESS_VIOLATION",
        "Memory access violation in the runtime or GPU driver. Try Safe Mode, disable Ray Tracing, or update GPU drivers.",
    ),
    0xC000001D: (
        "STATUS_ILLEGAL_INSTRUCTION",
        "CPU executed an unsupported instruction (often AVX2 missing on older CPUs).",
    ),
    0xC00000FD: (
        "STATUS_STACK_OVERFLOW",
        "Stack overflow inside the compatibility layer. Try switching profile or disabling experimental mods.",
    ),
    0xC0000017: (
        "STATUS_NO_MEMORY",
        "Out of virtual/commit memory. Enable Windows system-managed pagefile (16+ GB) or use the Ultra-Low RAM / Series S preset.",
    ),
    0xC0000135: (
        "STATUS_DLL_NOT_FOUND",
        "A required DLL is missing (Visual C++ x64 Runtime, D3D12 Agility SDK, or DLSS/FSR SDK).",
    ),
    0xC0000409: (
        "STATUS_STACK_BUFFER_OVERRUN",
        "Runtime abort / fast-fail exception (often assertion or corrupted shader cache). Try clearing runtime cache or Safe Mode.",
    ),
    0x80000003: (
        "STATUS_BREAKPOINT",
        "Debug breakpoint or panic trap hit inside the runtime.",
    ),
    0xE0434352: (
        "CLR_EXCEPTION",
        "Unhandled .NET runtime exception.",
    ),
}

_SKIP_EXE_RE = re.compile(
    r"^(lumen|sixdeck|victory|xcl|neonbridge|setup|unins|vcredist|dxsetup|crash|redist)",
    re.IGNORECASE,
)


def decode_exit_code(code: int) -> dict[str, str]:
    """Decodes an integer process exit code into hex, NTSTATUS name, and actionable hint."""
    u = int(code) & 0xFFFFFFFF
    hex_str = f"0x{u:08X}"
    if u in NTSTATUS_CATALOG:
        name, hint = NTSTATUS_CATALOG[u]
        return {
            "raw": str(code),
            "hex": hex_str,
            "name": name,
            "formatted": f"{code} ({hex_str} {name})",
            "hint": hint,
        }
    if code < 0 or u >= 0x80000000:
        return {
            "raw": str(code),
            "hex": hex_str,
            "name": "",
            "formatted": f"{code} ({hex_str})",
            "hint": "Unexpected native runtime crash. Inspect the last log lines or launch in Safe Mode.",
        }
    return {
        "raw": str(code),
        "hex": str(code),
        "name": "",
        "formatted": str(code),
        "hint": "",
    }


def resolve_gdk_title_exe(
    path_input: str | Path | None,
    *,
    allow_fallback: bool = True,
) -> dict[str, str] | None:
    """Generically detects an unpacked GDK title executable via MicrosoftGame.Config, with safe fallbacks.

    Priority:
    1. Direct `.exe` file path passed by the user.
    2. `MicrosoftGame.Config` XML `<Executable Name="..."/>` (ranking non-DevOnly & Scarlett/Xbox first,
       strictly rejecting path traversal outside the title directory).
    3. Well-known dump executables (`game_scarlett_master.exe`, `eboot.bin`).
    4. Optional largest non-utility `.exe` (>= 1 MB) inside the folder.
    """
    if not path_input:
        return None
    raw = str(path_input).strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None

    if p.is_file():
        if p.suffix.lower() in {".exe", ".bin"}:
            rp = p.resolve()
            return {"folder": str(rp.parent), "exe": str(rp), "source": "selected-exe"}
        return None

    folder = p.resolve()

    # 1. MicrosoftGame.Config (case-insensitive lookup for cross-platform support)
    cfg: Path | None = None
    try:
        for child in folder.iterdir():
            if child.is_file() and child.name.lower() == "microsoftgame.config":
                cfg = child
                break
    except OSError:
        cfg = None

    if cfg is not None:
        try:
            tree = ET.parse(cfg)
            root_el = tree.getroot()
            # Handle optional XML namespaces cleanly by matching local tag name
            nodes = [
                el
                for el in root_el.iter()
                if el.tag.rsplit("}", 1)[-1] == "Executable" and el.attrib.get("Name")
            ]
            nodes.sort(
                key=lambda n: (
                    0 if str(n.attrib.get("IsDevOnly", "")).lower() == "true" else 2,
                    2 if re.search(r"Scarlett|Xbox", str(n.attrib.get("TargetDeviceFamily", "")), re.I) else 1,
                ),
                reverse=True,
            )
            for nd in nodes:
                rel_name = str(nd.attrib.get("Name") or "").strip().replace("\\", "/")
                if not rel_name:
                    continue
                cand = (folder / rel_name).resolve()
                # Strict path containment check (no directory traversal)
                try:
                    cand.relative_to(folder)
                except ValueError:
                    continue
                if cand.is_file():
                    return {"folder": str(folder), "exe": str(cand), "source": "MicrosoftGame.Config"}
        except Exception:
            pass

    # 2. Direct known executables in folder
    for known_name in ("game_scarlett_master.exe", "eboot.bin"):
        cand = folder / known_name
        if cand.is_file():
            return {"folder": str(folder), "exe": str(cand.resolve()), "source": "known-exe"}

    # 3. Fallback to largest non-utility .exe >= 1 MB
    if allow_fallback:
        try:
            cands = [
                f
                for f in folder.iterdir()
                if f.is_file()
                and f.suffix.lower() == ".exe"
                and not _SKIP_EXE_RE.match(f.name)
                and f.stat().st_size >= 1024 * 1024
            ]
            cands.sort(key=lambda f: f.stat().st_size, reverse=True)
            if cands:
                return {"folder": str(folder), "exe": str(cands[0].resolve()), "source": "folder-exe"}
        except OSError:
            pass

    return None


def find_linux_compat_runner() -> dict[str, str] | None:
    """Auto-detects umu-run, Steam Proton, or Wine on Linux/Steam Deck for launching Windows runtimes."""
    if os.name == "nt":
        return None
    env_p = os.environ.get("SIXDECK_PROTON", "") or os.environ.get("SUPERBUILD_PROTON", "")
    env_p = env_p.strip()
    if env_p and Path(env_p).is_file():
        return {"kind": "proton", "path": env_p}
    umu = shutil.which("umu-run")
    if umu:
        return {"kind": "umu", "path": umu}
    home = Path.home()
    for pat in (
        home / ".local/share/Steam/steamapps/common/Proton - Experimental/proton",
        home / ".steam/steam/steamapps/common/Proton - Experimental/proton",
        home / ".local/share/Steam/steamapps/common/Proton 9.0 (Beta)/proton",
        home / ".local/share/Steam/steamapps/common/Proton 9.0/proton",
    ):
        if pat.is_file():
            return {"kind": "proton", "path": str(pat)}
    compat = home / ".local/share/Steam/compatibilitytools.d"
    if compat.is_dir():
        try:
            for child in sorted(compat.iterdir(), reverse=True):
                p = child / "proton"
                if p.is_file():
                    return {"kind": "proton", "path": str(p)}
        except OSError:
            pass
    for w in ("wine64", "wine"):
        wp = shutil.which(w)
        if wp:
            return {"kind": "wine", "path": wp}
    return None


def get_system_memory_cross_platform() -> dict[str, Any]:
    """Reads RAM & virtual/swap/pagefile stats quickly on both Windows and Linux."""
    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                gb = 1024.0 ** 3
                total_ram = round(ms.ullTotalPhys / gb, 1)
                free_ram = round(ms.ullAvailPhys / gb, 1)
                total_commit = round(ms.ullTotalPageFile / gb, 1)
                free_commit = round(ms.ullAvailPageFile / gb, 1)
                pagefile_gb = round(max(0.0, (ms.ullTotalPageFile - ms.ullTotalPhys) / gb), 1)
                low_pagefile = total_ram > 0 and total_ram <= 20.0 and pagefile_gb < 16.0
                return {
                    "total_ram_gb": total_ram,
                    "free_ram_gb": free_ram,
                    "total_commit_gb": total_commit,
                    "free_commit_gb": free_commit,
                    "pagefile_gb": pagefile_gb,
                    "used_pct": int(ms.dwMemoryLoad),
                    "low_memory": free_ram < 5.0 or free_commit < 8.0,
                    "low_pagefile": low_pagefile,
                }
        except Exception:
            pass
        return {
            "total_ram_gb": 0.0,
            "free_ram_gb": 0.0,
            "total_commit_gb": 0.0,
            "free_commit_gb": 0.0,
            "pagefile_gb": 0.0,
            "used_pct": 0,
            "low_memory": False,
            "low_pagefile": False,
        }

    # Linux / POSIX (/proc/meminfo)
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        vals: dict[str, int] = {}
        try:
            for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    vals[parts[0].rstrip(":")] = int(parts[1])
            kb_to_gb = 1024.0 * 1024.0
            total_ram = round(vals.get("MemTotal", 0) / kb_to_gb, 1)
            avail_kb = vals.get("MemAvailable", vals.get("MemFree", 0))
            free_ram = round(avail_kb / kb_to_gb, 1)
            swap_total = round(vals.get("SwapTotal", 0) / kb_to_gb, 1)
            swap_free = round(vals.get("SwapFree", 0) / kb_to_gb, 1)
            total_commit = round(total_ram + swap_total, 1)
            free_commit = round(free_ram + swap_free, 1)
            used_pct = int(round(((total_ram - free_ram) / total_ram) * 100)) if total_ram > 0 else 0
            low_pagefile = total_ram > 0 and total_ram <= 20.0 and swap_total < 16.0
            return {
                "total_ram_gb": total_ram,
                "free_ram_gb": free_ram,
                "total_commit_gb": total_commit,
                "free_commit_gb": free_commit,
                "pagefile_gb": swap_total,
                "used_pct": max(0, min(100, used_pct)),
                "low_memory": (total_ram > 0 and free_ram < 5.0) or (total_commit > 0 and free_commit < 8.0),
                "low_pagefile": low_pagefile,
            }
        except OSError:
            pass

    return {
        "total_ram_gb": 0.0,
        "free_ram_gb": 0.0,
        "total_commit_gb": 0.0,
        "free_commit_gb": 0.0,
        "pagefile_gb": 0.0,
        "used_pct": 0,
        "low_memory": False,
        "low_pagefile": False,
    }


def detect_avx2() -> bool:
    """Detects CPU AVX2 instruction set support without external dependencies."""
    if os.name == "nt":
        try:
            # PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        except Exception:
            return True
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            tokens = cpuinfo.read_text(encoding="utf-8", errors="ignore").lower().split()
            return "avx2" in tokens
        except OSError:
            return True
    return True


def is_process_elevated() -> bool:
    """Returns True if running as Administrator (Windows) or root (Linux)."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _detect_gpus() -> list[dict[str, str]]:
    """Detects primary and secondary GPUs (vendor + name) cleanly without CMD popups."""
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    gpus: list[dict[str, str]] = []

    if os.name == "nt":
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=no_win,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json

                raw = json.loads(proc.stdout.strip())
                items = raw if isinstance(raw, list) else [raw]
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("Name") or "").strip()
                    pnp = str(it.get("PNPDeviceID") or "").upper()
                    if not name:
                        continue
                    vendor = "unknown"
                    if "VEN_10DE" in pnp or "nvidia" in name.lower():
                        vendor = "nvidia"
                    elif "VEN_1002" in pnp or any(k in name.lower() for k in ("amd", "radeon", "ati")):
                        vendor = "amd"
                    elif "VEN_8086" in pnp or "intel" in name.lower():
                        vendor = "intel"
                    if vendor != "unknown":
                        gpus.append({"name": name, "vendor": vendor, "pnp": pnp})
        except Exception:
            pass
    else:
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).decode("utf-8", errors="ignore").strip().splitlines()
                for ln in out:
                    if ln.strip():
                        gpus.append({"name": ln.strip(), "vendor": "nvidia", "pnp": ""})
            except Exception:
                pass
        if not gpus and shutil.which("lspci"):
            try:
                out = subprocess.check_output(["lspci"], stderr=subprocess.DEVNULL, timeout=3).decode("utf-8", errors="ignore")
                for ln in out.splitlines():
                    if any(k in ln for k in ("VGA compatible controller", "3D controller", "Display controller")):
                        desc = ln.split(":", 2)[-1].strip()
                        low = desc.lower()
                        if "nvidia" in low:
                            gpus.append({"name": desc, "vendor": "nvidia", "pnp": ""})
                        elif any(k in low for k in ("amd", "ati", "radeon")):
                            gpus.append({"name": desc, "vendor": "amd", "pnp": ""})
                        elif "intel" in low:
                            gpus.append({"name": desc, "vendor": "intel", "pnp": ""})
            except Exception:
                pass

    # Prefer discrete GPU first (NVIDIA / AMD RX / Intel Arc > iGPU)
    def _gpu_rank(g: dict[str, str]) -> int:
        v = g["vendor"]
        n = g["name"]
        if v == "nvidia":
            return 3
        if v == "amd":
            if re.search(r"\b(RX|PRO|VII|R9|FURY|VEGA\s*(56|64)|W\d{4}|MI\d+)\b", n, re.I):
                return 3
            if re.search(r"^AMD Radeon(\(TM\))? Graphics$|Radeon(\(TM\))?\s+(Vega|R[2-7]|[678]\d0M)", n, re.I):
                return 1
            return 2
        if v == "intel":
            return 3 if re.search(r"\bArc\s+[AB]\d+", n, re.I) else 0
        return 0

    gpus.sort(key=_gpu_rank, reverse=True)
    return gpus


def detect_hardware_profile() -> dict[str, Any]:
    """Builds a comprehensive, fast hardware diagnostics profile & actionable warnings."""
    mem = get_system_memory_cross_platform()
    gpus = _detect_gpus()
    primary = gpus[0] if gpus else {"name": "Unknown GPU", "vendor": "unknown"}
    gpu_name = primary["name"]
    vendor = primary["vendor"]
    vendor_map = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel"}
    vendor_name = vendor_map.get(vendor, "Unknown")
    older_rtx = vendor == "nvidia" and bool(re.search(r"\bRTX\s*(20|30)\d{2}\b", gpu_name, re.I))
    avx2 = detect_avx2()
    is_admin = is_process_elevated()
    multi_gpu = len(gpus) >= 2
    ram_gb = float(mem.get("total_ram_gb") or 0.0)
    pagefile_gb = float(mem.get("pagefile_gb") or 0.0)
    low_pagefile = bool(mem.get("low_pagefile", False))
    proton = find_linux_compat_runner()

    hw_sig = f"{gpu_name}|{round(ram_gb / 8) if ram_gb > 0 else 0}"
    hw_id = "hw" + hashlib.sha1(hw_sig.encode("utf-8")).hexdigest()[:10]

    warnings: list[dict[str, str]] = []
    if is_admin:
        warnings.append(
            {
                "id": "admin_elevation",
                "severity": "warn",
                "title": "Running as Administrator / Root",
                "message": (
                    "NVIDIA separates elevated and normal user shader caches. Reopen SixDeck as a standard user "
                    "so precompiled shaders are shared and reused."
                ),
            }
        )
    if not avx2:
        warnings.append(
            {
                "id": "no_avx2",
                "severity": "error",
                "title": "CPU Lacks AVX2",
                "message": "Modern Xbox Series (GDK) compatibility layers require a CPU with AVX2 instruction support.",
            }
        )
    if low_pagefile:
        warnings.append(
            {
                "id": "low_pagefile",
                "severity": "warn",
                "title": f"Low Virtual Memory ({pagefile_gb:.1f} GB on {ram_gb:.0f} GB RAM)",
                "message": (
                    "Large GDK titles can commit 30+ GB of virtual memory. Enable Windows system-managed pagefile "
                    "(or 16+ GB swap on Linux) to prevent STATUS_NO_MEMORY crashes."
                ),
            }
        )

    recommended_preset = (
        "superbuild-16gb-fast"
        if (ram_gb > 0 and ram_gb <= 20.0)
        else ("nvidia-balanced-1080p" if vendor == "nvidia" else ("amd-radeon-1080p" if vendor == "amd" else "superbuild-series-x"))
    )

    return {
        "os": platform.system(),
        "release": platform.release(),
        "hw_id": hw_id,
        "gpu_name": gpu_name,
        "gpu_vendor": vendor,
        "gpu_vendor_name": vendor_name,
        "older_rtx": older_rtx,
        "multi_gpu": multi_gpu,
        "gpu_count": len(gpus),
        "ram_gb": ram_gb,
        "pagefile_gb": pagefile_gb,
        "low_pagefile": low_pagefile,
        "avx2": avx2,
        "is_admin": is_admin,
        "proton_runner": proton,
        "recommended_preset": recommended_preset,
        "warnings": warnings,
    }


def score_runtime_for_hardware(
    runtime_name: str,
    *,
    hw: dict[str, Any],
    pw_entry: dict[str, Any] | None = None,
    local_record: dict[str, Any] | None = None,
    installed: bool = False,
    supports_low_ram: bool = False,
    min_ram_gb: float = 0.0,
) -> dict[str, Any]:
    """Objectively scores a compatibility runtime for the current PC (no artificial per-runner boost).

    Combines live PortWatch VI progress/FPS/stability, GPU vendor match, RAM fit, and local measured history.
    """
    score = 0.0
    why: list[str] = []

    vendor_name = str(hw.get("gpu_vendor_name") or "Unknown")
    ram_gb = float(hw.get("ram_gb") or 0.0)

    if pw_entry:
        pw_pct = float(pw_entry.get("score_pct") or 0.0)
        fps = float(pw_entry.get("fps") or 0.0)
        base_10 = round(pw_pct / 10.0, 1)
        score += base_10
        if fps > 0:
            why.append(f"PortWatch {pw_pct:.0f}% ({fps:.0f} FPS target)")
        else:
            why.append(f"PortWatch {pw_pct:.0f}%")

        gpus = [str(g).upper() for g in (pw_entry.get("gpu") or []) if g]
        if gpus and vendor_name != "Unknown":
            if vendor_name.upper() in gpus:
                score += 2.0
                why.append(f"supported on {vendor_name}")
            else:
                score -= 5.0
                why.append(f"listed for {'/'.join(gpus)} only")
    else:
        if installed:
            score += 4.0
            why.append("locally compiled runtime")

    if ram_gb > 0 and ram_gb <= 20.0:
        if supports_low_ram:
            score += 1.0
            why.append(f"low-RAM mode for {ram_gb:.0f} GB")
        elif min_ram_gb > 0 and ram_gb < min_ram_gb:
            score -= 2.5
            why.append(f"prefers {int(min_ram_gb)}+ GB RAM")

    if local_record:
        loc_fps = int(local_record.get("fps") or 0)
        crashed = bool(local_record.get("crashed", False))
        if not crashed and loc_fps >= 15:
            bonus = min(3.0, 1.0 + (loc_fps / 30.0))
            score += bonus
            why.append(f"verified locally ({loc_fps} FPS)")
        elif crashed:
            score -= 1.5
            why.append("crashed on last local run")

    if installed:
        score += 0.5
        why.append("ready on disk")

    return {
        "runtime": runtime_name,
        "score": round(score, 1),
        "why": ", ".join(why) if why else "available runtime",
        "installed": installed,
    }
