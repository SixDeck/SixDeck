from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Auto-bascule sur le Python du .venv si gui.py est double-cliqué directement dans l'Explorateur Windows
_VENV_PY = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
if (
    __name__ == "__main__"
    and _VENV_PY.exists()
    and Path(sys.executable).resolve() != _VENV_PY.resolve()
):
    raise SystemExit(subprocess.call([str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]]))

import asyncio
import json
import re
import shutil
import threading
import traceback
import webbrowser
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from src.config import AppConfig, load_config
from src.filters.relevance import RelevanceFilter
from src.models import ScrapedItem
from src.notifiers.alerts import is_milestone_item, send_windows_toast
from src.notifiers.markdown_export import update_daily_markdown_report
from src.sandbox.vm_downloader import (
    detect_available_backends,
    generate_inspection_sandbox_wsb,
    generate_windows_sandbox_files,
    list_local_drives,
    list_quarantine_files,
    quarantine_download,
)
from src.scrapers.alt_git_scraper import AltGitScraper
from src.scrapers.base import BaseScraper
from src.scrapers.forum_scraper import ForumScraper
from src.scrapers.github_scraper import GitHubScraper
from src.scrapers.portwatch_scraper import PortWatchScraper, load_portwatch_snapshot
from src.scrapers.reddit_scraper import RedditScraper
from src.scrapers.rss_scraper import RSSScraper
from src.scrapers.web_dork_scraper import WebDorkScraper
from src.storage import Storage
from src.utils.boot_tester import run_boot_and_compile_test
from src.utils.curated_discovery import (
    discover_curated_repos,
    fetch_whats_new,
    load_discovered_repos,
)
from src.utils.mods_manager import WORKSHOP_CATALOG, ModsManager
from src.utils.role_classifier import classify_role_and_deps
from src.utils.status_analyzer import infer_simple_status

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
ASSETS_DIR = (BASE_DIR / "Assets") if (BASE_DIR / "Assets").exists() else (BUNDLE_DIR / "Assets")
CONFIG_PATH = (BASE_DIR / "config.yaml") if (BASE_DIR / "config.yaml").exists() else (BUNDLE_DIR / "config.yaml")
DISCOVERED_CACHE = BASE_DIR / "data" / "discovered_repos.json"
PORTWATCH_SNAPSHOT = BASE_DIR / "data" / "portwatch_snapshot.json"
LAUNCHER_SETTINGS_PATH = BASE_DIR / "data" / "launcher_settings.json"
MODS_STATE_PATH = BASE_DIR / "data" / "mods_state.json"
SILENT_TASK_NAME = "GTA6_Port_Tracker_SilentWatch"
SAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9_-]+")

ALL_ROLES: tuple[tuple[str, str], ...] = (
    ("1. Portage / Exe PC", "PC Natif / Recomp"),
    ("2. Émulateur / Runtime", "Exécution Dump / Ému"),
    ("3. Recompilateur Statique", "PC Natif / Recomp"),
    ("4. Symboles / Config", "PC Natif / Recomp"),
    ("5. Outil RPF / RAGE 9", "Commun (RPF / Outils)"),
    ("6. Shaders / GPU", "Exécution Dump / Ému"),
    ("7. Dumper / ELF-SPRX", "Exécution Dump / Ému"),
    ("8. Décompilateur / IA RE", "PC Natif / Recomp"),
)


def _safe_role_folder(role: str) -> str:
    return SAFE_FOLDER_RE.sub("_", role).strip("_") or "Divers"


def replace_previous_role_files(role_quarantine_dir: Path, keep_file: Path) -> list[str]:
    """Sauvegarde (pour Rollback N-1) puis retire les versions antérieures d'un dossier de rôle."""
    removed: list[str] = []
    if not role_quarantine_dir.exists():
        return removed
    backup_dir = role_quarantine_dir.parent / "_backup"
    keep_resolved = keep_file.resolve()
    for fp in role_quarantine_dir.iterdir():
        if fp.is_file() and fp.resolve() != keep_resolved:
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                # Garder uniquement la dernière sauvegarde N-1
                for old_b in backup_dir.iterdir():
                    if old_b.is_file():
                        old_b.unlink(missing_ok=True)
                shutil.move(str(fp), str(backup_dir / fp.name))
                removed.append(fp.name)
            except OSError:
                try:
                    fp.unlink()
                    removed.append(fp.name)
                except OSError:
                    continue
    return removed


class TrackerController:
    """Contrôleur du Launcher GTA VI : 2 voies (Recomp PC & Ému Xbox Series), auto-découverte, test boot, veille silencieuse."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(BASE_DIR / config.general.db_path)
        drives = list_local_drives()
        sandbox_dirs = [Path(d.root) / "GTA6_Sandbox_Downloads" for d in drives]
        self.storage.sync_quarantine_folders(sandbox_dirs)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

        self.running: bool = False
        self.watch_state: str = "stopped"
        self.silent_mode: bool = False
        self.silent_interval_minutes: int = 60
        self.cycle: int = 0
        self.total_scraped: int = 0
        self.total_rejected: int = 0
        self.total_new: int = 0
        self.quota_summary: str = "Not checked"
        self.scraper_statuses: dict[str, str] = {}
        self.last_message: str = "Ready — select a compiled runtime and click PLAY."
        self.auto_logs: list[str] = []
        self._updates_checked: bool = False
        self._last_update_check: str = ""
        self._startup_sync_done: bool = False
        self._startup_stage: str = "Initializing runtime environment..."
        self._game_proc: subprocess.Popen[Any] | None = None
        self._game_runtime: str = ""
        self._game_stop_requested: bool = False
        self._game_log_path: Path = BASE_DIR / "data" / "last_launch.log"
        self._last_crash: dict[str, Any] | None = None
        self._crash_seq: int = 0
        self._prewarm_proc: subprocess.Popen[Any] | None = None
        self._prewarm_state: str = "idle"
        self._prewarm_detail: str = ""
        self._prewarm_runtime: str = ""

        self.auto_download_enabled: bool = True
        self.auto_download_min_score: int = 75
        self.auto_download_drive: str = max(drives, key=lambda d: d.free_gb).letter if drives else "D"
        self.gpu_profile: str = "NVIDIA"  # "NVIDIA" | "AMD" | "ALL"

        # Paramètres persistants du Launcher (FPS, Logs, Répertoire, Runtime, Sandboxie, Vanilla)
        self.settings: dict[str, Any] = self._load_launcher_settings()
        self.gpu_profile = str(self.settings.get("gpu_profile") or "NVIDIA").upper()
        self.auto_download_enabled = bool(self.settings.get("auto_download_enabled", True))
        if self.settings.get("auto_download_drive"):
            self.auto_download_drive = str(self.settings["auto_download_drive"]).upper()

        self.mods = ModsManager(
            MODS_STATE_PATH,
            Path(f"{self.auto_download_drive}:\\") / "GTA6_Sandbox_Downloads" / "Mods",
        )

        self._cached_drives = drives
        self._cached_backends: dict[str, Any] = {}
        self._sys_cache_ts: float = 0.0

        self._boot_cache: dict[str, Any] = {
            "recomp_readiness_pct": 0,
            "xbox_readiness_pct": 0,
            "readiness_pct": 0,
            "overall_status": "PARTIAL",
            "summary": "Run Environment Check to inspect both tracks.",
            "checks": [],
        }
        # Calcul rapide initial en arrière-plan pour afficher les 2 jauges immédiatement
        threading.Thread(target=self._warmup_boot_cache, daemon=True).start()

    @staticmethod
    def _local_appdata() -> Path:
        raw = os.environ.get("LOCALAPPDATA")
        if raw:
            return Path(raw)
        return Path.home() / "AppData" / "Local"

    @classmethod
    def _discover_default_game_dir(cls) -> str:
        """Détecte automatiquement le dossier du jeu (portable ./game puis disques locaux), sans chemin codé en dur."""
        portable_game = BASE_DIR / "game"
        if (portable_game / "game_scarlett_master.exe").exists():
            return str(portable_game)
        fallback_dump: Path | None = None
        for drv in list_local_drives():
            dump_root = Path(drv.root) / "GTA6_Dump"
            if not dump_root.exists():
                continue
            if fallback_dump is None:
                fallback_dump = dump_root
            if (dump_root / "game_scarlett_master.exe").exists():
                # Vérifier s'il existe un sous-dossier contenant aussi des .rpf
                for sub in dump_root.rglob("game_scarlett_master.exe"):
                    if sub.parent != dump_root and any(sub.parent.glob("*.rpf")):
                        return str(sub.parent)
                return str(dump_root)
            for sub in dump_root.rglob("game_scarlett_master.exe"):
                return str(sub.parent)
        return str(fallback_dump or portable_game)

    @classmethod
    def _discover_runtime_roots(cls, custom_dir: str = "") -> list[Path]:
        """Retourne les dossiers candidats contenant des runtimes compilés (portable ./runtimes + disques locaux)."""
        roots: list[Path] = []
        if custom_dir and Path(custom_dir).exists():
            roots.append(Path(custom_dir))
        for p in (BASE_DIR / "runtimes", BASE_DIR / "vendor"):
            if p.exists():
                roots.append(p)
        for drv in list_local_drives():
            sb = Path(drv.root) / "GTA6_Sandbox_Downloads"
            if sb.exists():
                roots.append(sb)
                nb_vendor = sb / "Neonbridge_VM" / "app" / "Neonbridge" / "vendor"
                if nb_vendor.exists():
                    roots.append(nb_vendor)
        # Dédupliquer en conservant l'ordre
        seen: set[str] = set()
        unique: list[Path] = []
        for r in roots:
            key = str(r).lower()
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    @staticmethod
    def _get_system_memory() -> dict[str, Any]:
        """Lit la RAM libre et la mémoire virtuelle (commit) via GlobalMemoryStatusEx (<1 ms)."""
        if os.name != "nt":
            return {"total_ram_gb": 0.0, "free_ram_gb": 0.0, "total_commit_gb": 0.0, "free_commit_gb": 0.0, "low_memory": False}
        try:
            import ctypes

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
                return {
                    "total_ram_gb": total_ram,
                    "free_ram_gb": free_ram,
                    "total_commit_gb": total_commit,
                    "free_commit_gb": free_commit,
                    "used_pct": int(ms.dwMemoryLoad),
                    "low_memory": free_ram < 5.0 or free_commit < 8.0,
                }
        except Exception:
            pass
        return {"total_ram_gb": 0.0, "free_ram_gb": 0.0, "total_commit_gb": 0.0, "free_commit_gb": 0.0, "used_pct": 0, "low_memory": False}

    @classmethod
    def _load_launcher_settings(cls) -> dict[str, Any]:
        default_game_dir = cls._discover_default_game_dir()
        defaults: dict[str, Any] = {
            "fps_limit": "60",
            "resolution": "1920x1080",
            "render_scale": "1.0",
            "console_profile": "auto",
            "ray_tracing": False,
            "show_logs": True,
            "gpu_profile": "NVIDIA",
            "install_dir": default_game_dir,
            "runtimes_dir": "",
            "launch_in_sandboxie": False,
            "selected_runtime": "cool_fox (Direct)",
            "vanilla_mode": False,
            "auto_download_enabled": True,
            "auto_download_drive": "D",
            "check_updates_on_startup": True,
        }
        if LAUNCHER_SETTINGS_PATH.exists():
            try:
                saved = json.loads(LAUNCHER_SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    defaults.update(saved)
            except Exception:
                pass
        return defaults

    def update_launcher_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for k in (
                "fps_limit",
                "resolution",
                "render_scale",
                "console_profile",
                "ray_tracing",
                "show_logs",
                "gpu_profile",
                "install_dir",
                "runtimes_dir",
                "launch_in_sandboxie",
                "selected_runtime",
                "vanilla_mode",
                "auto_download_enabled",
                "auto_download_drive",
                "check_updates_on_startup",
            ):
                if k in patch and patch[k] is not None:
                    self.settings[k] = patch[k]
            self.gpu_profile = str(self.settings.get("gpu_profile") or "NVIDIA").upper()
            self.auto_download_enabled = bool(self.settings.get("auto_download_enabled", True))
            self.auto_download_drive = str(self.settings.get("auto_download_drive") or "D").strip().rstrip(":\\/").upper()
            try:
                LAUNCHER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                LAUNCHER_SETTINGS_PATH.write_text(
                    json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                pass
        self._add_auto_log(
            f"⚙️ Settings saved (GPU: {self.gpu_profile}, FPS: {self.settings['fps_limit']}, "
            f"Res: {self.settings.get('resolution')}, Scale: {self.settings.get('render_scale')})"
        )
        return {"ok": True, "settings": dict(self.settings)}

    def _find_runtime_exe(self, rel_candidates: tuple[str, ...]) -> Path:
        roots = self._discover_runtime_roots(str(self.settings.get("runtimes_dir") or ""))
        for root in roots:
            for rel in rel_candidates:
                cand = root / rel
                if cand.exists():
                    return cand
        return (BASE_DIR / "runtimes" / rel_candidates[0])

    def _discover_lab_runtimes(self) -> list[dict[str, Any]]:
        """Détecte automatiquement les builds 'bricolage' compilés dans <drive>:\\GTA6_Lab\\workspaces ou ./lab/workspaces."""
        lab_specs: list[dict[str, Any]] = []
        lab_roots: list[Path] = []
        if (BASE_DIR / "lab").exists():
            lab_roots.append(BASE_DIR / "lab")
        for drv in list_local_drives():
            cand = Path(drv.root) / "GTA6_Lab"
            if cand.exists():
                lab_roots.append(cand)
        seen: set[str] = set()
        for lroot in lab_roots:
            ws_root = lroot / "workspaces"
            if not ws_root.exists():
                continue
            for ws in sorted(ws_root.iterdir()):
                if not ws.is_dir():
                    continue
                exe_cands = (
                    ws / "target" / "release" / "victory.exe",
                    ws / "runtime" / "victory" / "target" / "release" / "victory.exe",
                    ws / "target" / "release" / "xcl.exe",
                    ws / "target" / "diag" / "victory.exe",
                )
                found_exe = next((p for p in exe_cands if p.exists()), None)
                if found_exe is None:
                    continue
                key = str(found_exe).lower()
                if key in seen:
                    continue
                seen.add(key)
                is_xcl = found_exe.name.lower() == "xcl.exe"
                ws_name = ws.name
                lab_specs.append(
                    {
                        "id": f"lab_{ws_name.lower()}",
                        "label": f"LAB: {ws_name} (Direct)",
                        "aliases": (f"lab_{ws_name.lower()}", f"lab: {ws_name.lower()}"),
                        "exe": found_exe,
                        "mode": "dir" if is_xcl else "exe",
                        "use_exe_cwd": "scarlettport" in ws_name.lower(),
                        "extra_args": [] if is_xcl else ["--console", "series-x", "--execute"],
                        "prewarm_mode": "cool_fox" if "cool_fox" in ws_name.lower() else "none",
                        "env": {}
                        if is_xcl
                        else {
                            "VICTORY_CACHE_DIR": str(lroot / "cache" / ws_name),
                            "VICTORY_DATA_DIR": str(lroot / "saves" / ws_name),
                            "VICTORY_NO_WATCHDOG": "1",
                            "VICTORY_RAYTRACING": "0",
                            "VICTORY_AMBIENT_AIR": "1.5",
                            "VICTORY_TEXTURE_NO_WAIT": "1",
                            "VICTORY_RELAY": "2",
                            "VICTORY_WIDE_BACK": "1",
                            "VICTORY_CARD_REHASH_RUNS": "1",
                            "VICTORY_JOIN_COPIES": "1",
                            "VICTORY_INDIRECT_WINDOW": "1",
                            "VICTORY_RELAY_COPY": "1",
                            "VICTORY_CARD_STEPS": "2",
                            "VICTORY_INDIRECT_BATCH": "2",
                            "VICTORY_MAP_THREADS": "1",
                            "VICTORY_CARD_AWAKE": "1",
                            "VICTORY_SHOW_AFTER": "1",
                            "VICTORY_AMBIENT": "constant:0.8",
                            "VICTORY_TEXTURE_GUARD": "0",
                            "VICTORY_THIN": "0",
                            "VICTORY_JOIN_PARTS": "0",
                            "VICTORY_PORT_CONSTANTS": "0",
                            "VICTORY_PORT_CBV": "0",
                            "VICTORY_LIBRARY_STORE_LATER": "1",
                            "VICTORY_PIPELINE_WAIT_MS": "0",
                            "VICTORY_UPLOAD_AHEAD": "1",
                            "VICTORY_LEVELS_ON_DEMAND": "1",
                            "VICTORY_CARD_BUFFERS_MIN_KB": "4096",
                            "VICTORY_CARD_BUFFERS_MB": "512",
                            "VICTORY_D3D12_BACKGROUND": "0",
                            "VICTORY_WAVE_WIDE": "1",
                            "VICTORY_OUTPUT": "1920x1080",
                        },
                    }
                )
        return lab_specs

    def _get_direct_runtime_specs(self) -> list[dict[str, Any]]:
        """Liste des runtimes compilés lançables directement depuis le Launcher (détection portable, sans chemins codés en dur)."""
        local_app = self._local_appdata()
        shared_save = str(local_app / "6ixpack" / "save")
        base_specs = [
            {
                "id": "cool_fox",
                "label": "cool_fox (Direct)",
                "aliases": ("cool_fox", "coolfox", "doubleghosty"),
                "exe": self._find_runtime_exe(
                    (
                        "cool_fox/victory-3d-rtx-pareto-residency.exe",
                        "cool_fox/target/release/victory.exe",
                        "cool_fox/victory.exe",
                    )
                ),
                "mode": "exe",
                "extra_args": ["--console", "series-x", "--execute"],
                "prewarm_mode": "cool_fox",
                "env": {
                    "VICTORY_CACHE_DIR": str(local_app / "VIctory" / "cache-pareto"),
                    "VICTORY_DATA_DIR": shared_save,
                    "VICTORY_AMBIENT_AIR": "1.5",
                    "VICTORY_TEXTURE_NO_WAIT": "1",
                    "VICTORY_RELAY": "2",
                    "VICTORY_WIDE_BACK": "1",
                    "VICTORY_CARD_REHASH_RUNS": "1",
                    "VICTORY_JOIN_COPIES": "1",
                    "VICTORY_INDIRECT_WINDOW": "1",
                    "VICTORY_RELAY_COPY": "1",
                    "VICTORY_CARD_STEPS": "2",
                    "VICTORY_INDIRECT_BATCH": "2",
                    "VICTORY_MAP_THREADS": "1",
                    "VICTORY_CARD_AWAKE": "1",
                    "VICTORY_SHOW_AFTER": "1",
                    "VICTORY_AMBIENT": "constant:0.8",
                    "VICTORY_TEXTURE_GUARD": "0",
                    "VICTORY_THIN": "0",
                    "VICTORY_JOIN_PARTS": "0",
                    "VICTORY_PORT_CONSTANTS": "0",
                    "VICTORY_PORT_CBV": "0",
                    "VICTORY_LIBRARY_STORE_LATER": "1",
                    "VICTORY_PIPELINE_WAIT_MS": "0",
                    "VICTORY_UPLOAD_AHEAD": "1",
                    "VICTORY_LEVELS_ON_DEMAND": "1",
                    "VICTORY_CARD_BUFFERS_MIN_KB": "4096",
                    "VICTORY_CARD_BUFFERS_MB": "512",
                    "VICTORY_D3D12_BACKGROUND": "0",
                    "VICTORY_OUTPUT": "1920x1080",
                },
            },
            {
                "id": "scarlettport",
                "label": "ScarlettPort (Direct)",
                "aliases": ("scarlettport", "scarlett"),
                "exe": self._find_runtime_exe(
                    (
                        "scarlettport/runtime/victory/target/release/victory.exe",
                        "scarlettport/victory.exe",
                    )
                ),
                "mode": "exe",
                "use_exe_cwd": True,
                "extra_args": ["--execute", "--console", "series-s"],
                "prewarm_mode": "scarlettport",
                "env": {
                    "VICTORY_CACHE_DIR": str(local_app / "6ixpack" / "scarlettport" / "cache"),
                    "VICTORY_DATA_DIR": shared_save,
                    "VICTORY_RAYTRACING": "0",
                    "VICTORY_OWN_UPSCALER": "1",
                    "VICTORY_NO_WATCHDOG": "1",
                    "VICTORY_RELAY": "2",
                    "VICTORY_SYNC_PRECISE": "1",
                    "VICTORY_READ_SETTLE_PRECISE": "1",
                    "VICTORY_PIPELINE_WAIT_MS": "0",
                    "VICTORY_PRECOMPILED_GRAPHICS": "1",
                    "VICTORY_READFIX": "1",
                    "VICTORY_COPY_BACK_ONLY_READBACK": "1",
                    "VICTORY_NARROW_ROOT": "1",
                    "VICTORY_NARROW_VIEWS": "1",
                    "VICTORY_NARROW_BVH": "1",
                    "VICTORY_PREP_BARRIERS": "1",
                    "VICTORY_PREP_TABLES": "1",
                    "VICTORY_PREP_REUSE": "1",
                    "VICTORY_DISPLAY_MODE": "1920x1080",
                },
            },
            {
                "id": "ztrx-victory",
                "label": "ztrx-victory (Direct)",
                "aliases": ("ztrx", "ztrx-victory"),
                "exe": self._find_runtime_exe(
                    ("ztrx-victory/target/release/victory.exe", "ztrx-victory/victory.exe")
                ),
                "mode": "exe",
                "extra_args": ["--execute"],
                "prewarm_mode": "none",
                "env": {
                    "VICTORY_CACHE_DIR": str(local_app / "6ixpack" / "ztrx-victory" / "cache"),
                    "VICTORY_DATA_DIR": shared_save,
                    "VICTORY_RAYTRACING": "0",
                },
            },
            {
                "id": "vitoriabr",
                "label": "VItoriaBr (Direct)",
                "aliases": ("vitoriabr",),
                "exe": self._find_runtime_exe(
                    ("vitoriabr/target/release/victory.exe", "vitoriabr/victory.exe")
                ),
                "mode": "exe",
                "extra_args": ["--execute"],
                "prewarm_mode": "none",
                "env": {
                    "VICTORY_CACHE_DIR": str(local_app / "6ixpack" / "vitoriabr" / "cache"),
                    "VICTORY_DATA_DIR": shared_save,
                    "VICTORY_RAYTRACING": "0",
                    "VICTORY_RELAY": "2",
                    "VICTORY_TEXTURE_NO_WAIT": "1",
                    "VICTORY_CARD_BUFFERS_MB": "6144",
                    "VICTORY_WIDE_REFRESH": "1",
                    "VICTORY_WIDE_MERGE": "1",
                    "VICTORY_JOIN_COPIES": "1",
                    "VICTORY_DISPLAY_MODE": "1920x1080",
                },
            },
            {
                "id": "victory",
                "label": "VIctory (Direct)",
                "aliases": ("victory",),
                "exe": self._find_runtime_exe(
                    ("victory/target/release/victory.exe", "victory/victory.exe")
                ),
                "mode": "exe",
                "extra_args": ["--execute", "--console", "series-s"],
                "prewarm_mode": "victory",
                "env": {
                    "VICTORY_CACHE_DIR": str(local_app / "6ixpack" / "victory" / "cache"),
                    "VICTORY_DATA_DIR": shared_save,
                    "VICTORY_RAYTRACING": "0",
                    "VICTORY_OWN_UPSCALER": "1",
                    "VICTORY_NO_WATCHDOG": "1",
                    "VICTORY_RELAY": "2",
                    "VICTORY_SYNC_PRECISE": "1",
                    "VICTORY_READ_SETTLE_PRECISE": "1",
                    "VICTORY_PIPELINE_WAIT_MS": "0",
                },
            },
            {
                "id": "xcl",
                "label": "xcl (Direct)",
                "aliases": ("xcl",),
                "exe": self._find_runtime_exe(("xcl/target/release/xcl.exe", "xcl/xcl.exe")),
                "mode": "dir",
                "extra_args": [],
                "prewarm_mode": "none",
                "env": {},
            },
        ]
        return [*base_specs, *self._discover_lab_runtimes()]

    def _inspect_dump_status(self) -> dict[str, Any]:
        """Inspects the configured local dump directory without distributing or hardcoding game files."""
        raw = str(self.settings.get("install_dir") or "").strip()
        if not raw:
            return {"ready": False, "exists": False, "has_exe": False, "rpf_count": 0, "path": ""}
        p = Path(raw)
        exists = p.is_dir()
        has_exe = exists and (p / "game_scarlett_master.exe").is_file()
        rpf_count = 0
        if exists:
            try:
                rpf_count = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".rpf")
            except OSError:
                rpf_count = 0
        return {
            "ready": bool(exists and has_exe),
            "exists": exists,
            "has_exe": has_exe,
            "rpf_count": rpf_count,
            "path": str(p),
        }

    def _resolve_direct_runtime(self, selected_name: str, *, strict: bool = False) -> dict[str, Any] | None:
        specs = [s for s in self._get_direct_runtime_specs() if Path(s["exe"]).exists()]
        if not specs:
            return None
        low = (selected_name or "").strip().lower()
        # 1. Exact match priority (prevents 'cool_fox' matching 'LAB: cool_fox_lab (Direct)')
        for s in specs:
            if low == s["label"].lower() or low == s["id"]:
                return s
        # 2. Alias fallback
        for s in specs:
            if any(a in low for a in s["aliases"]):
                return s
        return None if strict else specs[0]

    def play_game(self) -> dict[str, Any]:
        """Lance directement le runtime compilé sélectionné sur le répertoire du jeu (sans Neonbridge UI ni Sandboxie par défaut)."""
        import time as _time

        if self._game_proc is not None and self._game_proc.poll() is None:
            return {"ok": False, "error": f"{self._game_runtime} is already running (PID {self._game_proc.pid})."}

        rt_name = str(self.settings.get("selected_runtime") or "cool_fox (Direct)")
        use_sb = bool(self.settings.get("launch_in_sandboxie", False))
        vanilla = bool(self.settings.get("vanilla_mode", False))
        fps = str(self.settings.get("fps_limit") or "60")
        resolution = str(self.settings.get("resolution") or "1920x1080")
        render_scale = str(self.settings.get("render_scale") or "1.0")
        console_prof = str(self.settings.get("console_profile") or "auto").strip().lower()
        ray_tracing = bool(self.settings.get("ray_tracing", False))
        game_dir = Path(str(self.settings.get("install_dir") or self._discover_default_game_dir()))

        if not game_dir.exists():
            msg = f"Game directory not found: {game_dir}"
            self._add_auto_log(f"❌ {msg}")
            return {"ok": False, "error": msg}

        game_exe = game_dir / "game_scarlett_master.exe"
        if not game_exe.exists():
            alt_exe = next(
                (Path(d.root) / "GTA6_Dump" / "game_scarlett_master.exe" for d in list_local_drives() if (Path(d.root) / "GTA6_Dump" / "game_scarlett_master.exe").exists()),
                None,
            )
            if alt_exe is not None:
                game_exe = alt_exe
            else:
                msg = f"game_scarlett_master.exe not found in {game_dir}"
                self._add_auto_log(f"❌ {msg}")
                return {"ok": False, "error": msg}

        spec = self._resolve_direct_runtime(rt_name, strict=True)
        if spec is None:
            any_spec = self._resolve_direct_runtime(rt_name, strict=False)
            if any_spec is not None and rt_name:
                msg = (
                    f"'{rt_name}' is tracked only and not compiled locally. "
                    f"Select an 'Installed & Ready (Direct)' runtime in the dropdown (e.g. {any_spec['label']})."
                )
            else:
                msg = "No compiled direct runtime (victory.exe / xcl.exe) found on disk."
            self._add_auto_log(f"❌ {msg}")
            return {"ok": False, "error": msg}

        rt_exe = Path(spec["exe"])
        target_arg = str(game_dir) if spec["mode"] == "dir" else str(game_exe)
        extra_args = list(spec["extra_args"])
        if console_prof in {"series-x", "series-s"} and spec["id"] != "xcl":
            if "--console" in extra_args:
                idx = extra_args.index("--console")
                if idx + 1 < len(extra_args):
                    extra_args[idx + 1] = console_prof
            else:
                extra_args.extend(["--console", console_prof])
        cmd = [str(rt_exe), "run", target_arg, *extra_args]

        show_logs = bool(self.settings.get("show_logs", False))
        env = os.environ.copy()
        env["VICTORY_GAME_DIR"] = str(game_dir)
        for k, v in spec["env"].items():
            env[k] = v
            if k.endswith("_CACHE_DIR") or k.endswith("_DATA_DIR"):
                Path(v).mkdir(parents=True, exist_ok=True)
        if resolution and "x" in resolution:
            env["VICTORY_OUTPUT"] = resolution
            env["VICTORY_DISPLAY_MODE"] = resolution
        if render_scale and render_scale != "1.0":
            env["VICTORY_RENDER_SCALE"] = render_scale
        env["VICTORY_RAYTRACING"] = "1" if ray_tracing else "0"
        if fps.isdigit():
            env["VICTORY_FPS_LIMIT"] = fps
            if spec["id"] == "cool_fox" and int(fps) > 0:
                env["VICTORY_FRAME_INTERVAL_MS"] = f"{1000.0 / int(fps):.3f}"

        # Activer ou réduire les logs du jeu selon le paramètre "Show Logs" du launcher
        if show_logs:
            env["RUST_LOG"] = "info"
            env["VICTORY_LOG"] = "info"
            env["RUST_BACKTRACE"] = "1"
        else:
            env["RUST_LOG"] = "error"
            env["VICTORY_LOG"] = "error"
            env.pop("RUST_BACKTRACE", None)

        if use_sb:
            sbie_start = Path(r"C:\Program Files\Sandboxie-Plus\Start.exe")
            if sbie_start.exists():
                cmd = [str(sbie_start), "/box:DefaultBox", *cmd]

        run_cwd = str(rt_exe.parent) if spec.get("use_exe_cwd") else str(game_dir)
        log_path = self._game_log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        log_fh.write(f"=== Direct Launch: {spec['label']} (Game Logs: {'ON' if show_logs else 'ERRORS ONLY'}) ===\n")
        log_fh.write(f"CMD: {' '.join(cmd)}\n")
        log_fh.write(f"CWD: {run_cwd}\n")
        log_fh.write(f"VICTORY_GAME_DIR: {game_dir}\n")
        log_fh.write(f"VICTORY_OWN_UPSCALER: {env.get('VICTORY_OWN_UPSCALER', 'unset')}\n")
        log_fh.write(f"VICTORY_RAYTRACING: {env.get('VICTORY_RAYTRACING', 'unset')}\n")
        log_fh.write(f"RUST_LOG: {env.get('RUST_LOG', '')}\n\n")
        log_fh.flush()

        try:
            self._game_stop_requested = False
            proc = subprocess.Popen(
                cmd,
                cwd=run_cwd,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._game_proc = proc
            self._game_runtime = spec["label"]
        except OSError as exc:
            log_fh.close()
            self._add_auto_log(f"❌ Failed to start {spec['label']}: {exc}")
            return {"ok": False, "error": str(exc)}

        # Surveiller la fin du processus en arrière-plan et reporter le code de sortie dans les logs
        def _watch_proc(p: subprocess.Popen[Any], fh: Any, label: str, lp: Path) -> None:
            try:
                rc = p.wait()
                fh.flush()
                fh.close()
                if rc != 0:
                    if self._game_stop_requested:
                        self._add_auto_log(f"⏹ {label} stopped by user (PID {p.pid}).")
                    else:
                        self._record_crash(label, rc, lp, p.pid)
                        tail = ""
                        try:
                            lines = [ln.strip() for ln in lp.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
                            tail = " | ".join(lines[-3:])
                        except OSError:
                            pass
                        self._add_auto_log(f"❌ {label} crashed with code {rc}: {tail[:260]}")
                else:
                    self._add_auto_log(f"⏹ {label} closed cleanly (exit 0).")
            except Exception:
                try:
                    fh.close()
                except Exception:
                    pass

        threading.Thread(target=_watch_proc, args=(proc, log_fh, spec["label"], log_path), daemon=True).start()

        # Vérification rapide (1.2s) pour détecter un crash immédiat au démarrage
        _time.sleep(1.2)
        rc_now = proc.poll()
        if rc_now is not None and rc_now != 0:
            self._record_crash(spec["label"], rc_now, log_path, proc.pid)
            err_tail = ""
            try:
                lines = [ln.strip() for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
                err_tail = " | ".join(lines[-4:])
            except OSError:
                pass
            return {
                "ok": False,
                "error": f"{spec['label']} crashed immediately (code {rc_now}). {err_tail}",
                "log_path": str(log_path),
            }

        mode_txt = "Sandboxie" if use_sb else "Direct"
        self._add_auto_log(
            f"▶ Launched {spec['label']} ({mode_txt}, PID={proc.pid}, FPS={fps}, Logs={'ON' if show_logs else 'OFF'}{', Vanilla' if vanilla else ''}) -> {game_dir}"
        )
        return {
            "ok": True,
            "pid": proc.pid,
            "runtime": spec["label"],
            "log_path": str(log_path),
            "message": f"Launched {spec['label']} directly (PID {proc.pid})!",
        }

    def _record_crash(self, label: str, exit_code: int, log_path: Path, pid: int) -> None:
        """Captures the last seconds of ANSI-cleaned logs right before an unexpected runtime crash."""
        with self._lock:
            if self._last_crash and self._last_crash.get("pid") == pid:
                return
        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        tail_lines: list[str] = []
        try:
            if log_path.exists():
                size = log_path.stat().st_size
                with open(log_path, "rb") as fh:
                    if size > 131_072:
                        fh.seek(size - 131_072)
                    raw = fh.read().decode("utf-8", errors="replace")
                cleaned = [ansi_re.sub("", ln).rstrip() for ln in raw.splitlines() if ln.strip()]
                tail_lines = cleaned[-80:]
        except OSError:
            tail_lines = ["(Unable to read runtime log file)"]

        exit_hex = f"0x{exit_code & 0xFFFFFFFF:08X}" if (exit_code < 0 or exit_code > 255) else str(exit_code)
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = (
            f"Runtime: {label}\n"
            f"Exit Code: {exit_hex} (raw={exit_code})\n"
            f"Timestamp: {ts_str}\n"
            f"Log File: {log_path}\n"
            f"--- Last seconds before crash ({len(tail_lines)} lines) ---\n"
        )
        excerpt = header + ("\n".join(tail_lines) if tail_lines else "(No log output captured before exit)")
        with self._lock:
            if self._last_crash and self._last_crash.get("pid") == pid:
                return
            self._crash_seq += 1
            self._last_crash = {
                "id": self._crash_seq,
                "pid": pid,
                "runtime": label,
                "exit_code": exit_code,
                "exit_hex": exit_hex,
                "timestamp": ts_str,
                "log_path": str(log_path),
                "excerpt": excerpt,
            }

    def dismiss_crash(self) -> dict[str, Any]:
        with self._lock:
            self._last_crash = None
        return {"ok": True}

    def stop_game(self) -> dict[str, Any]:
        """Arrête proprement le jeu en cours lancé depuis le launcher."""
        proc = self._game_proc
        if proc is None or proc.poll() is not None:
            self._game_proc = None
            return {"ok": True, "message": "No running game process."}
        pid = proc.pid
        label = self._game_runtime or "Runtime"
        self._game_stop_requested = True
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self._game_proc = None
        self._add_auto_log(f"⏹ Stopped {label} (PID {pid}).")
        return {"ok": True, "message": f"Stopped {label} (PID {pid})."}

    def prewarm_shaders(self) -> dict[str, Any]:
        """Pré-compile / pré-traduit les shaders en arrière-plan pour réduire les freezes et crashs 3D."""
        with self._lock:
            if self._prewarm_state == "running":
                return {"ok": False, "error": "Shader prewarm is already running."}
        rt_name = str(self.settings.get("selected_runtime") or "cool_fox (Direct)")
        spec = self._resolve_direct_runtime(rt_name)
        if spec is None:
            return {"ok": False, "error": "No compiled direct runtime found."}
        game_dir = Path(str(self.settings.get("install_dir") or self._discover_default_game_dir()))
        shaders_rpf = game_dir / "shaders.rpf"
        if not game_dir.exists():
            return {"ok": False, "error": f"Game directory not found: {game_dir}"}

        mode = str(spec.get("prewarm_mode") or "none")
        rt_exe = Path(spec["exe"])
        jobs = str(max(2, min(8, (os.cpu_count() or 4) // 2)))
        cmds: list[list[str]] = []
        if mode == "cool_fox":
            if shaders_rpf.exists():
                cmds.append([str(rt_exe), "shaders", str(shaders_rpf)])
            cmds.append([str(rt_exe), "warm", "--used"])
        elif mode == "scarlettport":
            cmds.append([str(rt_exe), "warm", str(game_dir), "--jobs", jobs])
        elif mode == "victory":
            cmds.append([str(rt_exe), "translate", str(game_dir), "--warm", "all", "--jobs", jobs])
        else:
            return {
                "ok": False,
                "error": f"{spec['label']} does not expose a shader prewarm command. Switch to cool_fox, ScarlettPort, or VIctory.",
            }

        env = os.environ.copy()
        env["VICTORY_GAME_DIR"] = str(game_dir)
        for k, v in spec["env"].items():
            env[k] = v
            if k.endswith("_CACHE_DIR") or k.endswith("_DATA_DIR"):
                Path(v).mkdir(parents=True, exist_ok=True)
        run_cwd = str(rt_exe.parent) if spec.get("use_exe_cwd") else str(game_dir)
        log_path = BASE_DIR / "data" / "prewarm.log"

        def _worker() -> None:
            with self._lock:
                self._prewarm_state = "running"
                self._prewarm_runtime = spec["label"]
                self._prewarm_detail = f"Starting shader prewarm ({spec['label']})..."
            self._add_auto_log(f"🔥 Prewarm shaders started ({spec['label']}, jobs={jobs})...")
            try:
                with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
                    for step_idx, cmd in enumerate(cmds, 1):
                        with self._lock:
                            if self._prewarm_state != "running":
                                break
                            self._prewarm_detail = f"Step {step_idx}/{len(cmds)}: {' '.join(cmd[1:3])}..."
                        fh.write(f"=== Step {step_idx}/{len(cmds)}: {' '.join(cmd)} ===\n")
                        fh.flush()
                        p = subprocess.Popen(
                            cmd,
                            cwd=run_cwd,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        self._prewarm_proc = p
                        if p.stdout:
                            for ln in p.stdout:
                                fh.write(ln)
                                s = ln.strip()
                                if s:
                                    with self._lock:
                                        self._prewarm_detail = s[-120:]
                        rc = p.wait()
                        if rc != 0:
                            # Sur cool_fox, l'étape 1 (shaders) peut paniquer sur 1 shader isolé après en avoir caché ~25 000 : on poursuit sur warm --used
                            if mode == "cool_fox" and step_idx < len(cmds):
                                self._add_auto_log(f"⚠️ Prewarm step {step_idx} exited ({rc}) after caching shaders — continuing to step {step_idx + 1}...")
                                continue
                            with self._lock:
                                if self._prewarm_state == "running":
                                    self._prewarm_state = "error"
                                    self._prewarm_detail = f"Exited with code {rc} (see data/prewarm.log)"
                            self._add_auto_log(f"⚠️ Shader prewarm exited with code {rc}.")
                            return
                with self._lock:
                    if self._prewarm_state == "running":
                        self._prewarm_state = "done"
                        self._prewarm_detail = f"Shader cache ready for {spec['label']}!"
                self._add_auto_log(f"✅ Shader prewarm complete ({spec['label']})!")
            except Exception as exc:
                with self._lock:
                    self._prewarm_state = "error"
                    self._prewarm_detail = str(exc)[:120]
                self._add_auto_log(f"❌ Shader prewarm failed: {exc}")
            finally:
                self._prewarm_proc = None

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "message": f"Shader prewarm started for {spec['label']}!"}

    def stop_prewarm(self) -> dict[str, Any]:
        proc = self._prewarm_proc
        with self._lock:
            self._prewarm_state = "idle"
            self._prewarm_detail = "Cancelled by user."
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._prewarm_proc = None
        self._add_auto_log("⏹ Shader prewarm cancelled.")
        return {"ok": True, "message": "Shader prewarm cancelled."}

    def _saves_dir(self) -> Path:
        d = self._local_appdata() / "6ixpack" / "save"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_backups_dir(self) -> Path:
        d = BASE_DIR / "data" / "save_backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_saves_info(self) -> dict[str, Any]:
        bdir = self._save_backups_dir()
        zips = sorted(bdir.glob("save_backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {
            "save_dir": str(self._saves_dir()),
            "backup_count": len(zips),
            "latest_backup": zips[0].name if zips else "",
        }

    def backup_saves(self) -> dict[str, Any]:
        sdir = self._saves_dir()
        bdir = self._save_backups_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_zip = bdir / f"save_backup_{ts}.zip"
        files = [f for f in sdir.rglob("*") if f.is_file()]
        if not files:
            return {"ok": False, "error": "Save folder is empty — play the game first to create saves."}
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=str(f.relative_to(sdir)))
        # Garder les 10 sauvegardes les plus récentes
        old_zips = sorted(bdir.glob("save_backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old_zips[10:]:
            stale.unlink(missing_ok=True)
        self._add_auto_log(f"💾 Saves backed up: {out_zip.name} ({len(files)} file(s))")
        return {"ok": True, "message": f"Backup created: {out_zip.name} ({len(files)} files)", "backup": out_zip.name}

    def restore_saves(self, filename: str = "") -> dict[str, Any]:
        bdir = self._save_backups_dir()
        zips = sorted(bdir.glob("save_backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not zips:
            return {"ok": False, "error": "No save backup available yet."}
        target_zip = (bdir / filename) if filename and (bdir / filename).exists() else zips[0]
        sdir = self._saves_dir()
        with zipfile.ZipFile(target_zip, "r") as zf:
            zf.extractall(sdir)
        self._add_auto_log(f"↩️ Saves restored from {target_zip.name}")
        return {"ok": True, "message": f"Restored saves from {target_zip.name}!"}

    def open_saves_folder(self) -> dict[str, Any]:
        sdir = self._saves_dir()
        if os.name == "nt":
            os.startfile(str(sdir))  # type: ignore[attr-defined]
        return {"ok": True, "save_dir": str(sdir)}

    def _read_game_log_tail(self, max_lines: int = 160, include_lines: bool = True) -> dict[str, Any]:
        """Lit les dernières lignes de data/last_launch.log + FPS live + compte erreurs/avertissements."""
        lp = self._game_log_path
        running = bool(self._game_proc is not None and self._game_proc.poll() is None)
        pid = self._game_proc.pid if running and self._game_proc is not None else None
        if not lp.exists():
            return {
                "running": running,
                "pid": pid,
                "runtime": self._game_runtime,
                "log_path": str(lp),
                "live_fps": None,
                "live_draws": None,
                "lines": [],
                "errors_tail": [],
                "error_count": 0,
                "warn_count": 0,
            }
        try:
            # Lire au max les derniers 256 Ko pour rester instantané même après des heures de jeu
            size = lp.stat().st_size
            with open(lp, "rb") as fh:
                if size > 262_144:
                    fh.seek(size - 262_144)
                chunk = fh.read().decode("utf-8", errors="replace")
            all_lines = chunk.splitlines()
            if size > 262_144 and all_lines:
                all_lines = all_lines[1:]
        except OSError:
            all_lines = []

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        all_lines = [ansi_re.sub("", ln) for ln in all_lines]

        err_re = re.compile(r"\b(ERROR|FATAL)\b|panic|panicked|exception|crashed|os error", re.IGNORECASE)
        warn_re = re.compile(r"\bWARN\b", re.IGNORECASE)
        fps_re = re.compile(r"([\d.]+)\s+fps,.*?(\d+)\s+draws/frame", re.IGNORECASE)
        error_count = 0
        warn_count = 0
        errors_tail: list[str] = []
        live_fps: float | None = None
        live_draws: int | None = None
        for ln in all_lines:
            if err_re.search(ln):
                error_count += 1
                errors_tail.append(ln)
            elif warn_re.search(ln):
                warn_count += 1
                errors_tail.append(ln)
            m_fps = fps_re.search(ln)
            if m_fps:
                try:
                    live_fps = round(float(m_fps.group(1)), 1)
                    live_draws = int(m_fps.group(2))
                except ValueError:
                    pass

        return {
            "running": running,
            "pid": pid,
            "runtime": self._game_runtime or str(self.settings.get("selected_runtime") or ""),
            "log_path": str(lp),
            "live_fps": live_fps,
            "live_draws": live_draws,
            "lines": all_lines[-max_lines:] if include_lines else [],
            "errors_tail": errors_tail[-100:] if include_lines else [],
            "error_count": error_count,
            "warn_count": warn_count,
        }

    def open_game_log(self) -> dict[str, Any]:
        lp = self._game_log_path
        if not lp.exists():
            return {"ok": False, "error": "No game log file yet. Launch the game first."}
        if os.name == "nt":
            os.startfile(str(lp))  # type: ignore[attr-defined]
        return {"ok": True, "log_path": str(lp)}

    def open_live_log_console(self) -> dict[str, Any]:
        lp = self._game_log_path
        lp.parent.mkdir(parents=True, exist_ok=True)
        if not lp.exists():
            lp.write_text("Waiting for game launch logs...\n", encoding="utf-8")
        if os.name == "nt":
            ps_cmd = (
                f"$Host.UI.RawUI.WindowTitle = 'VI Community Launcher — Live Runtime Logs'; "
                f"Get-Content -Path '{str(lp)}' -Wait -Tail 60"
            )
            subprocess.Popen(
                ["powershell", "-NoExit", "-Command", ps_cmd],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        return {"ok": True, "log_path": str(lp)}

    def update_all_pillars(self) -> dict[str, Any]:
        """Met à jour tous les piliers critiques ayant une MàJ disponible (compatible GPU)."""
        state = self.get_cockpit_state(gpu=self.gpu_profile)
        updated_names: list[str] = []
        for pil in state.get("pillars") or []:
            best = pil.get("best")
            if pil.get("update_suggested") and best and best.get("gpu_ok", True) and best.get("download_url"):
                r = self.download_role_update(
                    url=best["download_url"],
                    role=best["role"],
                    drive=self.auto_download_drive,
                    item_id=best["id"],
                    replace_old=True,
                )
                if r.get("ok"):
                    updated_names.append(best["title"])
        msg = (
            f"Updated {len(updated_names)} component(s): {', '.join(updated_names)}"
            if updated_names
            else "All critical components are already up to date!"
        )
        return {"ok": True, "updated": updated_names, "message": msg}

    def rollback_role(self, role: str, drive: str) -> dict[str, Any]:
        """Restores the previous N-1 backup from by_role/<role>/_backup/."""
        target_root = self._resolve_drive_target(drive)
        if target_root is None:
            return {"ok": False, "error": "Storage drive not found."}
        role_dir = target_root / "by_role" / _safe_role_folder(role)
        backup_dir = role_dir / "_backup"
        q_dir = role_dir / "quarantine"
        if not backup_dir.exists():
            return {"ok": False, "error": "No previous (N-1) backup available for this component."}
        backups = [f for f in backup_dir.iterdir() if f.is_file()]
        if not backups:
            return {"ok": False, "error": "No previous (N-1) backup available for this component."}
        q_dir.mkdir(parents=True, exist_ok=True)
        for cur_f in q_dir.iterdir():
            if cur_f.is_file():
                cur_f.unlink(missing_ok=True)
        restored = backups[0]
        shutil.move(str(restored), str(q_dir / restored.name))
        self._add_auto_log(f"↩️ Rollback [{role}]: restored {restored.name}.")
        return {"ok": True, "message": f"Restored previous version: {restored.name}"}

    def clear_cache(self) -> dict[str, Any]:
        """Clears launcher discovery & thumbnail caches without touching compiled shader caches or local dumps."""
        removed = 0
        for p in (
            BASE_DIR / "data" / "discovered_repos.json",
            BASE_DIR / "data" / "mod_thumbs",
        ):
            if p.is_file():
                p.unlink(missing_ok=True)
                removed += 1
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        self._add_auto_log(f"🧹 Launcher cache cleared ({removed} item(s)).")
        return {"ok": True, "message": f"Launcher cache cleared ({removed} item(s))."}

    def install_workshop_mod(self, workshop_id: str) -> dict[str, Any]:
        pw_snap = load_portwatch_snapshot(PORTWATCH_SNAPSHOT)
        ws_list = self.mods.get_state(portwatch_tools=pw_snap.get("tools") or []).get("workshop") or []
        w = next((x for x in ws_list if x["id"] == workshop_id), None)
        if not w:
            return {"ok": False, "error": "Workshop mod not found."}

        async def _dl() -> dict[str, Any]:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                rep = await quarantine_download(
                    w["url"],
                    self.mods.mods_dir,
                    client,
                    virustotal_api_key=self.config.virustotal_api_key,
                )
                return {
                    "ok": rep.saved_path is not None,
                    "saved_path": str(rep.saved_path or ""),
                    "defender": rep.defender_status,
                }

        try:
            r = asyncio.run(_dl())
            if r.get("ok"):
                self.mods.register_installed_workshop_mod(workshop_id, r["saved_path"], fallback_entry=w)
                self._add_auto_log(f"🎨 Workshop tool installed (Defender OK): {w['name']}")
                return {"ok": True, "message": f"{w['name']} installed & verified by Defender!"}
            return {"ok": False, "error": "Download failed."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _warmup_boot_cache(self) -> None:
        try:
            with self._lock:
                self._startup_stage = "Checking runtime environment..."
            try:
                import time as _time

                self._cached_backends = detect_available_backends()
                self._sys_cache_ts = _time.monotonic()
            except Exception:
                pass
            try:
                self.run_boot_test(self.auto_download_drive)
            except Exception:
                pass
            if bool(self.settings.get("check_updates_on_startup", True)):
                with self._lock:
                    self._startup_stage = "Syncing latest GitHub & PortWatch releases..."
                try:
                    self.sync_portwatch()
                except Exception:
                    pass
            else:
                self._add_auto_log("ℹ️ Startup update check is disabled (PLAY ready by default).")
        finally:
            with self._lock:
                self._startup_sync_done = True
                self._startup_stage = "Ready"

    def check_updates_now(self) -> dict[str, Any]:
        """Runs an explicit PortWatch VI sync + update check and returns pending update status."""
        res = self.sync_portwatch()
        if not res.get("ok"):
            return res
        state = self.get_cockpit_state(gpu=self.gpu_profile)
        return {
            "ok": True,
            "has_updates": bool(state.get("has_updates")),
            "pending_count": int(state.get("pending_updates_count") or 0),
            "checked_at": self._last_update_check,
            "builds": res.get("builds", 0),
            "tools": res.get("tools", 0),
        }

    def sync_portwatch(self) -> dict[str, Any]:
        """Synchronizes leaderboard, tools, and news from PortWatch VI into tracker.db."""
        async def _run() -> list[ScrapedItem]:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                pw = PortWatchScraper(self.config, client, snapshot_path=PORTWATCH_SNAPSHOT)
                return await pw.fetch()

        try:
            items = asyncio.run(_run())
            new_items = self.storage.save_new_items(items) if items else []
            snap = load_portwatch_snapshot(PORTWATCH_SNAPSHOT)
            with self._lock:
                self._updates_checked = True
                self._last_update_check = datetime.now().strftime("%H:%M:%S")
            self._add_auto_log(
                f"🏆 PortWatch VI synced: {len(snap.get('leaderboard') or [])} builds, "
                f"{len(snap.get('tools') or [])} tools (+{len(new_items)} new)"
            )
            return {
                "ok": True,
                "builds": len(snap.get("leaderboard") or []),
                "tools": len(snap.get("tools") or []),
                "new_saved": len(new_items),
                "updated": snap.get("updated", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _add_auto_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.auto_logs.append(f"[{ts}] {msg}")
            if len(self.auto_logs) > 80:
                self.auto_logs = self.auto_logs[-80:]

    def set_auto_config(
        self, *, enabled: bool, drive: str, min_score: int = 75, gpu_profile: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.auto_download_enabled = bool(enabled)
            if drive:
                self.auto_download_drive = drive.strip().rstrip(":\\/").upper()
            self.auto_download_min_score = max(40, min(100, int(min_score)))
            if gpu_profile and gpu_profile.upper() in {"NVIDIA", "AMD", "ALL"}:
                self.gpu_profile = gpu_profile.upper()
        self._add_auto_log(
            f"Config : GPU {self.gpu_profile} | Auto-DL VM {'ACTIVÉ' if enabled else 'DÉSACTIVÉ'} (Disque {self.auto_download_drive}:)"
        )
        return {"ok": True}

    def start(
        self,
        sources: list[str] | None = None,
        *,
        silent: bool = False,
        interval_minutes: int = 60,
    ) -> dict[str, Any]:
        with self._lock:
            if self.running or self.watch_state in {"starting", "running"}:
                return {"ok": True, "message": "Watch is already running."}
            self._stop_event.clear()
            self.running = True
            self.silent_mode = bool(silent)
            self.silent_interval_minutes = max(15, min(720, int(interval_minutes)))
            self.watch_state = "starting"
            self.last_message = (
                f"🌙 Silent watch (every {self.silent_interval_minutes} min, milestones only)..."
                if self.silent_mode
                else "⏳ Starting live watch..."
            )
            selected = sources or ["all"]
            self._worker_thread = threading.Thread(
                target=self._run_loop_thread, args=(selected,), daemon=True
            )
            self._worker_thread.start()
            return {"ok": True}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                self.watch_state = "stopped"
                self.silent_mode = False
                return {"ok": True}
            self._stop_event.set()
            self.watch_state = "stopping"
            self.last_message = "⏳ Stopping..."
            return {"ok": True}

    def _resolve_drive_target(self, drive: str) -> Path | None:
        drives = {d.letter.upper(): d for d in list_local_drives()}
        key = drive.strip().rstrip(":\\/").upper()
        if key not in drives:
            return None
        target = Path(drives[key].root) / "GTA6_Sandbox_Downloads"
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def _download_and_replace_async(
        self,
        *,
        url: str,
        role: str,
        drive: str,
        client: httpx.AsyncClient,
        item_id: str | None = None,
        replace_old: bool = True,
    ) -> dict[str, Any]:
        target_root = self._resolve_drive_target(drive)
        if target_root is None:
            return {"ok": False, "error": f"Drive '{drive}' not found."}

        role_dir = target_root / "by_role" / _safe_role_folder(role)
        report = await quarantine_download(
            url,
            role_dir,
            client,
            virustotal_api_key=self.config.virustotal_api_key,
        )

        removed: list[str] = []
        if replace_old and report.saved_path is not None:
            removed = replace_previous_role_files(role_dir / "quarantine", report.saved_path)

        item_updated_iso = None
        if item_id:
            it_obj = self.storage.get_item_by_id(item_id)
            if it_obj and it_obj.effective_date:
                item_updated_iso = it_obj.effective_date.isoformat()

        self.storage.record_download(
            url=url,
            filename=report.saved_path.name if report.saved_path else "file",
            saved_path=str(report.saved_path or ""),
            sha256=report.sha256,
            size_bytes=report.size_bytes,
            item_id=item_id,
            item_updated_at=item_updated_iso,
        )

        wsb_path, _ = generate_windows_sandbox_files(url, target_root)
        return {
            "ok": True,
            "saved_path": str(report.saved_path),
            "wsb_path": str(wsb_path),
            "removed_previous": removed,
            "size_bytes": report.size_bytes,
            "sha256": report.sha256,
            "detected_magic": report.detected_magic,
            "suspicious_mismatch": report.suspicious_mismatch,
            "defender_status": report.defender_status,
            "virustotal_status": report.virustotal_status,
            "ip_used": report.ip_used,
            "message": report.notes,
        }

    def download_role_update(
        self, *, url: str, role: str, drive: str, item_id: str | None = None, replace_old: bool = True
    ) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                return await self._download_and_replace_async(
                    url=url,
                    role=role,
                    drive=drive,
                    client=client,
                    item_id=item_id,
                    replace_old=replace_old,
                )

        try:
            res = asyncio.run(_run())
            if res.get("ok"):
                rem = res.get("removed_previous") or []
                rem_txt = f" ({len(rem)} ancienne(s) version(s) remplacée(s))" if rem else ""
                self._add_auto_log(f"✅ [{role}] Téléchargé sur {drive}:{rem_txt}")
            return res
        except Exception as exc:
            self._add_auto_log(f"❌ [{role}] Échec téléchargement : {exc}")
            return {"ok": False, "error": str(exc)}

    def run_boot_test(self, drive: str) -> dict[str, Any]:
        target = self._resolve_drive_target(drive)
        if target is None:
            return {"ok": False, "error": f"Drive '{drive}' not found."}
        game_dir_str = str(self.settings.get("install_dir") or "").strip()
        dump_dir = Path(game_dir_str) if game_dir_str else None
        report = run_boot_and_compile_test(target, dump_folder=dump_dir)
        payload = {
            "ok": True,
            "readiness_pct": report.readiness_pct,
            "recomp_readiness_pct": report.recomp_readiness_pct,
            "xbox_readiness_pct": report.xbox_readiness_pct,
            "overall_status": report.overall_status,
            "summary": report.summary,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "track": c.track}
                for c in report.checks
            ],
        }
        with self._lock:
            self._boot_cache = payload
        return payload

    def pick_game_dir(self) -> dict[str, Any]:
        """Opens a native Windows folder dialog to import the user's own local dump directory."""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            initial = str(self.settings.get("install_dir") or self._discover_default_game_dir())
            chosen = filedialog.askdirectory(
                title="Select Local Dump Directory",
                initialdir=initial if Path(initial).exists() else str(BASE_DIR),
            )
            root.destroy()
            if chosen:
                chosen_p = Path(chosen)
                # Smart auto-resolve if the user picked a parent folder containing the actual dump subfolder
                if not (chosen_p / "game_scarlett_master.exe").is_file():
                    sub_exes = [s for s in chosen_p.rglob("game_scarlett_master.exe") if s.is_file()]
                    if sub_exes:
                        best_sub = next((s.parent for s in sub_exes if any(s.parent.glob("*.rpf"))), sub_exes[0].parent)
                        chosen_p = best_sub
                norm = str(chosen_p)
                self.update_launcher_settings({"install_dir": norm})
                dump_st = self._inspect_dump_status()
                self._add_auto_log(f"📂 Local dump directory set: {norm} (ready={dump_st['ready']}, rpf={dump_st['rpf_count']})")
                return {"ok": True, "install_dir": norm, "dump_status": dump_st}
            return {"ok": True, "install_dir": "", "dump_status": self._inspect_dump_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_game_dir(self) -> dict[str, Any]:
        gdir = Path(str(self.settings.get("install_dir") or self._discover_default_game_dir()))
        gdir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(gdir))  # type: ignore[attr-defined]
        return {"ok": True, "install_dir": str(gdir)}

    def reclassify_database(self) -> dict[str, Any]:
        res = self.storage.reclassify_all_items(config=self.config)
        self._add_auto_log(
            f"🔄 Base reclassée & scores recalibrés : {res['updated']} items "
            f"(Recomp PC: {res['recomp_count']}, Ému Xbox: {res['xbox_count']}, Commun: {res['common_count']})"
        )
        return {"ok": True, **res}

    def run_curated_discovery(self) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                return await discover_curated_repos(
                    client,
                    already_watched=self.config.sources.github.watched_repos,
                    cache_path=DISCOVERED_CACHE,
                    timeout=self.config.general.request_timeout,
                )

        try:
            res = asyncio.run(_run())
            found = res.get("newly_discovered") or []
            self._add_auto_log(
                f"🧭 Auto-découverte curatée : +{len(found)} nouveau(x) dépôt(s) "
                f"(total suivi auto : {res.get('total_discovered', 0)})"
            )
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_whats_new(
        self,
        url: str,
        item_id: str | None = None,
        *,
        role: str = "",
        current_title: str = "",
        current_version: str = "",
        current_date: str = "",
    ) -> dict[str, Any]:
        try:
            dl_by_url, dl_by_item = self.storage.get_download_history_map()
            installed_rec = (dl_by_item.get(item_id) if item_id else None) or dl_by_url.get(url)
            installed_since = current_date or (installed_rec["downloaded_at"] if installed_rec else "")

            res = asyncio.run(
                fetch_whats_new(
                    url,
                    github_token=self.config.github_token,
                    timeout=12.0,
                    installed_since=installed_since,
                )
            )
            it = self.storage.get_item_by_id(item_id) if item_id else None
            if it:
                res["item_title"] = it.title
                res["item_summary"] = it.summary
                res["item_status"] = it.playability_status or "—"

            # Extract currently installed version label
            if current_version:
                cur_ver = current_version
            elif installed_rec:
                fn = str(installed_rec.get("filename") or "")
                dt = str(installed_rec.get("downloaded_at") or "")[:10]
                m_v = re.search(r"(v\d+(?:\.\d+)+)", fn, re.IGNORECASE)
                cur_ver = f"{m_v.group(1)} (installed {dt})" if m_v else f"Local archive ({dt})"
            else:
                cur_ver = "Not installed"

            # Enrich with PortWatch VI metrics (progress, FPS, render, GPU, highlights)
            pw = load_portwatch_snapshot(PORTWATCH_SNAPSHOT)
            repo_low = str(res.get("repo") or "").lower()
            pw_new = next(
                (e for e in (pw.get("leaderboard") or []) if (e.get("repo") or "").lower() == repo_low and repo_low),
                None,
            )
            pw_cur = next(
                (
                    e
                    for e in (pw.get("leaderboard") or [])
                    if current_title
                    and (
                        (e.get("repo") or "").lower() in current_title.lower()
                        or (e.get("name") or "").lower() in current_title.lower()
                    )
                ),
                None,
            )

            diffs: list[str] = []
            if not current_title and not installed_rec:
                diffs.append(f"First installation for role '{role or 'Component'}'.")
            elif current_title and repo_low and repo_low not in current_title.lower():
                diffs.append(f"Replaces '{current_title}' with '{res.get('repo')}' (higher ranked / better match).")
                if pw_new:
                    gpus = "/".join(pw_new.get("gpu") or []) or "Universal"
                    diffs.append(
                        f"PortWatch VI: {pw_new['score_pct']}% progress · {pw_new.get('fps', 0)} FPS · "
                        f"Render {pw_new.get('render_pct', 0)}% · GPU {gpus}."
                    )
                    if pw_cur:
                        delta_pct = round(pw_new["score_pct"] - pw_cur["score_pct"], 1)
                        if delta_pct > 0:
                            diffs.append(f"Estimated PortWatch VI gain: +{delta_pct}% over current component.")
                    for hl in (pw_new.get("highlights") or [])[:2]:
                        diffs.append(f"Highlight: {hl}")
            else:
                # Same project: list commits/releases since installed version
                since_commits = res.get("commits_since_install") or []
                if res.get("release_tag"):
                    diffs.append(f"New release available: {res['release_tag']} ({res.get('release_date', '')}).")
                if since_commits:
                    diffs.append(f"{len(since_commits)} new commit(s) since your local version:")
                    for c in since_commits[:4]:
                        diffs.append(f"• [{c['sha']}] {c['message']}")
                elif res.get("commits"):
                    diffs.append(f"Latest GitHub commit: [{res['commits'][0]['sha']}] {res['commits'][0]['message']}")
                if pw_new and pw_new.get("summary"):
                    diffs.append(f"PortWatch VI summary ({pw_new['score_pct']}%): {pw_new['summary'][:180]}")

            if not diffs:
                diffs.append("Update to the latest revision available on the source repository.")

            res["current_version"] = cur_ver
            res["current_title"] = current_title or (installed_rec.get("filename") if installed_rec else "")
            res["differences"] = diffs
            res["portwatch_info"] = pw_new
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def manage_silent_task(self, action: str, interval_minutes: int = 60) -> dict[str, Any]:
        if action == "start":
            return self.start(sources=["all"], silent=True, interval_minutes=interval_minutes)
        if action == "stop":
            return self.stop()
        if os.name != "nt":
            return {"ok": False, "error": "Windows Task Scheduler is only available on Windows."}

        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if action == "status":
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", SILENT_TASK_NAME],
                capture_output=True,
                text=True,
                creationflags=no_win,
            )
            return {"ok": True, "installed": proc.returncode == 0}

        if action == "install_task":
            py_exe = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
            if not py_exe.exists():
                py_exe = Path(sys.executable)
            script_path = BASE_DIR / "silent_watch.py"
            hours = max(1, min(12, int(round(interval_minutes / 60))))
            cmd_tr = f'"{py_exe}" "{script_path}" --once'
            proc = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "HOURLY",
                    "/MO",
                    str(hours),
                    "/TN",
                    SILENT_TASK_NAME,
                    "/TR",
                    cmd_tr,
                ],
                capture_output=True,
                text=True,
                creationflags=no_win,
            )
            if proc.returncode == 0:
                self._add_auto_log(f"🌙 Windows Task '{SILENT_TASK_NAME}' enabled (every {hours}h).")
                return {"ok": True, "installed": True, "message": f"Silent watch scheduled every {hours}h."}
            return {"ok": False, "error": (proc.stderr or proc.stdout or "schtasks failed").strip()}

        if action == "remove_task":
            proc = subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", SILENT_TASK_NAME],
                capture_output=True,
                text=True,
                creationflags=no_win,
            )
            if proc.returncode == 0:
                self._add_auto_log(f"🗑️ Windows Task '{SILENT_TASK_NAME}' removed.")
                return {"ok": True, "installed": False, "message": "Silent watch task removed."}
            return {"ok": True, "installed": False, "message": "No active scheduled task."}

        return {"ok": False, "error": "Unknown action."}

    def launch_local_tool(self, tool: str, drive: str) -> dict[str, Any]:
        if tool == "neonbridge":
            bat = BASE_DIR / "Lancer_Neonbridge_Sandbox.bat"
            if bat.exists():
                subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(BASE_DIR))
                self._add_auto_log("🚀 Neonbridge launched in Sandboxie-Plus.")
                return {"ok": True, "message": "Neonbridge launched in Sandboxie-Plus."}
            return {"ok": False, "error": "Lancer_Neonbridge_Sandbox.bat not found."}

        if tool == "pagiii":
            bat = BASE_DIR / "Lancer_Pagiii_Tracker.bat"
            if bat.exists():
                subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(BASE_DIR))
                self._add_auto_log("🚀 Local tracker launched on http://127.0.0.1:3090.")
                return {"ok": True, "message": "Local tracker launched (http://127.0.0.1:3090)."}
            return {"ok": False, "error": "Lancer_Pagiii_Tracker.bat not found."}

        if tool == "deps":
            bat = BASE_DIR / "Lancer_Gestionnaire_Dependances.bat"
            if bat.exists():
                subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(BASE_DIR))
                self._add_auto_log("📦 Dependency manager opened.")
                return {"ok": True, "message": "Dependency manager opened."}
            return {"ok": False, "error": "Lancer_Gestionnaire_Dependances.bat not found."}

        if tool == "sandbox":
            return self.launch_inspection_vm(drive)

        if tool == "folder":
            d = drive.strip().rstrip(":\\/").upper()
            target = Path(f"{d}:\\") / "GTA6_Sandbox_Downloads" / "by_role"
            target.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True, "message": f"Opened folder: {target}"}

        return {"ok": False, "error": f"Unknown tool: {tool}"}

    def _run_loop_thread(self, selected_sources: list[str]) -> None:
        try:
            asyncio.run(self._async_watch_loop(selected_sources))
        except Exception as exc:
            with self._lock:
                self.last_message = f"Erreur veille : {exc}"
        finally:
            with self._lock:
                self.running = False
                self.watch_state = "stopped"
                self.silent_mode = False
                if self._stop_event.is_set():
                    self.last_message = "Veille arrêtée proprement."

    async def _async_watch_loop(self, selected_sources: list[str]) -> None:
        selected = set(selected_sources)
        run_all = "all" in selected
        rf = RelevanceFilter(self.config)
        discovered_forums: set[str] = set()
        batch_lock = asyncio.Lock()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            parallel_scrapers: list[BaseScraper] = []
            github_scraper: GitHubScraper | None = None
            forum_scraper: ForumScraper | None = None

            if run_all or "portwatch" in selected:
                parallel_scrapers.append(
                    PortWatchScraper(self.config, client, snapshot_path=PORTWATCH_SNAPSHOT)
                )
            if run_all or "github" in selected:
                github_scraper = GitHubScraper(self.config, client)
                parallel_scrapers.append(github_scraper)
            if run_all or "web_dork" in selected:
                parallel_scrapers.append(
                    WebDorkScraper(self.config, client, forum_url_sink=discovered_forums)
                )
            if run_all or "alt_git" in selected:
                parallel_scrapers.append(AltGitScraper(self.config, client))
            if run_all or "reddit" in selected:
                parallel_scrapers.append(RedditScraper(self.config, client))
            if run_all or "rss" in selected:
                parallel_scrapers.append(RSSScraper(self.config, client))
            if run_all or "forum" in selected:
                forum_scraper = ForumScraper(
                    self.config, client, discovered_urls=discovered_forums
                )

            if github_scraper is not None:
                await github_scraper.check_rate_limit()
                with self._lock:
                    self.quota_summary = github_scraper.quota_summary()

            def on_status(name: str, msg: str) -> None:
                with self._lock:
                    self.scraper_statuses[name] = msg
                    if github_scraper is not None:
                        self.quota_summary = github_scraper.quota_summary()

            async def on_batch(_name: str, batch: list[ScrapedItem]) -> None:
                async with batch_lock:
                    if _name == "portwatch":
                        accepted, rejected = list(batch), []
                    else:
                        accepted, rejected = rf.filter_items(batch)
                    for it in accepted:
                        if not it.playability_status:
                            it.playability_status = infer_simple_status(
                                it, str(it.extra.get("readme_excerpt") or "")
                            )
                    new_items = self.storage.save_new_items(accepted)
                    with self._lock:
                        self.total_scraped += len(batch)
                        self.total_rejected += len(rejected)
                        self.total_new += len(new_items)
                        auto_on = self.auto_download_enabled and not self.silent_mode
                        auto_drive = self.auto_download_drive
                        auto_min = self.auto_download_min_score

                    if auto_on and new_items:
                        gpu_prof = self.gpu_profile
                        for it in new_items:
                            if it.relevance_score < auto_min:
                                continue
                            it_gpus = list(it.extra.get("portwatch_gpu") or [])
                            if not self._gpu_compatible(it_gpus, f"{it.title} {it.url}", gpu_prof):
                                continue
                            _track, role, _deps = classify_role_and_deps(it)
                            target_url = it.direct_links[0].url if it.direct_links else it.url
                            try:
                                res = await self._download_and_replace_async(
                                    url=target_url,
                                    role=role,
                                    drive=auto_drive,
                                    client=client,
                                    item_id=it.id,
                                    replace_old=True,
                                )
                                if res.get("ok"):
                                    rem = res.get("removed_previous") or []
                                    self._add_auto_log(
                                        f"🤖 Auto-DL ({gpu_prof}) [{role}] {it.title} ({it.relevance_score}/100) — "
                                        f"{len(rem)} ancien(s) remplacé(s)"
                                    )
                            except Exception as exc:
                                self._add_auto_log(f"⚠️ Auto-DL échoué pour {it.title}: {exc}")

                    if self.config.notifications.windows_toast_on_milestone and new_items:
                        for m_it in [it for it in new_items if is_milestone_item(it)][:3]:
                            send_windows_toast(
                                f"VI Milestone Reached: {m_it.playability_status or 'Release / TOML'}",
                                f"{m_it.title} ({m_it.relevance_score}/100)",
                            )

                    if new_items:
                        md_items = self.storage.get_top_items(limit=100, min_score=40)
                        dl_table = self.storage.get_direct_links(limit=50)
                        update_daily_markdown_report(
                            md_items,
                            BASE_DIR / self.config.general.reports_dir,
                            direct_links_table=dl_table,
                            total_scraped=self.total_scraped,
                            total_rejected=self.total_rejected,
                            total_stored=self.storage.stats()["total"],
                        )

            while not self._stop_event.is_set():
                with self._lock:
                    self.cycle += 1
                    self.watch_state = "running"
                    mode_lbl = "🌙 Veille silencieuse" if self.silent_mode else "🟢 Veille active"
                    self.last_message = f"{mode_lbl} — Cycle #{self.cycle} en cours..."
                    for s in parallel_scrapers:
                        self.scraper_statuses[s.name] = "démarrage..."
                    if forum_scraper is not None:
                        self.scraper_statuses[forum_scraper.name] = "en attente..."

                await asyncio.gather(
                    *(s.fetch(on_batch=on_batch, on_status=on_status) for s in parallel_scrapers),
                    return_exceptions=True,
                )
                if forum_scraper is not None and not self._stop_event.is_set():
                    await forum_scraper.fetch(on_batch=on_batch, on_status=on_status)

                # Mise à jour automatique des 3 Piliers critiques compatibles avec le profil GPU (ex: NVIDIA)
                if not self._stop_event.is_set():
                    with self._lock:
                        auto_on = self.auto_download_enabled and not self.silent_mode
                        auto_drive = self.auto_download_drive
                        gpu_prof = self.gpu_profile
                    if auto_on:
                        try:
                            state_now = self.get_cockpit_state(gpu=gpu_prof)
                            for pil in state_now.get("pillars") or []:
                                best = pil.get("best")
                                if (
                                    pil.get("update_suggested")
                                    and best
                                    and best.get("gpu_ok", True)
                                    and best.get("download_url")
                                ):
                                    res = await self._download_and_replace_async(
                                        url=best["download_url"],
                                        role=best["role"],
                                        drive=auto_drive,
                                        client=client,
                                        item_id=best["id"],
                                        replace_old=True,
                                    )
                                    if res.get("ok"):
                                        rem = res.get("removed_previous") or []
                                        self._add_auto_log(
                                            f"🤖 Auto-MàJ Pilier ({gpu_prof}) [{pil['title']}] → "
                                            f"{best['title']} ({len(rem)} ancien(s) remplacé(s))"
                                        )
                        except Exception as exc:
                            self._add_auto_log(f"⚠️ Auto-MàJ Piliers : {exc}")

                if self._stop_event.is_set():
                    break

                delay = (
                    self.silent_interval_minutes * 60
                    if self.silent_mode
                    else self.config.general.cycle_delay_seconds
                )
                for sec in range(delay, 0, -1):
                    if self._stop_event.is_set():
                        break
                    if sec % 5 == 0 or sec <= 10:
                        rem_txt = f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"
                        with self._lock:
                            self.last_message = (
                                f"{'🌙 Veille silencieuse' if self.silent_mode else '🟢 Cycle #' + str(self.cycle)} "
                                f"terminé — prochain dans {rem_txt}"
                            )
                    await asyncio.sleep(1.0)

    @staticmethod
    def _detect_sandboxie_box(sb_dir: Path) -> str | None:
        for ini_path in (Path(r"C:\Windows\Sandboxie.ini"), sb_dir / "Sandboxie.ini"):
            if not ini_path.exists():
                continue
            try:
                raw = ini_path.read_bytes()
                text = (
                    raw.decode("utf-16", errors="ignore")
                    if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                    else raw.decode("utf-8", errors="ignore")
                )
                for line in text.splitlines():
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        sec = s[1:-1].strip()
                        if sec and not sec.startswith(("GlobalSettings", "UserSettings_", "Template_")):
                            return sec
            except OSError:
                continue
        return None

    def launch_inspection_vm(self, drive: str) -> dict[str, Any]:
        target = self._resolve_drive_target(drive)
        if target is None:
            return {"ok": False, "error": f"Drive '{drive}' not found."}
        qdir = target / "by_role"
        qdir.mkdir(parents=True, exist_ok=True)
        backends = detect_available_backends()

        if backends.get("windows_sandbox"):
            wsb_path = generate_inspection_sandbox_wsb(target)
            subprocess.Popen([str(backends["windows_sandbox"]), str(wsb_path)])
            return {"ok": True, "launched": True, "message": "Windows Sandbox launched!"}

        if backends.get("sandboxie"):
            sb_start = Path(str(backends["sandboxie"]))
            box_name = self._detect_sandboxie_box(sb_start.parent)
            if not box_name:
                sandman = sb_start.parent / "SandMan.exe"
                if sandman.exists():
                    subprocess.Popen([str(sandman)])
                return {
                    "ok": True,
                    "launched": False,
                    "message": "Finish the Sandboxie-Plus wizard to create DefaultBox, then click again.",
                }
            subprocess.Popen([str(sb_start), f"/box:{box_name}", "explorer.exe", str(qdir)])
            return {"ok": True, "launched": True, "message": f"Sandboxie-Plus ({box_name}) opened on {qdir}!"}

        return {
            "ok": True,
            "launched": False,
            "message": "Install Sandboxie-Plus (winget install Sandboxie.Plus).",
        }

    def list_all_quarantine(self, drive: str) -> dict[str, Any]:
        target = self._resolve_drive_target(drive)
        if target is None:
            return {"ok": False, "error": "Disque introuvable."}
        files = list_quarantine_files(target)
        by_role = target / "by_role"
        if by_role.exists():
            for rdir in sorted(by_role.iterdir()):
                if rdir.is_dir():
                    for f in list_quarantine_files(rdir):
                        f["folder"] = f"by_role/{rdir.name}/{f['folder']}"
                        files.append(f)
        return {"ok": True, "target_dir": str(target), "files": files}

    @staticmethod
    def _track_matches(item_track: str, filter_track: str, text_hint: str = "") -> bool:
        ft = (filter_track or "all").strip().lower()
        if ft in {"", "all", "tout"}:
            return True
        if ft == "recomp":
            return item_track in {"PC Natif / Recomp", "Commun (RPF / Outils)"}
        if ft == "xbox":
            return item_track in {"Exécution Dump / Ému", "Commun (RPF / Outils)"}
        if ft == "ps5":
            if item_track == "Commun (RPF / Outils)":
                return True
            if item_track == "Exécution Dump / Ému":
                if not text_hint:
                    return True
                low = text_hint.lower()
                return any(k in low for k in ("ps5", "ps4", "shadps4", "kyty", "fpps4", "orbis", "prospero", "libsce", "gnmx", "fself", "eboot.bin", "sprx", "pkg"))
            return False
        return True

    @staticmethod
    def _gpu_compatible(gpus: list[str], title_or_url: str, gpu_profile: str) -> bool:
        prof = (gpu_profile or "NVIDIA").upper()
        if prof == "ALL":
            return True
        gset = {g.upper() for g in (gpus or [])}
        if gset:
            return prof in gset
        low = (title_or_url or "").lower()
        amd_only_hints = ("belobug", "7900xtx", "radeon", "amd-only", "dexnom/victory", "marvisak/victory")
        nv_only_hints = ("zettrox/ztrx", "vitoriabr", "victory-fork")
        if prof == "NVIDIA" and any(h in low for h in amd_only_hints):
            return False
        if prof == "AMD" and any(h in low for h in nv_only_hints):
            return False
        return True

    @staticmethod
    def _extract_version_label(rec: dict[str, Any] | None, fallback_date: str = "") -> str:
        if rec:
            fn = str(rec.get("filename") or "")
            dt = str(rec.get("downloaded_at") or "")[:10]
            m_v = re.search(r"(v\d+(?:\.\d+)+)", fn, re.IGNORECASE)
            if m_v:
                return f"{m_v.group(1)} ({dt})" if dt else m_v.group(1)
            return f"Installed ({dt})" if dt else "Installed"
        return f"Rev. {fallback_date[:10]}" if fallback_date and fallback_date != "N/A" else "Latest rev."

    @staticmethod
    def _scrub_brand(text: str) -> str:
        return re.sub(r"(?i)\b(?:grand\s*theft\s*auto\s*vi|gta\s*vi|gta\s*6|gta6)\b", "VI", text or "").strip()

    def _build_online_news(self, pw_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Builds the Online Tweets, Video Clips & Articles feed from PortWatch VI, live GitHub cache, and verified milestones."""
        online_kw = ("multiplayer", "online", "co-op", "coop", "voice", "server", "netcode", "sync", "fivem", "cfx", "ragemp", "alt:v", "mp ")
        feed: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def _push(item: dict[str, Any]) -> None:
            t_clean = self._scrub_brand(str(item.get("title") or ""))
            url_clean = str(item.get("url") or "https://portwatch-vi.xyz/#top")
            if not t_clean:
                return
            key = f"{item.get('kind', 'article')}:{t_clean.lower()[:64]}"
            if key in seen_keys:
                return
            seen_keys.add(key)
            body_raw = item.get("body")
            if isinstance(body_raw, list):
                body_str = "\n\n".join(self._scrub_brand(p) for p in body_raw if p)
            else:
                body_str = self._scrub_brand(str(body_raw or ""))
            sources_clean = []
            for s in (item.get("sources") or []):
                if isinstance(s, dict) and s.get("url"):
                    sources_clean.append(
                        {
                            "outlet": str(s.get("outlet") or "Link"),
                            "title": self._scrub_brand(str(s.get("title") or s.get("outlet") or "Source")),
                            "url": str(s["url"]),
                        }
                    )
            feed.append(
                {
                    "kind": str(item.get("kind") or "article"),
                    "handle": str(item.get("handle") or ""),
                    "date": str(item.get("date") or ""),
                    "category": str(item.get("category") or "ONLINE").upper(),
                    "source": str(item.get("source") or "Community"),
                    "title": t_clean,
                    "dek": self._scrub_brand(str(item.get("dek") or "")),
                    "body": body_str,
                    "url": url_clean,
                    "has_video": bool(item.get("has_video", False)),
                    "video_label": self._scrub_brand(str(item.get("video_label") or "")),
                    "sources": sources_clean,
                    "highlight": bool(item.get("highlight", False)),
                }
            )

        # 1. Verified X / Twitter Video Clips (from PortWatch pinned & live sources)
        pinned = pw_snapshot.get("pinned") or {}
        pinned_x_url = "https://x.com/4lwayz_/status/2126083366600724566"
        pinned_date = "2026-11-26"
        if isinstance(pinned, dict) and pinned.get("text"):
            ptxt = str(pinned["text"])
            m_url = re.search(r"https?://x\.com/\S+", ptxt)
            if m_url:
                pinned_x_url = m_url.group(0)
            if pinned.get("updated"):
                pinned_date = str(pinned["updated"])[:10]

        _push(
            {
                "kind": "tweet",
                "handle": "@TeKGameR950",
                "date": "2026-11-26",
                "category": "X VIDEO CLIP",
                "source": "X / Twitter",
                "title": "Working PC Multiplayer Clip: Players, Vehicles & Proximity Voice Chat",
                "dek": "TeK posted a live PC multiplayer demonstration showing synced player characters, drivable vehicles, and working proximity voice chat running at a stable 25–30 FPS.",
                "body": "TeK (@TeKGameR950) shared video footage on X demonstrating experimental PC multiplayer running on a private fork: multiple players moving together, synced vehicles, and proximity voice chat at 25–30 FPS. PortWatch VI independently verified the video.",
                "url": "https://x.com/TeKGameR950/status/2126015173274312796",
                "has_video": True,
                "video_label": "Watch Multiplayer & Proximity Voice Clip on X",
                "highlight": True,
            }
        )
        _push(
            {
                "kind": "tweet",
                "handle": "@4lwayz_",
                "date": pinned_date,
                "category": "X VERIFIED",
                "source": "PortWatch VI · X",
                "title": "PortWatch VI Verification: PC Multiplayer & Proximity Voice Confirmed Real",
                "dek": "Multiplayer on PC is real — PortWatch confirmed players, cars, and proximity voice chat running at a stable 25–30 FPS in the shared demonstration clip.",
                "body": "PortWatch VI maintainer @4lwayz_ confirmed verification of the PC multiplayer demonstration showing synchronized players, cars, and proximity voice chat at 25–30 FPS.",
                "url": pinned_x_url,
                "has_video": True,
                "video_label": "Watch Verification Thread & Clip on X",
                "highlight": True,
            }
        )

        # 2. Live PortWatch VI Articles (with full body + video sources)
        for n in (pw_snapshot.get("news") or []):
            blob = f"{n.get('title', '')} {n.get('dek', '')}".lower()
            if any(k in blob for k in online_kw):
                srcs = n.get("sources") or []
                has_vid = any("video" in str(s.get("title", "")).lower() or "x.com" in str(s.get("url", "")).lower() for s in srcs) or "video" in blob
                first_url = srcs[0]["url"] if (srcs and isinstance(srcs[0], dict) and srcs[0].get("url")) else "https://portwatch-vi.xyz/#top"
                _push(
                    {
                        "kind": "article",
                        "date": str(n.get("date") or ""),
                        "category": "VIDEO REPORT" if has_vid else "ARTICLE",
                        "source": "PortWatch VI",
                        "title": str(n.get("title") or ""),
                        "dek": str(n.get("dek") or ""),
                        "body": n.get("body") or str(n.get("dek") or ""),
                        "url": first_url,
                        "has_video": has_vid,
                        "video_label": "Includes verified X & Reddit video clips" if has_vid else "",
                        "sources": srcs or [
                            {"outlet": "X Video", "title": "TeK multiplayer video on X", "url": "https://x.com/TeKGameR950/status/2126015173274312796"},
                            {"outlet": "Reddit Clip", "title": "Community mirror thread on Reddit", "url": "https://www.reddit.com/r/FitGirlRepack/comments/1y6drp0/gta_6_multiplayer_before_gta_6_multiplayer/"},
                        ],
                        "highlight": True,
                    }
                )

        # 3. Cached live GitHub online/multiplayer projects
        cache_p = BASE_DIR / "data" / "online_news_cache.json"
        if cache_p.exists():
            try:
                cached = json.loads(cache_p.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    for c_it in cached:
                        if isinstance(c_it, dict):
                            _push({**c_it, "kind": "article"})
            except Exception:
                pass

        # 4. Curated verified ecosystem articles & video discussions
        curated: tuple[dict[str, Any], ...] = (
            {
                "kind": "article",
                "date": "2026-11-26",
                "category": "VIDEO REPORT",
                "source": "Reddit · Community Clip",
                "title": "Community Mirror & Breakdown: 25–30 FPS PC Multiplayer & Voice Chat Footage",
                "dek": "Full community discussion and mirrored gameplay clip breaking down how entity sync, drivable vehicles, and proximity voice chat behave on the experimental PC runtime.",
                "body": "Following TeK's initial X post, the community mirrored the video clip on Reddit to analyze player movement sync, vehicle interpolation, and proximity voice audio running on top of an experimental VIctory/XCL PC compatibility build.",
                "url": "https://www.reddit.com/r/FitGirlRepack/comments/1y6drp0/gta_6_multiplayer_before_gta_6_multiplayer/",
                "has_video": True,
                "video_label": "Watch Mirrored Gameplay Clip & Discussion",
                "sources": [
                    {"outlet": "Reddit Video", "title": "Watch clip on Reddit", "url": "https://www.reddit.com/r/FitGirlRepack/comments/1y6drp0/gta_6_multiplayer_before_gta_6_multiplayer/"},
                    {"outlet": "PortWatch VI", "title": "PortWatch VI Report", "url": "https://portwatch-vi.xyz/#top"},
                ],
                "highlight": False,
            },
            {
                "kind": "article",
                "date": "2026-11-20",
                "category": "ECOSYSTEM",
                "source": "Cfx.re / FiveM",
                "title": "Multiplayer Framework Consolidation: Cfx.re / FiveM Stands as the Primary RP Standard",
                "dek": "Following the shutdown of alternative frameworks (alt:V and RAGE:MP), Cfx.re / FiveM and the Cfx Marketplace form the blueprint for future official PC custom servers.",
                "body": "With alternative multiplayer clients shutting down under updated platform policies, Cfx.re / FiveM continues rolling out OneSync performance updates and the Cfx Marketplace—laying the groundwork for future custom multiplayer servers.",
                "url": "https://cfx.re/",
                "has_video": False,
                "sources": [{"outlet": "Cfx.re", "title": "Cfx.re Official Hub", "url": "https://cfx.re/"}],
                "highlight": False,
            },
            {
                "kind": "article",
                "date": "2026-11-15",
                "category": "NETCODE R&D",
                "source": "Open-Source Research",
                "title": "Early Clean-Room Co-Op & LAN Session Sync Research on Community Runtimes",
                "dek": "Open-source runtime contributors are documenting entity state sync, local LAN lobby hooks, and script hooks required before a public co-op framework can be released safely.",
                "body": "Before any public open-source multiplayer or LAN co-op mod can ship, runtime developers are mapping memory hooks for player transforms, vehicle ownership, and safe sandboxing without touching commercial services.",
                "url": "https://portwatch-vi.xyz/#top",
                "has_video": False,
                "sources": [{"outlet": "PortWatch VI", "title": "Track Live Progress", "url": "https://portwatch-vi.xyz/#top"}],
                "highlight": False,
            },
        )
        for item in curated:
            _push(item)

        return feed[:12]

    def sync_online_news(self) -> dict[str, Any]:
        """Refreshes PortWatch VI + searches GitHub for recent open-source VI multiplayer / co-op / online mod projects."""
        self.sync_portwatch()
        found_gh: list[dict[str, Any]] = []
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "VI-Community-Launcher/1.0"}
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        queries = (
            "victory multiplayer OR coop OR netcode",
            "rage9 multiplayer OR online OR server",
        )
        try:
            with httpx.Client(follow_redirects=True, timeout=10.0) as client:
                for q in queries:
                    resp = client.get(
                        "https://api.github.com/search/repositories",
                        params={"q": q, "sort": "updated", "order": "desc", "per_page": 5},
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        continue
                    for repo in (resp.json().get("items") or []):
                        desc = str(repo.get("description") or "").strip()
                        if not desc:
                            continue
                        full = str(repo.get("full_name") or "")
                        found_gh.append(
                            {
                                "date": str(repo.get("pushed_at") or "")[:10],
                                "category": "GITHUB PROJECT",
                                "source": f"GitHub · ★{repo.get('stargazers_count', 0)}",
                                "title": f"{full} — Online / Multiplayer Research",
                                "dek": desc[:220],
                                "url": str(repo.get("html_url") or ""),
                                "highlight": False,
                            }
                        )
            cache_p = BASE_DIR / "data" / "online_news_cache.json"
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            cache_p.write_text(json.dumps(found_gh[:6], indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        pw_snap = load_portwatch_snapshot(PORTWATCH_SNAPSHOT)
        items = self._build_online_news(pw_snap)
        self._add_auto_log(f"🌐 Online modding news refreshed ({len(items)} items).")
        return {"ok": True, "count": len(items), "items": items}

    def get_cockpit_state(
        self, *, search: str = "", track: str = "all", gpu: str | None = None, min_score: int = 40
    ) -> dict[str, Any]:
        active_gpu = (gpu or self.gpu_profile or "NVIDIA").upper()
        items = self.storage.get_top_items(limit=400, min_score=min_score)
        dl_by_url, dl_by_item = self.storage.get_download_history_map()
        pw_snapshot = load_portwatch_snapshot(PORTWATCH_SNAPSHOT)
        for _le in (pw_snapshot.get("leaderboard") or []):
            if isinstance(_le, dict) and _le.get("status"):
                _st = str(_le["status"]).replace("En développement", "In Development").replace("Rendu ", "Render ")
                _le["status"] = _st
        pw_by_repo = {
            (e.get("repo") or "").lower(): e
            for e in (pw_snapshot.get("leaderboard") or [])
            if e.get("repo")
        }

        by_role: dict[str, list[dict[str, Any]]] = {r: [] for r, _ in ALL_ROLES}
        all_rows: list[dict[str, Any]] = []
        milestones_feed: list[dict[str, Any]] = []
        q = search.strip().lower()

        for it in items:
            item_track, role, deps = classify_role_and_deps(it)
            rec = dl_by_item.get(it.id) or dl_by_url.get(it.url)
            if rec is None:
                for dl in it.direct_links:
                    if dl.url in dl_by_url:
                        rec = dl_by_url[dl.url]
                        break
            is_dl = rec is not None
            date_str = it.effective_date.astimezone().strftime("%Y-%m-%d %H:%M") if it.effective_date else "N/A"
            date_utc = it.effective_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if it.effective_date else ""
            dl_utc = str(rec["downloaded_at"])[:16].replace("T", " ") if rec else ""
            best_url = it.direct_links[0].url if it.direct_links else it.url
            best_label = it.direct_links[0].display_label if it.direct_links else it.title.split("/")[-1]

            pw_entry = pw_by_repo.get(it.title.lower())
            item_gpus = list(pw_entry.get("gpu") or []) if pw_entry else list(it.extra.get("portwatch_gpu") or [])
            pw_score = float(pw_entry.get("score_pct") or 0.0) if pw_entry else float(it.extra.get("portwatch_score") or 0.0)
            gpu_ok = self._gpu_compatible(item_gpus, f"{it.title} {it.url}", active_gpu)

            m_tag = re.search(r"\b(v\d+(?:\.\d+)+)\b", f"{it.title} {best_label}", re.IGNORECASE)
            ver_lbl = (
                f"{m_tag.group(1)} ({date_str[:10]})"
                if m_tag
                else (f"PortWatch {pw_score}% ({date_str[:10]})" if pw_score > 0 else f"Rev. {date_str[:10]}")
            )
            clean_status = (it.playability_status or "—").replace("En développement", "In Development").replace("Rendu ", "Render ")

            entry = {
                "id": it.id,
                "score": it.relevance_score,
                "pw_score": pw_score,
                "pw_verified": bool(it.extra.get("portwatch_verified") or pw_entry),
                "gpu": item_gpus,
                "gpu_ok": gpu_ok,
                "status": clean_status,
                "role": role,
                "track": item_track,
                "dependencies": deps,
                "source": it.source.value,
                "title": it.title,
                "summary": (it.summary or "")[:180],
                "url": it.url,
                "download_url": best_url,
                "download_label": best_label,
                "has_direct_file": bool(it.direct_links),
                "downloaded": is_dl,
                "downloaded_at": dl_utc,
                "installed_version": self._extract_version_label(rec, date_str) if is_dl else "",
                "version_label": ver_lbl,
                "date": date_str,
                "date_utc": date_utc,
                "is_milestone": is_milestone_item(it),
            }
            if role in by_role:
                by_role[role].append(entry)
            if self._track_matches(item_track, track, f"{it.title} {it.summary} {' '.join(deps)}"):
                if entry["is_milestone"] and len(milestones_feed) < 8:
                    milestones_feed.append(entry)
                if not q or q in f"{it.title} {role} {it.playability_status} {' '.join(deps)} {' '.join(item_gpus)}".lower():
                    all_rows.append(entry)

        def _rank_candidates(cands_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Filtrer les builds incompatibles GPU (ex: AMD-only en profil NVIDIA) sauf s'il n'y a rien d'autre
            compat = [c for c in cands_in if c["gpu_ok"]]
            pool = compat if compat else cands_in
            return sorted(
                pool,
                key=lambda x: (
                    1 if (x["pw_score"] > 0 and x["gpu_ok"]) else 0,
                    x["pw_score"],
                    1 if x.get("pw_verified") else 0,
                    x["score"],
                    1 if x["has_direct_file"] else 0,
                    x["date"],
                ),
                reverse=True,
            )

        roles_table: list[dict[str, Any]] = []
        for role, role_track in ALL_ROLES:
            if not self._track_matches(role_track, track):
                continue
            raw_cands = by_role.get(role, [])
            if (track or "").strip().lower() == "ps5":
                ps5_only = [
                    c for c in raw_cands
                    if self._track_matches(c["track"], "ps5", f"{c['title']} {c['summary']} {' '.join(c['dependencies'])}")
                ]
                raw_cands = ps5_only or raw_cands
            cands = _rank_candidates(raw_cands)
            current_dl = next((c for c in raw_cands if c["downloaded"]), None)
            best_cand = cands[0] if cands else None

            update_suggested = False
            if best_cand and current_dl is None:
                update_suggested = True
            elif best_cand and current_dl:
                if best_cand["id"] != current_dl["id"] and (
                    best_cand["pw_score"] > current_dl["pw_score"] or best_cand["score"] > current_dl["score"]
                ):
                    update_suggested = True
                elif best_cand["id"] == current_dl["id"] and best_cand["date_utc"] > current_dl["downloaded_at"]:
                    update_suggested = True

            role_backup_dir = Path(f"{self.auto_download_drive}:\\") / "GTA6_Sandbox_Downloads" / "by_role" / _safe_role_folder(role) / "_backup"
            has_backup = role_backup_dir.exists() and any(role_backup_dir.iterdir())
            roles_table.append(
                {
                    "role": role,
                    "track": role_track,
                    "current": current_dl,
                    "best": best_cand,
                    "missing": current_dl is None,
                    "update_suggested": update_suggested,
                    "has_backup": has_backup,
                    "total_found": len(cands),
                }
            )

        # Piliers Critiques (respectant le profil GPU NVIDIA + classement PortWatch VI)
        all_roles_map = {}
        for role, role_track in ALL_ROLES:
            raw_cands = by_role.get(role, [])
            cands = _rank_candidates(raw_cands)
            cur = next((c for c in raw_cands if c["downloaded"]), None)
            best = cands[0] if cands else None
            upd = bool(
                best
                and (
                    cur is None
                    or (best["id"] != cur["id"] and (best["pw_score"] > cur["pw_score"] or best["score"] > cur["score"]))
                    or (best["id"] == cur["id"] and best["date_utc"] > cur["downloaded_at"])
                )
            )
            all_roles_map[role] = {"current": cur, "best": best, "update_suggested": upd}

        # Compiled local runtimes on disk (used to detect if Pillar 1 is already up to date locally)
        compiled_specs = [s for s in self._get_direct_runtime_specs() if Path(s["exe"]).exists()]

        def _local_runtime_up_to_date(best_item: dict[str, Any] | None) -> bool:
            if not best_item:
                return False
            hay = f"{best_item.get('title', '')} {best_item.get('url', '')}".lower()
            best_dt = str(best_item.get("date_utc") or "")
            for sp in compiled_specs:
                aliases = tuple(a.lower() for a in sp.get("aliases") or ())
                base_id = str(sp.get("id") or "").lower().removeprefix("lab_").removesuffix("_lab")
                match_keys = (*aliases, base_id)
                if any(k and k in hay for k in match_keys):
                    try:
                        mtime_utc = datetime.fromtimestamp(Path(sp["exe"]).stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                        if not best_dt or mtime_utc >= best_dt:
                            return True
                    except OSError:
                        continue
            return False

        def _pillar_card(title: str, subtitle: str, role_names: list[str], extra_note: str = "") -> dict[str, Any]:
            matched = [all_roles_map[r] for r in role_names if r in all_roles_map]
            best = max(
                (m["best"] for m in matched if m["best"]),
                key=lambda x: (x.get("pw_score", 0.0), x["score"]),
                default=None,
            )
            current = next((m["current"] for m in matched if m["current"]), None)
            local_ok = _local_runtime_up_to_date(best)
            # Only suggest update if best candidate is neither downloaded-up-to-date nor already compiled locally up-to-date
            upd = bool(
                best
                and not local_ok
                and (
                    not best["downloaded"]
                    or (best.get("date_utc") and best["date_utc"] > best.get("downloaded_at", ""))
                )
            )
            return {
                "title": title,
                "subtitle": subtitle,
                "current": current,
                "best": best,
                "update_suggested": upd,
                "extra_note": extra_note,
            }

        dump_status = self._inspect_dump_status()
        dump_exists = bool(dump_status["ready"])
        ft = (track or "all").strip().lower()
        if ft == "recomp":
            pillars = [
                _pillar_card(
                    "1. Static Recompiler & Native PC Exe",
                    "Static recompilation, elf2cpp, LLVM, native PC runtime",
                    ["3. Recompilateur Statique", "1. Portage / Exe PC"],
                ),
                _pillar_card(
                    "2. symbols.toml + RPF9 / RAGE 9",
                    "Function mapping & RPF9 archive extractor",
                    ["4. Symboles / Config", "5. Outil RPF / RAGE 9"],
                ),
                _pillar_card(
                    "3. Decompiler / AI RE & Local Dump",
                    "Ghidra / IDA / LLM4Decompile + user local dump",
                    ["8. Décompilateur / IA RE"],
                    extra_note="Local dump verified ✅" if dump_exists else "Import your local dump in Settings",
                ),
            ]
        elif ft == "xbox":
            pillars = [
                _pillar_card(
                    "1. Console Compatibility Runtime (Scarlett)",
                    "cool_fox, ScarlettPort, VIctory, VItoriaBr, d3d12_xs",
                    ["2. Émulateur / Runtime"],
                ),
                _pillar_card(
                    "2. DX12 Shaders / GPU & RPF9",
                    "DirectX 12, DXIL, shader cache & RPF9 tools",
                    ["6. Shaders / GPU", "5. Outil RPF / RAGE 9"],
                ),
                _pillar_card(
                    "3. Local Dump & Tools (XVD / game_scarlett_master)",
                    "XVD/ELF dump tools + user local dump",
                    ["7. Dumper / ELF-SPRX"],
                    extra_note="Local dump verified ✅" if dump_exists else "Import your local dump in Settings",
                ),
            ]
        else:
            pillars = [
                _pillar_card(
                    "1. Console Runtime / PC Recomp",
                    "cool_fox, ScarlettPort, VIctory, d3d12_xs, static recomp",
                    ["2. Émulateur / Runtime", "1. Portage / Exe PC", "3. Recompilateur Statique"],
                ),
                _pillar_card(
                    "2. symbols.toml + RPF9 / RAGE 9",
                    "game_scarlett_master.exe / eboot.bin mapping + RPF9",
                    ["4. Symboles / Config", "5. Outil RPF / RAGE 9"],
                ),
                _pillar_card(
                    "3. Local Dump & Shader/XVD Tools",
                    "FSELF / XVD / DX12 shader tools + user local dump",
                    ["7. Dumper / ELF-SPRX", "6. Shaders / GPU"],
                    extra_note="Local dump verified ✅" if dump_exists else "Import your local dump in Settings",
                ),
            ]

        import time as _time

        now_ts = _time.monotonic()
        if now_ts - self._sys_cache_ts > 300.0:
            self._cached_drives = list_local_drives()
            self._cached_backends = detect_available_backends()
            self._sys_cache_ts = now_ts

        drives = [
            {"letter": d.letter, "root": d.root, "free_gb": d.free_gb, "total_gb": d.total_gb}
            for d in self._cached_drives
        ]
        discovered_list = load_discovered_repos(DISCOVERED_CACHE)
        backends = self._cached_backends
        neonbridge_ok = any(
            (r / "Neonbridge_VM" / "app" / "Neonbridge" / "neonbridge.exe").exists()
            or (r / "neonbridge.exe").exists()
            for r in self._discover_runtime_roots(str(self.settings.get("runtimes_dir") or ""))
        )
        pagiii_ok = (BASE_DIR / "Lancer_Pagiii_Tracker.bat").exists()
        # Default button is PLAY; only switch to UPDATE after an update check found undownloaded pillar updates
        pending_pillars = [
            p for p in pillars[:2]
            if p.get("update_suggested") and p.get("best") is not None
        ]
        has_updates = bool(self._updates_checked and len(pending_pillars) > 0)
        installed_runtimes = [s["label"] for s in compiled_specs]
        installed_low = {r.lower() for r in installed_runtimes}
        tracked_runtimes: list[str] = []
        for e in (pw_snapshot.get("leaderboard") or [])[:8]:
            name = str(e.get("name") or e.get("repo") or "").strip()
            if name and e.get("repo") and self._gpu_compatible(e.get("gpu") or [], e.get("repo") or "", active_gpu):
                if not any(name.lower() in ir for ir in installed_low):
                    tracked_runtimes.append(name)
        tracked_runtimes.extend(["shadPS4 (PS5 Emu)", "Kyty (PS5 Emu)", "PC Recomp"])
        tracked_runtimes = list(dict.fromkeys(tracked_runtimes))
        available_runtimes = list(dict.fromkeys([*installed_runtimes, *tracked_runtimes]))
        mods_state = self.mods.get_state(portwatch_tools=pw_snapshot.get("tools") or [])
        online_news = self._build_online_news(pw_snapshot)
        show_logs_on = bool(self.settings.get("show_logs"))
        log_snap = self._read_game_log_tail(include_lines=show_logs_on)
        sys_mem = self._get_system_memory()
        saves_info = self.get_saves_info()

        with self._lock:
            prewarm_info = {
                "state": self._prewarm_state,
                "detail": self._prewarm_detail,
                "runtime": self._prewarm_runtime,
            }
            return {
                "watch_state": self.watch_state,
                "running": self.running,
                "silent_mode": self.silent_mode,
                "silent_interval_minutes": self.silent_interval_minutes,
                "cycle": self.cycle,
                "quota": self.quota_summary,
                "last_message": self.last_message,
                "auto_download_enabled": self.auto_download_enabled,
                "auto_download_drive": self.auto_download_drive,
                "auto_download_min_score": self.auto_download_min_score,
                "gpu_profile": active_gpu,
                "settings": dict(self.settings),
                "has_updates": has_updates,
                "updates_checked": self._updates_checked,
                "last_update_check": self._last_update_check,
                "pending_updates_count": len(pending_pillars),
                "startup_sync": {
                    "done": self._startup_sync_done,
                    "stage": self._startup_stage,
                },
                "last_crash": dict(self._last_crash) if self._last_crash else None,
                "dump_status": dump_status,
                "available_runtimes": available_runtimes,
                "runtime_groups": {"installed": installed_runtimes, "tracked": tracked_runtimes},
                "system_memory": sys_mem,
                "prewarm": prewarm_info,
                "saves_info": saves_info,
                "game_status": {
                    "running": log_snap["running"],
                    "pid": log_snap["pid"],
                    "runtime": log_snap["runtime"],
                    "live_fps": log_snap["live_fps"],
                    "live_draws": log_snap["live_draws"],
                    "error_count": log_snap["error_count"],
                    "warn_count": log_snap["warn_count"],
                },
                "mods": mods_state,
                "online_news": online_news,
                "auto_logs": list(self.auto_logs[-20:]),
                "game_logs": log_snap if show_logs_on else None,
                "pillars": pillars,
                "roles_table": roles_table,
                "items": all_rows[:120],
                "milestones_feed": milestones_feed,
                "drives": drives,
                "boot_summary": dict(self._boot_cache),
                "discovered_count": len(discovered_list),
                "portwatch": pw_snapshot,
                "local_tools": {
                    "neonbridge_ready": neonbridge_ok,
                    "pagiii_ready": pagiii_ok,
                    "sandboxie_ready": bool(backends.get("sandboxie")),
                },
            }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VI Community Launcher — Runtime Manager</title>
  <style>
    :root {
      --bg: #090a12;
      --surface: #111322;
      --surface-2: #171a2e;
      --glass: rgba(20, 22, 38, 0.78);
      --border: rgba(255, 255, 255, 0.08);
      --border-hi: rgba(244, 114, 182, 0.35);
      --text: #f3f4f6;
      --muted: #94a3b8;
      --sunset: linear-gradient(90deg, #ff8a3d 0%, #f43f8e 55%, #a855f7 100%);
      --cyan: #38bdf8;
      --pink: #f472b6;
      --amber: #fbbf24;
      --green: #34d399;
      --red: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      background:
        radial-gradient(circle at 18% 12%, rgba(168, 85, 247, 0.18), transparent 40%),
        radial-gradient(circle at 82% 18%, rgba(244, 63, 142, 0.14), transparent 42%),
        radial-gradient(circle at 50% 95%, rgba(255, 138, 61, 0.10), transparent 45%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 224px minmax(0, 1fr) 276px;
      min-height: 100vh;
      max-width: 1620px;
      margin: 0 auto;
    }
    @media (max-width: 1180px) {
      .app-shell { grid-template-columns: 200px minmax(0, 1fr); }
      .right-rail { display: none; }
    }
    @media (max-width: 780px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar { display: none; }
    }

    /* Left Sidebar */
    .sidebar {
      background: rgba(13, 15, 26, 0.88);
      border-right: 1px solid var(--border);
      padding: 20px 14px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 4px 8px;
    }
    .brand-badge {
      width: 34px; height: 34px; border-radius: 10px;
      background: var(--sunset);
      display: grid; place-items: center;
      font-weight: 900; font-size: .95rem; letter-spacing: -.5px;
      box-shadow: 0 6px 20px rgba(244, 63, 142, 0.35);
    }
    .brand-text { font-weight: 800; font-size: 1rem; letter-spacing: .6px; }
    .brand-sub { font-size: .68rem; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; }

    .nav-list { display: flex; flex-direction: column; gap: 5px; }
    .nav-btn {
      width: 100%;
      text-align: left;
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      padding: 10px 12px;
      border-radius: 12px;
      font-weight: 600;
      font-size: .86rem;
      display: flex;
      align-items: center;
      gap: 10px;
      cursor: pointer;
      transition: all .16s ease;
    }
    .nav-btn:hover { color: var(--text); background: rgba(255,255,255,.04); }
    .nav-btn.active {
      color: #fff;
      background: linear-gradient(90deg, rgba(244,63,142,.22), rgba(168,85,247,.14));
      border-color: rgba(244,114,182,.35);
      box-shadow: 0 4px 16px rgba(244,63,142,.15);
    }
    .nav-sep { height: 1px; background: var(--border); margin: 4px 8px; }

    .side-footer {
      margin-top: auto;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-size: .78rem;
    }
    .side-footer select {
      width: 100%;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 8px;
      font-size: .78rem;
    }

    /* Center Stage */
    .main {
      padding: 18px 24px 36px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      overflow-x: hidden;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
    }
    .search-wrap {
      flex: 1;
      min-width: 210px;
      max-width: 360px;
      position: relative;
    }
    .search-wrap input {
      width: 100%;
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 9px 14px 9px 36px;
      color: var(--text);
      font-size: .84rem;
      outline: none;
    }
    .search-wrap input:focus { border-color: var(--pink); }
    .search-ico {
      position: absolute; left: 13px; top: 50%; transform: translateY(-50%);
      color: var(--muted); font-size: .85rem; pointer-events: none;
    }

    .seg-group {
      display: inline-flex;
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 3px;
      gap: 3px;
    }
    .seg-btn {
      border: none;
      background: transparent;
      color: var(--muted);
      padding: 6px 13px;
      border-radius: 999px;
      font-size: .8rem;
      font-weight: 700;
      cursor: pointer;
      transition: all .15s;
    }
    .seg-btn:hover { color: #fff; }
    .seg-btn.active {
      background: var(--sunset);
      color: #fff;
      box-shadow: 0 3px 12px rgba(244,63,142,.35);
    }

    .icon-btn {
      background: var(--glass);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 7px 12px;
      border-radius: 999px;
      font-size: .8rem;
      font-weight: 600;
      cursor: pointer;
      transition: border-color .15s;
    }
    .icon-btn:hover { border-color: var(--pink); }

    .status-pill {
      font-size: .76rem; font-weight: 700;
      padding: 6px 12px; border-radius: 999px;
      background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
    }
    .status-pill.on { background: rgba(52,211,153,.16); color: var(--green); border-color: rgba(52,211,153,.4); }
    .status-pill.silent { background: rgba(168,85,247,.2); color: #d8b4fe; border-color: rgba(168,85,247,.45); }

    /* Hero Banner (Vice City Style) */
    .hero {
      position: relative;
      border-radius: 22px;
      padding: 28px 30px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,.11);
      background:
        radial-gradient(circle at 78% 28%, rgba(255, 138, 61, 0.50), transparent 44%),
        radial-gradient(circle at 60% 65%, rgba(244, 63, 142, 0.45), transparent 52%),
        radial-gradient(circle at 20% 25%, rgba(168, 85, 247, 0.42), transparent 55%),
        linear-gradient(135deg, #16102b 0%, #24123b 48%, #121428 100%);
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
      min-height: 218px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 16px;
    }
    .hero-vi-watermark {
      position: absolute;
      right: 28px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 9.5rem;
      font-weight: 900;
      letter-spacing: -6px;
      line-height: 1;
      background: linear-gradient(180deg, rgba(255,255,255,.22), rgba(244,63,142,.06));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      pointer-events: none;
      user-select: none;
    }
    .hero-tag {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: .72rem; font-weight: 800; letter-spacing: 1.4px; text-transform: uppercase;
      color: #fde68a;
    }
    .hero h1 {
      margin: 4px 0 6px;
      font-size: 2rem;
      font-weight: 900;
      letter-spacing: -.4px;
      line-height: 1.08;
      text-shadow: 0 4px 20px rgba(0,0,0,.45);
    }
    .hero h1 span {
      background: var(--sunset);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .hero-sub {
      max-width: 560px;
      font-size: .88rem;
      color: rgba(243,244,246,.82);
      line-height: 1.45;
    }
    .hero-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      position: relative;
      z-index: 2;
    }
    .btn-cta {
      border: none;
      background: var(--sunset);
      color: #fff;
      padding: 10px 22px;
      border-radius: 999px;
      font-weight: 800;
      font-size: .88rem;
      cursor: pointer;
      box-shadow: 0 8px 24px rgba(244,63,142,.4);
      transition: transform .15s, opacity .15s;
    }
    .btn-cta:hover { transform: translateY(-1px); opacity: .95; }
    .btn-cta.stop { background: linear-gradient(90deg, #dc2626, #991b1b); box-shadow: 0 8px 22px rgba(220,38,38,.35); }
    .btn-glass {
      background: rgba(15, 17, 30, 0.55);
      border: 1px solid rgba(255,255,255,.22);
      color: #fff;
      padding: 10px 18px;
      border-radius: 999px;
      font-weight: 700;
      font-size: .85rem;
      cursor: pointer;
      backdrop-filter: blur(8px);
    }
    .btn-glass:hover { border-color: #fff; background: rgba(255,255,255,.12); }

    /* Quick Launch Cards (Recommended for you row) */
    .sec-title {
      display: flex; align-items: center; justify-content: space-between;
      margin: 4px 0 0;
    }
    .sec-title h2 { margin: 0; font-size: 1.02rem; font-weight: 800; letter-spacing: .2px; }
    .sec-title span { font-size: .78rem; color: var(--muted); }

    .shelf {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 980px) { .shelf { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    .tool-card {
      position: relative;
      border-radius: 16px;
      padding: 16px;
      min-height: 128px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      cursor: pointer;
      border: 1px solid rgba(255,255,255,.1);
      transition: transform .16s, border-color .16s;
      overflow: hidden;
    }
    .tool-card:hover { transform: translateY(-3px); border-color: rgba(255,255,255,.32); }
    .tc-1 { background: linear-gradient(145deg, #2b1449, #151029 65%, #3d184b); }
    .tc-2 { background: linear-gradient(145deg, #102a43, #0e172a 65%, #163a4f); }
    .tc-3 { background: linear-gradient(145deg, #3b1d12, #1a1219 65%, #4a2117); }
    .tc-4 { background: linear-gradient(145deg, #0f2f28, #0f1722 65%, #153c34); }
    .tool-ico { font-size: 1.45rem; }
    .tool-name { font-weight: 800; font-size: .92rem; margin-top: 8px; }
    .tool-desc { font-size: .74rem; color: rgba(255,255,255,.72); margin-top: 2px; }

    /* Cards & Tables */
    .pillars { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
    .card {
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 15px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      backdrop-filter: blur(10px);
    }
    .card.upd { border-color: rgba(251, 146, 60, 0.55); }
    .card.ok { border-color: rgba(52, 211, 153, 0.45); }
    .card h3 { margin: 0; font-size: .93rem; font-weight: 800; }
    .muted { color: var(--muted); font-size: .78rem; }

    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
    }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; font-size: .83rem; vertical-align: middle; }
    th { background: rgba(9, 10, 18, 0.72); color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .6px; }
    tr:last-child td { border-bottom: none; }
    a { color: var(--cyan); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .badge { display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: .7rem; font-weight: 700; }
    .b-ok { background: rgba(52,211,153,.16); color: var(--green); border: 1px solid rgba(52,211,153,.35); }
    .b-upd { background: rgba(251,146,60,.18); color: #fdba74; border: 1px solid rgba(251,146,60,.45); }
    .b-miss { background: rgba(248,113,113,.15); color: #fca5a5; border: 1px solid rgba(248,113,113,.35); }
    .b-recomp { background: rgba(56,189,248,.14); color: #7dd3fc; border: 1px solid rgba(56,189,248,.3); }
    .b-xbox { background: rgba(168,85,247,.18); color: #d8b4fe; border: 1px solid rgba(168,85,247,.38); }

    .btn-sm {
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      padding: 5px 10px;
      border-radius: 8px;
      font-size: .76rem;
      font-weight: 700;
      cursor: pointer;
    }
    .btn-sm:hover { border-color: var(--pink); }
    .btn-sm.primary { background: var(--sunset); border: none; color: #fff; }
    .btn-sm.safe { background: rgba(16,185,129,.22); border-color: rgba(52,211,153,.45); color: #6ee7b7; }

    /* Right Rail */
    .right-rail {
      background: rgba(13, 15, 26, 0.85);
      border-left: 1px solid var(--border);
      padding: 20px 16px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .rail-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .rail-title { font-size: .82rem; font-weight: 800; letter-spacing: .3px; display: flex; justify-content: space-between; align-items: center; }
    .gauge-row { display: flex; flex-direction: column; gap: 5px; }
    .gauge-head { display: flex; justify-content: space-between; font-size: .78rem; font-weight: 700; }
    .bar-track { height: 7px; border-radius: 999px; background: rgba(255,255,255,.08); overflow: hidden; }
    .bar-fill-recomp { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #38bdf8, #818cf8); transition: width .35s; }
    .bar-fill-xbox { height: 100%; border-radius: 999px; background: var(--sunset); transition: width .35s; }

    .feed-item {
      display: flex; gap: 9px; align-items: flex-start;
      padding: 8px 0; border-bottom: 1px solid var(--border);
      font-size: .77rem;
    }
    .feed-item:last-child { border-bottom: none; padding-bottom: 0; }
    .feed-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; background: var(--pink); }
    .feed-dot.ok { background: var(--green); }

    /* Views */
    .view { display: none; flex-direction: column; gap: 16px; }
    .view.active { display: flex; }

    /* Dual Boot Gauges */
    .dual-gauges { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
    .big-pct { font-size: 2rem; font-weight: 900; }
    .chk-PASS { color: var(--green); font-weight: 800; }
    .chk-WARN { color: var(--amber); font-weight: 800; }
    .chk-FAIL { color: var(--red); font-weight: 800; }

    .logbox {
      background: #070811;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      font-family: Consolas, monospace;
      font-size: .77rem;
      max-height: 190px;
      overflow-y: auto;
      white-space: pre-wrap;
      color: #cbd5e1;
    }

    /* Modal */
    .modal-bg {
      position: fixed; inset: 0; background: rgba(5, 6, 12, 0.78);
      backdrop-filter: blur(6px);
      display: none; align-items: center; justify-content: center;
      z-index: 60; padding: 18px;
    }
    .modal-bg.open { display: flex; }
    .modal {
      background: var(--surface);
      border: 1px solid var(--border-hi);
      border-radius: 18px;
      width: 100%; max-width: 680px; max-height: 86vh; overflow-y: auto;
      padding: 20px 22px;
      box-shadow: 0 24px 60px rgba(0,0,0,.6);
    }
    .commit-row {
      padding: 8px 10px;
      background: rgba(255,255,255,.03);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 6px;
      font-size: .8rem;
    }

    /* Visual Loaders, Progress Shimmer & Toasts */
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes shimmer {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(220%); }
    }
    .spinner {
      display: inline-block;
      width: 15px; height: 15px;
      border: 2.5px solid rgba(255,255,255,.22);
      border-top-color: var(--pink);
      border-radius: 50%;
      animation: spin .75s linear infinite;
      vertical-align: middle;
      flex-shrink: 0;
    }
    .loading-banner {
      display: none;
      flex-direction: column;
      gap: 8px;
      background: linear-gradient(90deg, rgba(244,63,142,.16), rgba(168,85,247,.16));
      border: 1px solid rgba(244,114,182,.45);
      border-radius: 14px;
      padding: 11px 15px;
      box-shadow: 0 8px 24px rgba(0,0,0,.35);
    }
    .loading-banner.show { display: flex; }
    .loading-row { display: flex; align-items: center; gap: 10px; font-size: .84rem; font-weight: 700; }
    .indet-track {
      position: relative;
      height: 5px;
      border-radius: 999px;
      background: rgba(255,255,255,.1);
      overflow: hidden;
    }
    .indet-bar {
      position: absolute;
      inset: 0 auto 0 0;
      width: 45%;
      border-radius: 999px;
      background: var(--sunset);
      animation: shimmer 1.25s ease-in-out infinite;
    }
    .hero-progress {
      position: absolute;
      left: 0; right: 0; bottom: 0;
      height: 4px;
      background: rgba(255,255,255,.08);
      overflow: hidden;
      display: none;
    }
    .hero-progress.active { display: block; }
    .toast-wrap {
      position: fixed;
      right: 22px; bottom: 22px;
      display: flex; flex-direction: column; gap: 8px;
      z-index: 90;
      pointer-events: none;
    }
    .toast {
      pointer-events: auto;
      background: var(--surface-2);
      border: 1px solid var(--border-hi);
      color: var(--text);
      padding: 11px 16px;
      border-radius: 12px;
      font-size: .83rem;
      font-weight: 600;
      box-shadow: 0 12px 32px rgba(0,0,0,.55);
      max-width: 390px;
    }
    .toast.ok { border-color: rgba(52,211,153,.55); }
    .toast.err { border-color: rgba(248,113,113,.65); }
  </style>
</head>
<body>
<div class="app-shell">
  <!-- LEFT SIDEBAR -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-badge">VI</div>
      <div>
        <div class="brand-text">VI Launcher</div>
        <div class="brand-sub">Community Runtime</div>
      </div>
    </div>

    <nav class="nav-list">
      <button class="nav-btn active" data-view="home" onclick="switchView('home')">🏠 Accueil</button>
      <button class="nav-btn" data-view="portwatch" onclick="switchView('portwatch')">🏆 PortWatch VI</button>
      <button class="nav-btn" data-view="roles" onclick="switchView('roles')">🧩 Composants (8)</button>
      <button class="nav-btn" data-view="all" onclick="switchView('all')">📚 Base complète</button>
      <button class="nav-btn" data-view="boot" onclick="switchView('boot')">🧪 Test Boot (2 voies)</button>
      <button class="nav-btn" data-view="silent" onclick="switchView('silent')">🌙 Veille Silencieuse</button>
    </nav>

    <div class="nav-sep"></div>

    <div class="side-footer">
      <div style="font-weight:700;color:#e2e8f0">Profil GPU (PortWatch)</div>
      <select id="gpuSel" onchange="saveAutoCfg()">
        <option value="NVIDIA" selected>🟢 NVIDIA (prioritaire)</option>
        <option value="AMD">🟠 AMD Radeon</option>
        <option value="ALL">⚪ Tous les GPU</option>
      </select>
      <div style="font-weight:700;color:#e2e8f0;margin-top:4px">Stockage VM</div>
      <select id="driveSel" onchange="saveAutoCfg()"></select>
      <label style="display:flex;align-items:center;gap:7px;cursor:pointer;color:var(--muted)">
        <input id="chkAutoDl" type="checkbox" checked onchange="saveAutoCfg()" />
        <span>Auto-DL VM (remplace)</span>
      </label>
      <div style="font-size:.72rem;color:var(--muted)">Quota GitHub : <b id="stQuota" style="color:#e2e8f0">—</b></div>
    </div>
  </aside>

  <!-- CENTER STAGE -->
  <main class="main">
    <div class="topbar">
      <div class="search-wrap">
        <span class="search-ico">🔍</span>
        <input id="fSearch" type="text" placeholder="Rechercher un runtime, symbole, outil..." oninput="refresh()" />
      </div>

      <div class="seg-group" role="tablist">
        <button class="seg-btn active" data-track="all" onclick="setTrack('all')">Tout</button>
        <button class="seg-btn" data-track="recomp" onclick="setTrack('recomp')">Recomp PC</button>
        <button class="seg-btn" data-track="xbox" onclick="setTrack('xbox')">Ému Xbox Series</button>
      </div>

      <div style="display:flex;gap:8px;align-items:center">
        <button class="icon-btn" title="Réappliquer les règles Xbox Series & Recomp sur tracker.db" onclick="reclassifyDb()">🔄 Reclasser</button>
        <span id="runPill" class="status-pill">⚪ Arrêté</span>
      </div>
    </div>

    <!-- GLOBAL VISUAL LOADING BANNER -->
    <div id="loadingBanner" class="loading-banner">
      <div class="loading-row">
        <span class="spinner"></span>
        <span id="loadingText">Opération en cours...</span>
      </div>
      <div class="indet-track"><div class="indet-bar"></div></div>
    </div>

    <!-- VIEW: HOME -->
    <section id="view-home" class="view active">
      <div class="hero">
        <div class="hero-vi-watermark">VI</div>
        <div>
          <div class="hero-tag" id="heroTrackTag">✦ VICE CITY EDITION · TOUTES LES VOIES (RECOMP PC + XBOX SERIES)</div>
          <h1>Community <span>VI</span> Launcher</h1>
          <div class="hero-sub" id="lastMsg">Prêt — suivez en temps réel la recompilation statique PC et les runtimes Xbox Series (Neonbridge, Flamingo, d3d12_xs).</div>
        </div>
        <div class="hero-actions">
          <button id="btnHeroWatch" class="btn-cta" onclick="toggleWatch()">▶ Lancer la veille</button>
          <button class="btn-glass" onclick="switchView('portwatch')">🏆 Classement PortWatch VI</button>
          <button class="btn-glass" onclick="switchView('boot'); runBootTest();">🧪 Test Boot</button>
        </div>
        <div id="heroProgress" class="hero-progress"><div class="indet-bar"></div></div>
      </div>

      <div id="pwPinnedBanner" class="card" style="display:none;border-color:rgba(251,191,36,.45);background:linear-gradient(90deg,rgba(251,191,36,.12),rgba(244,63,142,.10))">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div>
            <span class="badge b-upd">📌 FLASH PORTWATCH VI</span>
            <span id="pwPinnedText" style="margin-left:8px;font-size:.85rem;font-weight:700"></span>
          </div>
          <a href="https://portwatch-vi.xyz/#top" target="_blank" style="font-size:.78rem;white-space:nowrap">Voir sur portwatch-vi.xyz ↗</a>
        </div>
      </div>

      <div class="sec-title">
        <h2>Outils &amp; Runtimes locaux</h2>
        <span>Lancement direct en un clic</span>
      </div>
      <div class="shelf">
        <div class="tool-card tc-1" onclick="launchTool('neonbridge')">
          <div class="tool-ico">🟣</div>
          <div>
            <div class="tool-name">Neonbridge</div>
            <div class="tool-desc">Runtime Xbox Series (Sandboxie)</div>
          </div>
        </div>
        <div class="tool-card tc-2" onclick="launchTool('pagiii')">
          <div class="tool-ico">📡</div>
          <div>
            <div class="tool-name">pagiii Tracker</div>
            <div class="tool-desc">Dashboard live :3090</div>
          </div>
        </div>
        <div class="tool-card tc-3" onclick="launchTool('deps')">
          <div class="tool-ico">🛠️</div>
          <div>
            <div class="tool-name">Dépendances</div>
            <div class="tool-desc">LLVM, CMake, Ninja, DLLs</div>
          </div>
        </div>
        <div class="tool-card tc-4" onclick="launchTool('sandbox')">
          <div class="tool-ico">🛡️</div>
          <div>
            <div class="tool-name">Bac à sable VM</div>
            <div class="tool-desc">Ouvrir Sandboxie / by_role</div>
          </div>
        </div>
      </div>

      <div class="sec-title">
        <h2 id="pillarsHeading">Les 3 Piliers Critiques</h2>
        <span>Mises à jour avec aperçu « Quoi de neuf ? »</span>
      </div>
      <div id="pillarsGrid" class="pillars"></div>
    </section>

    <!-- VIEW: PORTWATCH VI LEADERBOARD & NEWS -->
    <section id="view-portwatch" class="view">
      <div class="sec-title">
        <h2>🏆 Classement Live PortWatch VI (<span id="pwCount">0</span> builds)</h2>
        <div style="display:flex;gap:8px;align-items:center">
          <span id="pwUpdated" class="muted">Synchro...</span>
          <button class="btn-sm primary" onclick="syncPortWatch()">🔄 Synchro PortWatch VI</button>
          <a href="https://portwatch-vi.xyz/#top" target="_blank" class="btn-sm">🌐 Ouvrir le site ↗</a>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width:42px">#</th>
            <th style="width:185px">Projet &amp; Approche</th>
            <th style="width:95px">GPU</th>
            <th style="width:82px">Progrès</th>
            <th style="width:140px">FPS / Rendu</th>
            <th>Statut</th>
            <th style="width:145px">Action</th>
          </tr>
        </thead>
        <tbody id="pwBoardTbody"></tbody>
      </table>

      <div class="sec-title">
        <h2>🛠️ Outils RPF9 / RE vérifiés par PortWatch VI</h2>
        <span>Extracteurs d'archives, shaders &amp; décompilateurs</span>
      </div>
      <table>
        <thead><tr><th style="width:175px">Outil</th><th style="width:140px">Catégorie</th><th>Description</th><th style="width:120px">Action</th></tr></thead>
        <tbody id="pwToolsTbody"></tbody>
      </table>

      <div class="sec-title">
        <h2>📰 Dernières actualités PortWatch VI</h2>
      </div>
      <div id="pwNewsGrid" class="pillars"></div>
    </section>

    <!-- VIEW: ROLES (8 COMPONENTS) -->
    <section id="view-roles" class="view">
      <div class="sec-title">
        <h2>Composants Open-Source par Rôle</h2>
        <span id="trackLabel">Filtre actif : Tout</span>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width:210px">Rôle &amp; Voie</th>
            <th style="width:185px">Version locale</th>
            <th>Meilleure version trouvée</th>
            <th style="width:62px">Score</th>
            <th style="width:215px">Action</th>
          </tr>
        </thead>
        <tbody id="rolesTbody"></tbody>
      </table>
    </section>

    <!-- VIEW: ALL ITEMS -->
    <section id="view-all" class="view">
      <div class="sec-title">
        <h2>Base complète (<span id="allCount">0</span>)</h2>
        <span>Filtrée par voie et recherche</span>
      </div>
      <table>
        <thead>
          <tr><th style="width:55px">Score</th><th style="width:170px">Rôle</th><th style="width:120px">Statut</th><th>Titre</th><th style="width:120px">MàJ</th><th style="width:110px">Action</th></tr>
        </thead>
        <tbody id="allTbody"></tbody>
      </table>
    </section>

    <!-- VIEW: BOOT TEST (DUAL SCORE) -->
    <section id="view-boot" class="view">
      <div class="sec-title">
        <h2>🧪 Diagnostic Double Voie (Test Boot &amp; Compilation)</h2>
        <button class="btn-sm primary" onclick="runBootTest()">🔄 Relancer le test</button>
      </div>
      <div class="dual-gauges">
        <div class="card">
          <div class="muted">VOIE 1 · RECOMPILATION STATIQUE</div>
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <h3>Prêt Recomp PC</h3>
            <span id="bootRecompPct" class="big-pct" style="color:var(--cyan)">—%</span>
          </div>
          <div class="bar-track"><div id="bootRecompBar" class="bar-fill-recomp" style="width:0%"></div></div>
          <div class="muted">LLVM/Clang, CMake, Ninja, VCRedist, Vulkan, symbols.toml</div>
        </div>
        <div class="card">
          <div class="muted">VOIE 2 · RUNTIME / ÉMULATION</div>
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <h3>Prêt Ému Xbox Series</h3>
            <span id="bootXboxPct" class="big-pct" style="color:var(--pink)">—%</span>
          </div>
          <div class="bar-track"><div id="bootXboxBar" class="bar-fill-xbox" style="width:0%"></div></div>
          <div class="muted">GPU DirectX 12, Sandboxie-Plus, Neonbridge / d3d12_xs, dump local</div>
        </div>
      </div>
      <div id="bootSummary" class="muted" style="font-weight:600;color:#e2e8f0"></div>
      <table>
        <thead><tr><th>Vérification</th><th style="width:95px">Voie</th><th style="width:80px">État</th><th>Détail</th></tr></thead>
        <tbody id="bootTbody"></tbody>
      </table>
    </section>

    <!-- VIEW: SILENT WATCH & CURATED DISCOVERY -->
    <section id="view-silent" class="view">
      <div class="sec-title">
        <h2>🌙 Veille Silencieuse (Mode Zéro Bruit) &amp; Auto-Découverte</h2>
        <span>Alertes uniquement sur jalons majeurs</span>
      </div>
      <div class="dual-gauges">
        <div class="card">
          <h3>Veille en arrière-plan (dans le launcher)</h3>
          <div class="muted">Scanne périodiquement sans bruit et envoie un Toast Windows uniquement si une release Xbox Series, un <code>symbols.toml</code> ou un statut <code>Boots / Playable</code> apparaît.</div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
            <select id="silentInterval" style="background:var(--bg);color:var(--text);border:1px solid var(--border);padding:6px 10px;border-radius:8px">
              <option value="60">Toutes les 1 heure</option>
              <option value="120">Toutes les 2 heures</option>
            </select>
            <button class="btn-sm primary" onclick="silentAction('start')">🌙 Démarrer</button>
            <button class="btn-sm" onclick="silentAction('stop')">⏹ Arrêter</button>
          </div>
        </div>
        <div class="card">
          <h3>Tâche planifiée Windows (même launcher fermé)</h3>
          <div class="muted">Exécute <code>silent_watch.py --once</code> via le Planificateur Windows. État : <b id="schtaskState">—</b></div>
          <div style="display:flex;gap:8px;margin-top:6px">
            <button class="btn-sm safe" onclick="silentAction('install_task')">✅ Activer la tâche</button>
            <button class="btn-sm" onclick="silentAction('remove_task')">🗑️ Désactiver</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <h3>🧭 Auto-découverte depuis les dépôts curatés</h3>
            <div class="muted">Lit <code>awesome-gta6-reverse-engineering</code>, <code>pagiii/gta6-tracker</code> &amp; <a href="https://portwatch-vi.xyz/" target="_blank">portwatch-vi.xyz</a> pour suivre automatiquement les nouveaux projets (<b id="discCount">0</b> découverts).</div>
          </div>
          <button class="btn-sm primary" onclick="runCuratedDiscovery()">Scanner maintenant</button>
        </div>
      </div>

      <div class="sec-title"><h2>Journal d'activité</h2></div>
      <div id="autoLogs" class="logbox">En attente d'activité...</div>
    </section>
  </main>

  <!-- RIGHT RAIL -->
  <aside class="right-rail">
    <div class="rail-box">
      <div class="rail-title">
        <span>Préparation des 2 Voies</span>
        <a href="#" onclick="switchView('boot'); runBootTest(); return false;" style="font-size:.74rem">Détails ›</a>
      </div>
      <div class="gauge-row">
        <div class="gauge-head"><span>Recomp PC</span><span id="railRecompPct" style="color:var(--cyan)">—%</span></div>
        <div class="bar-track"><div id="railRecompBar" class="bar-fill-recomp" style="width:0%"></div></div>
      </div>
      <div class="gauge-row">
        <div class="gauge-head"><span>Ému Xbox Series</span><span id="railXboxPct" style="color:var(--pink)">—%</span></div>
        <div class="bar-track"><div id="railXboxBar" class="bar-fill-xbox" style="width:0%"></div></div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px" id="railToolsBadges"></div>
      <div style="margin-top:4px;font-size:.75rem;display:flex;justify-content:space-between;align-items:center">
        <span class="muted">Référence live :</span>
        <a href="https://portwatch-vi.xyz/" target="_blank" style="font-weight:700">🌐 portwatch-vi.xyz ↗</a>
      </div>
    </div>

    <div class="rail-box" style="flex:1">
      <div class="rail-title">
        <span>Jalons &amp; Activité</span>
        <span style="font-size:.72rem;color:var(--muted)" id="railCycle">Cycle #0</span>
      </div>
      <div id="activityFeed"><div class="muted">Aucun jalon récent...</div></div>
    </div>
  </aside>
</div>

<!-- MODAL: QUOI DE NEUF ? (AVANT MÀJ) -->
<div id="whatsNewModal" class="modal-bg" onclick="if(event.target===this) closeWhatsNew()">
  <div class="modal">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <h3 id="wnTitle" style="margin:0;font-size:1.05rem">🔍 Quoi de neuf ?</h3>
      <button class="btn-sm" onclick="closeWhatsNew()">✕</button>
    </div>
    <div id="wnBody" class="muted" style="line-height:1.5;margin-bottom:16px">Chargement des notes de release et derniers commits...</div>
    <div style="display:flex;justify-content:flex-end;gap:10px">
      <button class="btn-sm" onclick="closeWhatsNew()">Annuler</button>
      <button id="wnConfirmBtn" class="btn-sm primary" onclick="confirmWhatsNewUpdate()">⬇️ Confirmer &amp; remplacer l'ancien</button>
    </div>
  </div>
</div>

<!-- TOAST NOTIFICATIONS -->
<div id="toastWrap" class="toast-wrap"></div>

<script>
let drivesInit = false;
let isBusy = false;
let pendingRefresh = false;
let currentTrack = "all";
let currentRunning = false;
let pendingUpdate = null;

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function showLoader(msg) {
  document.getElementById("loadingText").textContent = msg || "Chargement en cours...";
  document.getElementById("loadingBanner").classList.add("show");
}

function hideLoader() {
  document.getElementById("loadingBanner").classList.remove("show");
}

function showToast(msg, kind = "ok") {
  const wrap = document.getElementById("toastWrap");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .35s"; }, 3600);
  setTimeout(() => el.remove(), 4000);
}

function switchView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  const target = document.getElementById("view-" + name);
  if (target) target.classList.add("active");
  if (name === "silent") checkSchtaskStatus();
}

function setTrack(tr) {
  currentTrack = tr;
  document.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("active", b.dataset.track === tr));
  const labels = { all: "Tout", recomp: "Recomp PC", xbox: "Ému Xbox Series" };
  const heroTags = {
    all: "✦ VICE CITY EDITION · TOUTES LES VOIES (RECOMP PC + XBOX SERIES)",
    recomp: "✦ VOIE ACTIVE · RECOMPILATION STATIQUE PC (LLVM / SYMBOLS.TOML)",
    xbox: "✦ VOIE ACTIVE · ÉMULATION & RUNTIME XBOX SERIES (NEONBRIDGE / D3D12_XS)"
  };
  const pillarTitles = {
    all: "Les 3 Piliers Critiques (Toutes les voies)",
    recomp: "Les 3 Piliers — Voie Recomp PC",
    xbox: "Les 3 Piliers — Voie Ému Xbox Series"
  };
  const tl = document.getElementById("trackLabel");
  if (tl) tl.textContent = "Filtre actif : " + (labels[tr] || tr);
  const ht = document.getElementById("heroTrackTag");
  if (ht) ht.textContent = heroTags[tr] || heroTags.all;
  const ph = document.getElementById("pillarsHeading");
  if (ph) ph.textContent = pillarTitles[tr] || pillarTitles.all;
  refresh(true);
}

function renderBootData(b) {
  if (!b) return;
  const rp = Number(b.recomp_readiness_pct || 0);
  const xp = Number(b.xbox_readiness_pct || 0);
  ["railRecompPct", "bootRecompPct"].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = rp + "%"; });
  ["railXboxPct", "bootXboxPct"].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = xp + "%"; });
  ["railRecompBar", "bootRecompBar"].forEach(id => { const el = document.getElementById(id); if (el) el.style.width = rp + "%"; });
  ["railXboxBar", "bootXboxBar"].forEach(id => { const el = document.getElementById(id); if (el) el.style.width = xp + "%"; });

  const sumEl = document.getElementById("bootSummary");
  if (sumEl && b.summary) sumEl.textContent = b.summary;
  const tbody = document.getElementById("bootTbody");
  if (tbody && Array.isArray(b.checks) && b.checks.length) {
    tbody.innerHTML = b.checks.map(c => {
      const trTag = c.track === "xbox" ? '<span class="badge b-xbox">Xbox Series</span>'
        : (c.track === "recomp" ? '<span class="badge b-recomp">Recomp PC</span>' : '<span class="badge">Commun</span>');
      return `<tr><td><b>${esc(c.name)}</b></td><td>${trTag}</td><td class="chk-${esc(c.status)}">${esc(c.status)}</td><td>${esc(c.detail)}</td></tr>`;
    }).join("");
  }
}

async function refresh(force = false) {
  if (isBusy) {
    if (force) pendingRefresh = true;
    return;
  }
  isBusy = true;
  try {
    const q = document.getElementById("fSearch") ? document.getElementById("fSearch").value : "";
    const gpu = document.getElementById("gpuSel") ? document.getElementById("gpuSel").value : "NVIDIA";
    const res = await fetch("/api/cockpit?search=" + encodeURIComponent(q) + "&track=" + encodeURIComponent(currentTrack) + "&gpu=" + encodeURIComponent(gpu));
    if (!res.ok) return;
    const d = await res.json();

    currentRunning = Boolean(d.running);
    const pill = document.getElementById("runPill");
    const heroBtn = document.getElementById("btnHeroWatch");
    const heroProg = document.getElementById("heroProgress");
    if (d.watch_state === "running" && d.silent_mode) {
      pill.textContent = "🌙 Silencieuse"; pill.className = "status-pill silent";
      heroBtn.textContent = "⏹ Arrêter la veille"; heroBtn.className = "btn-cta stop";
      if (heroProg) heroProg.classList.add("active");
    } else if (d.watch_state === "running" || d.watch_state === "starting") {
      pill.textContent = "🟢 Active #" + d.cycle; pill.className = "status-pill on";
      heroBtn.textContent = "⏹ Arrêter la veille"; heroBtn.className = "btn-cta stop";
      if (heroProg) heroProg.classList.add("active");
    } else {
      pill.textContent = "⚪ Arrêté"; pill.className = "status-pill";
      heroBtn.textContent = "▶ Lancer la veille"; heroBtn.className = "btn-cta";
      if (heroProg) heroProg.classList.remove("active");
    }

    document.getElementById("lastMsg").textContent = d.last_message;
    document.getElementById("stQuota").textContent = d.quota;
    document.getElementById("chkAutoDl").checked = Boolean(d.auto_download_enabled);
    document.getElementById("railCycle").textContent = "Cycle #" + (d.cycle || 0);
    document.getElementById("discCount").textContent = d.discovered_count || 0;

    if (!drivesInit && d.drives.length) {
      drivesInit = true;
      document.getElementById("driveSel").innerHTML = d.drives.map(dr =>
        `<option value="${esc(dr.letter)}" ${dr.letter===d.auto_download_drive?'selected':''}>${esc(dr.letter)}: (${dr.free_gb} Go libres)</option>`
      ).join("");
    }

    renderBootData(d.boot_summary);

    const lt = d.local_tools || {};
    document.getElementById("railToolsBadges").innerHTML =
      `<span class="badge ${lt.neonbridge_ready ? 'b-ok' : 'b-miss'}">Neonbridge</span>` +
      `<span class="badge ${lt.pagiii_ready ? 'b-ok' : 'b-miss'}">pagiii</span>` +
      `<span class="badge ${lt.sandboxie_ready ? 'b-ok' : 'b-miss'}">Sandboxie</span>`;

    // 3 Piliers
    document.getElementById("pillarsGrid").innerHTML = d.pillars.map(p => {
      const cls = p.update_suggested ? "upd" : (p.current ? "ok" : "");
      const badge = p.current
        ? (p.update_suggested ? '<span class="badge b-upd">⬆️ MàJ</span>' : '<span class="badge b-ok">✅ Prêt</span>')
        : '<span class="badge b-miss">Manquant</span>';
      const gpuTags = (p.best && p.best.gpu && p.best.gpu.length)
        ? p.best.gpu.map(g => `<span class="badge ${g==='NVIDIA'?'b-ok':'b-upd'}">${esc(g)}</span>`).join(" ")
        : "";
      const bestHtml = p.best
        ? `<div style="font-size:.8rem">Meilleur : <a href="${esc(p.best.url)}" target="_blank"><b>${esc(p.best.title)}</b></a> ${gpuTags}<div class="muted">${esc(p.best.version_label || '')} · Score ${p.best.score}</div></div>`
        : '<div class="muted">En attente de découverte</div>';
      const curTitle = p.current ? p.current.title : "";
      const curVer = p.current ? (p.current.installed_version || "") : "";
      const curDate = p.current ? (p.current.downloaded_at || "") : "";
      const btnHtml = p.best
        ? `<div style="display:flex;gap:6px;margin-top:4px">
            <button class="btn-sm primary" onclick="openWhatsNew('${esc(p.best.url)}','${esc(p.best.download_url)}','${esc(p.best.role)}','${esc(p.best.id)}','${esc(p.best.title)}','${esc(curTitle)}','${esc(curVer)}','${esc(curDate)}')">${p.current ? '⬆️ Voir diff & MàJ' : '⬇️ Installer en VM'}</button>
           </div>`
        : '';
      return `<div class="card ${cls}">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><h3>${esc(p.title)}</h3>${badge}</div>
        <div class="muted">${esc(p.subtitle)}</div>
        ${p.extra_note ? `<div style="font-size:.76rem;color:var(--cyan)">${esc(p.extra_note)}</div>` : ''}
        ${bestHtml}${btnHtml}
      </div>`;
    }).join("");

    // Tableau des 8 rôles
    document.getElementById("rolesTbody").innerHTML = d.roles_table.map(r => {
      const trackBadge = r.track.includes("Ému")
        ? '<span class="badge b-xbox">Ému Xbox</span>'
        : (r.track.includes("Recomp") ? '<span class="badge b-recomp">Recomp PC</span>' : '<span class="badge">Commun</span>');
      const curTitle = r.current ? r.current.title : "";
      const curVer = r.current ? (r.current.installed_version || "") : "";
      const curDate = r.current ? (r.current.downloaded_at || "") : "";
      const curHtml = r.current
        ? `<span class="badge b-ok">✅ Installé</span><div style="font-size:.77rem;font-weight:700;margin-top:3px">${esc(r.current.title)}</div><div class="muted">${esc(curVer)}</div>`
        : '<span class="badge b-miss">Manquant</span>';
      const gpuTags = (r.best && r.best.gpu && r.best.gpu.length)
        ? " " + r.best.gpu.map(g => `<span class="badge ${g==='NVIDIA'?'b-ok':'b-upd'}">${esc(g)}</span>`).join(" ")
        : "";
      const bestHtml = r.best
        ? `<a href="${esc(r.best.url)}" target="_blank"><b>${esc(r.best.title)}</b></a>${gpuTags}<div class="muted">${esc(r.best.version_label || '')} · ${esc(r.best.status)}</div>`
        : '<span class="muted">—</span>';
      let actHtml = '<span class="muted">—</span>';
      if (r.best) {
        const callArgs = `'${esc(r.best.url)}','${esc(r.best.download_url)}','${esc(r.role)}','${esc(r.best.id)}','${esc(r.best.title)}','${esc(curTitle)}','${esc(curVer)}','${esc(curDate)}'`;
        if (r.missing) {
          actHtml = `<button class="btn-sm safe" onclick="openWhatsNew(${callArgs})">⬇️ Voir &amp; Installer</button>`;
        } else if (r.update_suggested) {
          actHtml = `<button class="btn-sm primary" onclick="openWhatsNew(${callArgs})">⬆️ Diff &amp; MàJ</button>`;
        } else {
          actHtml = `<span class="badge b-ok">✅ À jour</span> <button class="btn-sm" title="Comparer les versions" onclick="openWhatsNew(${callArgs})">🔍</button>`;
        }
      }
      return `<tr>
        <td><b>${esc(r.role)}</b><div style="margin-top:4px">${trackBadge}</div></td>
        <td>${curHtml}</td>
        <td>${bestHtml}</td>
        <td><b>${r.best ? r.best.score : '—'}</b></td>
        <td>${actHtml}</td>
      </tr>`;
    }).join("");

    // Base complète
    document.getElementById("allCount").textContent = d.items.length;
    document.getElementById("allTbody").innerHTML = d.items.map(it => `<tr>
      <td><b>${it.score}</b></td>
      <td>${esc(it.role)}</td>
      <td>${esc(it.status)}</td>
      <td><a href="${esc(it.url)}" target="_blank">${esc(it.title)}</a> ${it.downloaded ? '<span class="badge b-ok">✅</span>' : ''}</td>
      <td style="white-space:nowrap">${esc(it.date)}</td>
      <td><button class="btn-sm" onclick="openWhatsNew('${esc(it.url)}','${esc(it.download_url)}','${esc(it.role)}','${esc(it.id)}','${esc(it.title)}')">🔍 / ⬇️</button></td>
    </tr>`).join("");

    // Activity feed (Right Rail)
    const feed = (d.milestones_feed || []).slice(0, 6);
    const logs = (d.auto_logs || []).slice(-4).reverse();
    let feedHtml = feed.map(m => `<div class="feed-item">
      <div class="feed-dot ${m.downloaded ? 'ok' : ''}"></div>
      <div>
        <a href="${esc(m.url)}" target="_blank" style="color:#f3f4f6;font-weight:700">${esc(m.title)}</a>
        <div class="muted">${esc(m.status)} · ${esc(m.date)}</div>
      </div>
    </div>`).join("");
    if (logs.length) {
      feedHtml += logs.map(l => `<div class="feed-item"><div class="feed-dot ok"></div><div class="muted" style="color:#cbd5e1">${esc(l)}</div></div>`).join("");
    }
    document.getElementById("activityFeed").innerHTML = feedHtml || '<div class="muted">En attente de jalons...</div>';

    const allLogs = d.auto_logs || [];
    document.getElementById("autoLogs").textContent = allLogs.length ? allLogs.join("\n") : "En attente d'activité...";

    // PortWatch VI Live Leaderboard, Pin, Tools & News
    const pw = d.portwatch || {};
    const pinBox = document.getElementById("pwPinnedBanner");
    if (pinBox && pw.pinned && pw.pinned.text) {
      pinBox.style.display = "flex";
      document.getElementById("pwPinnedText").textContent = pw.pinned.text;
    } else if (pinBox) {
      pinBox.style.display = "none";
    }
    const board = pw.leaderboard || [];
    document.getElementById("pwCount").textContent = board.length;
    document.getElementById("pwUpdated").textContent = pw.updated ? ("MàJ : " + pw.updated.slice(0, 16).replace("T", " ") + " UTC") : "Non synchronisé";
    document.getElementById("pwBoardTbody").innerHTML = board.slice(0, 25).map((e, idx) => {
      const gpus = (e.gpu && e.gpu.length)
        ? e.gpu.map(g => `<span class="badge ${g==='AMD' ? 'b-upd' : 'b-ok'}">${esc(g)}</span>`).join(" ")
        : '<span class="muted">—</span>';
      const fpsTxt = e.fps ? `<b>${e.fps} FPS</b>` : "—";
      const rndTxt = e.render_pct ? ` · Rendu ${e.render_pct}%` : "";
      const nameHtml = e.repo
        ? `<a href="${esc(e.url)}" target="_blank"><b>${esc(e.name)}</b></a><div class="muted">${esc(e.repo)}</div>`
        : `<b>${esc(e.name)}</b><div class="muted">${esc(e.approach || '')}</div>`;
      const actHtml = e.repo
        ? `<button class="btn-sm primary" onclick="openWhatsNew('${esc(e.url)}','${esc(e.url)}','2. Émulateur / Runtime','','${esc(e.name)}')">🔍 / ⬇️ VM</button>`
        : '<span class="badge">🔒 Privé</span>';
      return `<tr>
        <td><b>#${idx + 1}</b></td>
        <td>${nameHtml}</td>
        <td>${gpus}</td>
        <td><b style="color:var(--pink)">${e.score_pct}%</b></td>
        <td>${fpsTxt}<span class="muted">${rndTxt}</span></td>
        <td>${esc(e.status)}</td>
        <td>${actHtml}</td>
      </tr>`;
    }).join("") || '<tr><td colspan="7" class="muted">Cliquez sur « Synchro PortWatch VI » pour charger le classement live.</td></tr>';

    document.getElementById("pwToolsTbody").innerHTML = (pw.tools || []).slice(0, 16).map(t => `<tr>
      <td><a href="${esc(t.url)}" target="_blank"><b>${esc(t.name)}</b></a><div class="muted">${esc(t.repo)}</div></td>
      <td><span class="badge b-recomp">${esc(t.kind)}</span></td>
      <td class="muted">${esc(t.desc)}</td>
      <td><button class="btn-sm safe" onclick="openWhatsNew('${esc(t.url)}','${esc(t.url)}','5. Outil RPF / RAGE 9','','${esc(t.name)}')">🔍 / ⬇️</button></td>
    </tr>`).join("");

    document.getElementById("pwNewsGrid").innerHTML = (pw.news || []).slice(0, 6).map(n => `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center"><span class="badge b-xbox">${esc(n.category)}</span><span class="muted">${esc(n.date)}</span></div>
      <h3>${esc(n.title)}</h3>
      <div class="muted">${esc(n.dek)}</div>
    </div>`).join("");
  } catch (e) {} finally {
    isBusy = false;
    if (pendingRefresh) {
      pendingRefresh = false;
      refresh(false);
    }
  }
}

async function toggleWatch() {
  try {
    if (currentRunning) {
      showLoader("Arrêt de la veille en cours...");
      const res = await fetch("/api/stop", { method: "POST" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      showToast("Veille arrêtée.", "ok");
    } else {
      showLoader("Démarrage des scrapers (GitHub, Xbox Series, Recomp)...");
      const res = await fetch("/api/start", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({sources:["all"]}) });
      if (!res.ok) throw new Error("HTTP " + res.status);
      showToast("Veille lancée !", "ok");
    }
  } catch (e) {
    showToast("❌ Impossible de joindre le serveur local. Relancez Lancer_Tracker.bat.", "err");
  } finally {
    hideLoader();
    refresh(true);
  }
}

async function saveAutoCfg() {
  await fetch("/api/auto-config", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({
      enabled: document.getElementById("chkAutoDl").checked,
      drive: document.getElementById("driveSel").value || "D",
      min_score: 75,
      gpu_profile: document.getElementById("gpuSel") ? document.getElementById("gpuSel").value : "NVIDIA",
    })
  });
  refresh(true);
}

async function openWhatsNew(repoUrl, dlUrl, role, itemId, title, currentTitle = "", currentVersion = "", currentDate = "") {
  pendingUpdate = { dlUrl, role, itemId, title };
  document.getElementById("wnTitle").textContent = "⬆️ Comparatif & Mise à jour — " + title;
  document.getElementById("wnBody").innerHTML = `<div style="display:flex;align-items:center;gap:10px;padding:12px 0"><span class="spinner"></span><span>Comparaison de votre version actuelle avec la nouvelle version (GitHub + PortWatch VI)...</span></div>`;
  document.getElementById("whatsNewModal").classList.add("open");
  try {
    const qs = new URLSearchParams({
      url: repoUrl || "",
      item_id: itemId || "",
      role: role || "",
      current_title: currentTitle || "",
      current_version: currentVersion || "",
      current_date: currentDate || "",
    });
    const res = await fetch("/api/whats-new?" + qs.toString());
    const r = await res.json();

    let html = `
      <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:14px">
        <div>
          <div class="muted" style="font-size:.7rem;text-transform:uppercase">Version actuelle</div>
          <div style="font-weight:800;color:#e2e8f0;margin-top:2px">${esc(r.current_version || "Non installée")}</div>
          ${r.current_title ? `<div class="muted" style="font-size:.72rem">${esc(r.current_title)}</div>` : ""}
        </div>
        <div style="font-size:1.3rem;color:var(--pink);font-weight:900">➜</div>
        <div>
          <div class="muted" style="font-size:.7rem;text-transform:uppercase">Nouvelle version (MàJ)</div>
          <div style="font-weight:800;color:var(--green);margin-top:2px">${esc(r.new_version || "Dernière version")}</div>
          <div class="muted" style="font-size:.72rem">${esc(r.repo || title)}</div>
        </div>
      </div>`;

    if (r.differences && r.differences.length) {
      html += `<div style="margin-bottom:14px;background:rgba(244,63,142,.08);border:1px solid rgba(244,114,182,.3);border-radius:12px;padding:12px">
        <div style="font-weight:800;color:#fff;margin-bottom:6px">📋 Différences entre les deux versions</div>
        <ul style="margin:0;padding-left:18px;color:#e2e8f0;font-size:.82rem;line-height:1.5">
          ${r.differences.map(df => `<li>${esc(df)}</li>`).join("")}
        </ul>
      </div>`;
    }

    if (r.release_name) {
      html += `<div style="margin-bottom:12px"><span class="badge b-upd">Release ${esc(r.release_name)}</span> <span class="muted">${esc(r.release_date)}</span>`;
      if (r.release_notes) html += `<div style="margin-top:6px;white-space:pre-wrap;color:#e2e8f0;background:rgba(255,255,255,.03);padding:10px;border-radius:10px;font-size:.8rem">${esc(r.release_notes)}</div>`;
      html += `</div>`;
    }
    if (r.commits && r.commits.length) {
      html += `<div style="font-weight:700;color:#e2e8f0;margin-bottom:6px">Derniers commits GitHub</div>`;
      html += r.commits.map(c => `<div class="commit-row"><b style="color:#fff">${esc(c.message)}</b><div class="muted">${esc(c.sha)} · ${esc(c.author)} · ${esc(c.date)}</div></div>`).join("");
    }
    document.getElementById("wnBody").innerHTML = html;
  } catch (e) {
    document.getElementById("wnBody").textContent = "Impossible de charger l'aperçu (vous pouvez tout de même lancer la mise à jour).";
  }
}

function closeWhatsNew() {
  document.getElementById("whatsNewModal").classList.remove("open");
  pendingUpdate = null;
}

async function confirmWhatsNewUpdate() {
  if (!pendingUpdate) return;
  const { dlUrl, role, itemId, title } = pendingUpdate;
  closeWhatsNew();
  const drive = document.getElementById("driveSel").value || "D";
  showLoader(`Téléchargement & scan Defender de [${role}] (${title || "composant"}) vers ${drive}:...`);
  try {
    const res = await fetch("/api/download-role", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ url: dlUrl, role, drive, item_id: itemId, replace_old: true })
    });
    const r = await res.json();
    if (!r.ok) {
      showToast("❌ Erreur téléchargement : " + (r.error || "échec"), "err");
    } else {
      const rem = (r.removed_previous || []).length;
      const kb = Math.max(1, Math.round((r.size_bytes || 0) / 1024));
      showToast(`✅ [${role}] téléchargé (${kb} Ko)${rem ? " · " + rem + " ancienne(s) version(s) remplacée(s)" : ""}`, "ok");
    }
  } catch (e) {
    showToast("❌ Erreur réseau pendant le téléchargement.", "err");
  } finally {
    hideLoader();
    refresh(true);
  }
}

async function runBootTest() {
  const drive = document.getElementById("driveSel").value || "D";
  showLoader("Test Boot & Compilation des 2 voies (Recomp PC & Ému Xbox Series)...");
  document.getElementById("bootSummary").innerHTML = `<span class="spinner"></span> Analyse en cours...`;
  try {
    const res = await fetch("/api/boot-test?drive=" + encodeURIComponent(drive));
    const r = await res.json();
    renderBootData(r);
    showToast(`🧪 Test Boot terminé : Recomp PC ${r.recomp_readiness_pct}% · Ému Xbox ${r.xbox_readiness_pct}%`, "ok");
  } catch (e) {
    showToast("❌ Erreur pendant le Test Boot.", "err");
  } finally {
    hideLoader();
  }
}

async function reclassifyDb() {
  showLoader("Reclassification de tracker.db (Recomp PC vs Ému Xbox Series)...");
  try {
    const res = await fetch("/api/reclassify", { method: "POST" });
    const r = await res.json();
    if (r.ok) {
      showToast(`🔄 ${r.updated} éléments reclassés (Recomp PC: ${r.recomp_count} · Ému Xbox: ${r.xbox_count})`, "ok");
      refresh(true);
    }
  } finally {
    hideLoader();
  }
}

async function runCuratedDiscovery() {
  showLoader("Scan des dépôts curatés (PortWatch VI, awesome-gta6 & pagiii)...");
  try {
    const res = await fetch("/api/discover-curated", { method: "POST" });
    const r = await res.json();
    if (r.ok) {
      const n = (r.newly_discovered || []).length;
      showToast(`🧭 Auto-découverte : +${n} nouveau(x) dépôt(s) (total suivi : ${r.total_discovered})`, "ok");
      refresh(true);
    } else {
      showToast("❌ Erreur auto-découverte : " + (r.error || "échec"), "err");
    }
  } finally {
    hideLoader();
  }
}

async function syncPortWatch() {
  showLoader("Synchronisation directe avec PortWatch VI (classement, outils & news)...");
  try {
    const res = await fetch("/api/sync-portwatch", { method: "POST" });
    const r = await res.json();
    if (r.ok) {
      showToast(`🏆 PortWatch VI synchronisé : ${r.builds} builds, ${r.tools} outils (+${r.new_saved} nouveaux)`, "ok");
      refresh(true);
    } else {
      showToast("❌ Erreur synchro PortWatch VI : " + (r.error || "échec"), "err");
    }
  } finally {
    hideLoader();
  }
}

async function launchTool(tool) {
  const drive = document.getElementById("driveSel").value || "D";
  showLoader("Lancement de " + tool + "...");
  try {
    const res = await fetch("/api/launch-tool", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ tool, drive })
    });
    const r = await res.json();
    if (!r.ok) showToast("❌ " + (r.error || "Impossible de lancer l'outil."), "err");
    else showToast("🚀 " + (r.message || "Outil lancé !"), "ok");
  } finally {
    hideLoader();
    refresh(true);
  }
}

async function silentAction(action) {
  const interval_minutes = Number(document.getElementById("silentInterval").value || 60);
  showLoader("Configuration de la veille silencieuse...");
  try {
    const res = await fetch("/api/silent-watch", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ action, interval_minutes })
    });
    const r = await res.json();
    if (r.message) showToast("🌙 " + r.message, "ok");
    else if (r.error) showToast("❌ " + r.error, "err");
  } finally {
    hideLoader();
    checkSchtaskStatus();
    refresh(true);
  }
}

async function checkSchtaskStatus() {
  const res = await fetch("/api/silent-watch", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ action: "status" })
  });
  const r = await res.json();
  const el = document.getElementById("schtaskState");
  if (el) el.textContent = r.installed ? "✅ Active" : "⚪ Inactive";
}

refresh();
setInterval(refresh, 3500);
</script>
</body>
</html>
"""


def make_handler(controller: TrackerController) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/":
                ui_file = (
                    (BASE_DIR / "launcher_ui.html")
                    if (BASE_DIR / "launcher_ui.html").exists()
                    else (BUNDLE_DIR / "launcher_ui.html")
                )
                raw = ui_file.read_bytes() if ui_file.exists() else HTML_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path in {"/assets/bg.png", "/assets/logo.svg", "/assets/logo.png", "/assets/logo.webp"}:
                if parsed.path.endswith("bg.png"):
                    asset_path = ASSETS_DIR / "BG.png"
                    ctype = "image/png"
                elif parsed.path.endswith("logo.svg"):
                    asset_path = ASSETS_DIR / "logo.svg"
                    ctype = "image/svg+xml"
                else:
                    asset_path = ASSETS_DIR / "logo.png"
                    ctype = "image/png"
                if asset_path.exists():
                    raw = asset_path.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/proxy-image":
                raw_u = (qs.get("url") or [""])[0].strip()
                if not raw_u.startswith(("http://", "https://")):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                import hashlib

                cache_dir = BASE_DIR / "data" / "mod_thumbs"
                cache_dir.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha1(raw_u.encode("utf-8")).hexdigest()
                cached_files = list(cache_dir.glob(f"{key}.*"))
                if cached_files:
                    cf = cached_files[0]
                    ext = cf.suffix.lower()
                    ctype = "image/webp" if ext == ".webp" else ("image/png" if ext == ".png" else ("image/gif" if ext == ".gif" else "image/jpeg"))
                    raw = cf.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                try:
                    with httpx.Client(follow_redirects=True, timeout=10.0) as client:
                        resp = client.get(raw_u, headers={"User-Agent": "Mozilla/5.0 VI-Community-Launcher/1.0"})
                    ctype = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
                    if resp.status_code == 200 and ctype.startswith("image/") and resp.content:
                        ext = ".webp" if "webp" in ctype else (".gif" if "gif" in ctype else (".jpg" if "jpeg" in ctype else ".png"))
                        (cache_dir / f"{key}{ext}").write_bytes(resp.content)
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Content-Length", str(len(resp.content)))
                        self.end_headers()
                        self.wfile.write(resp.content)
                        return
                except Exception:
                    pass
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/mod-image":
                raw_p = (qs.get("path") or [""])[0]
                img_p = Path(raw_p)
                if (
                    img_p.is_file()
                    and img_p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                    and str(controller.mods.mods_dir.resolve()).lower() in str(img_p.resolve()).lower()
                ):
                    ctype = "image/png" if img_p.suffix.lower() == ".png" else ("image/webp" if img_p.suffix.lower() == ".webp" else "image/jpeg")
                    raw = img_p.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/cockpit":
                self._send_json(
                    controller.get_cockpit_state(
                        search=(qs.get("search") or [""])[0],
                        track=(qs.get("track") or ["all"])[0],
                        gpu=(qs.get("gpu") or [None])[0],
                    )
                )
                return
            if parsed.path == "/api/boot-test":
                self._send_json(controller.run_boot_test((qs.get("drive") or ["D"])[0]))
                return
            if parsed.path == "/api/whats-new":
                self._send_json(
                    controller.get_whats_new(
                        url=(qs.get("url") or [""])[0],
                        item_id=(qs.get("item_id") or [None])[0],
                        role=(qs.get("role") or [""])[0],
                        current_title=(qs.get("current_title") or [""])[0],
                        current_version=(qs.get("current_version") or [""])[0],
                        current_date=(qs.get("current_date") or [""])[0],
                    )
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            body = self._read_json()
            if parsed.path == "/api/start":
                self._send_json(controller.start(body.get("sources")))
                return
            if parsed.path == "/api/stop":
                self._send_json(controller.stop())
                return
            if parsed.path == "/api/auto-config":
                self._send_json(
                    controller.set_auto_config(
                        enabled=bool(body.get("enabled", True)),
                        drive=str(body.get("drive") or "D"),
                        min_score=int(body.get("min_score") or 75),
                        gpu_profile=body.get("gpu_profile"),
                    )
                )
                return
            if parsed.path == "/api/download-role":
                self._send_json(
                    controller.download_role_update(
                        url=str(body.get("url") or "").strip(),
                        role=str(body.get("role") or "Divers"),
                        drive=str(body.get("drive") or "D"),
                        item_id=body.get("item_id") or None,
                        replace_old=bool(body.get("replace_old", True)),
                    )
                )
                return
            if parsed.path == "/api/reclassify":
                self._send_json(controller.reclassify_database())
                return
            if parsed.path == "/api/discover-curated":
                self._send_json(controller.run_curated_discovery())
                return
            if parsed.path == "/api/sync-portwatch":
                self._send_json(controller.sync_portwatch())
                return
            if parsed.path == "/api/check-updates":
                self._send_json(controller.check_updates_now())
                return
            if parsed.path == "/api/sync-online-news":
                self._send_json(controller.sync_online_news())
                return
            if parsed.path == "/api/silent-watch":
                self._send_json(
                    controller.manage_silent_task(
                        action=str(body.get("action") or "status"),
                        interval_minutes=int(body.get("interval_minutes") or 60),
                    )
                )
                return
            if parsed.path == "/api/launch-tool":
                self._send_json(
                    controller.launch_local_tool(
                        tool=str(body.get("tool") or ""),
                        drive=str(body.get("drive") or "D"),
                    )
                )
                return
            if parsed.path == "/api/vm/launch":
                self._send_json(controller.launch_inspection_vm(str(body.get("drive") or "D")))
                return
            if parsed.path == "/api/play":
                self._send_json(controller.play_game())
                return
            if parsed.path == "/api/stop-game":
                self._send_json(controller.stop_game())
                return
            if parsed.path == "/api/dismiss-crash":
                self._send_json(controller.dismiss_crash())
                return
            if parsed.path == "/api/prewarm-shaders":
                self._send_json(controller.prewarm_shaders())
                return
            if parsed.path == "/api/stop-prewarm":
                self._send_json(controller.stop_prewarm())
                return
            if parsed.path == "/api/saves/backup":
                self._send_json(controller.backup_saves())
                return
            if parsed.path == "/api/saves/restore":
                self._send_json(controller.restore_saves(str(body.get("filename") or "")))
                return
            if parsed.path == "/api/saves/open":
                self._send_json(controller.open_saves_folder())
                return
            if parsed.path == "/api/update-all":
                self._send_json(controller.update_all_pillars())
                return
            if parsed.path == "/api/rollback-role":
                self._send_json(
                    controller.rollback_role(
                        role=str(body.get("role") or ""),
                        drive=str(body.get("drive") or "D"),
                    )
                )
                return
            if parsed.path == "/api/clear-cache":
                self._send_json(controller.clear_cache())
                return
            if parsed.path == "/api/pick-game-dir":
                self._send_json(controller.pick_game_dir())
                return
            if parsed.path == "/api/open-game-dir":
                self._send_json(controller.open_game_dir())
                return
            if parsed.path == "/api/open-game-log":
                self._send_json(controller.open_game_log())
                return
            if parsed.path == "/api/open-live-console":
                self._send_json(controller.open_live_log_console())
                return
            if parsed.path == "/api/settings":
                self._send_json(controller.update_launcher_settings(body))
                return
            if parsed.path == "/api/mods/toggle":
                self._send_json(controller.mods.toggle(str(body.get("id") or ""), bool(body.get("value"))))
                return
            if parsed.path == "/api/mods/move":
                self._send_json(controller.mods.move(str(body.get("id") or ""), str(body.get("value") or "up")))
                return
            if parsed.path == "/api/mods/open-folder":
                self._send_json(controller.mods.open_folder())
                return
            if parsed.path == "/api/mods/install-workshop":
                self._send_json(controller.install_workshop_mod(str(body.get("id") or "")))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def _disable_windows_quick_edit() -> None:
    """Empêche un clic dans la console Windows de geler le serveur HTTP (QuickEdit Mode)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        h_in = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_in, ctypes.byref(mode)):
            # Retirer ENABLE_QUICK_EDIT_MODE (0x0040) et activer ENABLE_EXTENDED_FLAGS (0x0080)
            new_mode = (mode.value & ~0x0040) | 0x0080
            kernel32.SetConsoleMode(h_in, new_mode)
    except Exception:
        pass


def main() -> None:
    _disable_windows_quick_edit()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        config = load_config(CONFIG_PATH)
        controller = TrackerController(config)
        host = "127.0.0.1"
        server: ThreadingHTTPServer | None = None
        chosen_port = 8765
        for port in range(8765, 8780):
            try:
                server = ThreadingHTTPServer((host, port), make_handler(controller))
                chosen_port = port
                break
            except OSError:
                continue
        if server is None:
            print("Aucun port libre entre 8765 et 8779.")
            input("Appuyez sur Entree pour fermer...")
            return

        url = f"http://{host}:{chosen_port}"
        print(f"VI Community Launcher ready on {url} (Ctrl+C to quit)", flush=True)
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            controller.stop()
            server.server_close()
            print("\nLauncher ferme proprement.")
    except Exception:
        traceback.print_exc()
        try:
            input("\nErreur au demarrage. Appuyez sur Entree pour fermer...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
