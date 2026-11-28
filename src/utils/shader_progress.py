from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

# Whitelist of user-provided VICTORY_* environment variables kept across clean launches
# (prevents stale shell vars from silently breaking shader cache-key parity).
KEPT_VICTORY_ENV: frozenset[str] = frozenset(
    {
        "VICTORY_HOME",
        "VICTORY_CONFIG",
        "VICTORY_GAME_DIR",
        "VICTORY_CACHE_DIR",
        "VICTORY_DATA_DIR",
        "VICTORY_STREAMLINE_DIR",
        "VICTORY_FIDELITYFX_DIR",
        "VICTORY_LOG",
        "VICTORY_SHADER_OPT",
        "VICTORY_LDS_ANALYZE",
        "VICTORY_LDS_WRITE_ONLY_OK",
        "VICTORY_WAVE_SPLIT",
        "VICTORY_WATER_TILE_SLOTS",
        "VICTORY_FILE_PHYSICAL",
        "VICTORY_FILE_PHYSICAL_DIR",
        "VICTORY_SEGMENT_TIGHT",
        "VICTORY_ALLOW_TEARING",
        "VICTORY_VSYNC",
        "VICTORY_DYNAMIC_WAIT",
        "VICTORY_DXC",
        "VICTORY_DXIL",
    }
)

# Default empirical shader translation curve (count -> elapsed seconds) from Lumen
SHADER_SEED_TOTAL = 26000
SHADER_SEED_CURVE: tuple[tuple[int, float], ...] = (
    (1000, 45.0),
    (2000, 46.0),
    (3000, 47.0),
    (4000, 48.0),
    (5000, 49.0),
    (6000, 49.0),
    (7000, 50.0),
    (8000, 51.0),
    (9000, 52.0),
    (10000, 53.0),
    (11000, 55.0),
    (12000, 56.0),
    (13000, 58.0),
    (14000, 61.0),
    (15000, 63.0),
    (16000, 64.0),
    (17000, 70.0),
    (18000, 75.0),
    (19000, 78.0),
    (20000, 81.0),
    (21000, 83.0),
    (22000, 89.0),
    (23000, 120.0),
    (24000, 149.0),
    (25000, 150.0),
    (25786, 152.0),
)

D3D12_AGILITY_VERSION = "1.620.2"
D3D12_AGILITY_NUGET_URL = f"https://www.nuget.org/api/v2/package/Microsoft.Direct3D.D3D12/{D3D12_AGILITY_VERSION}"


def parse_duration_seconds(txt: str) -> float:
    if not txt:
        return 0.0
    secs = 0.0
    mh = re.search(r"(\d+)\s*h", txt)
    mm = re.search(r"(\d+)\s*m(?!s)", txt)
    ms = re.search(r"(\d+(?:\.\d+)?)\s*s", txt)
    if mh:
        secs += float(mh.group(1)) * 3600.0
    if mm:
        secs += float(mm.group(1)) * 60.0
    if ms:
        secs += float(ms.group(1))
    if secs == 0.0:
        m_plain = re.match(r"^\s*(\d+(?:\.\d+)?)\s*$", txt)
        if m_plain:
            secs = float(m_plain.group(1))
    return secs


def format_eta(secs: float) -> str:
    if secs < 45.0:
        return "under a minute"
    mins = max(1, int(-(-secs // 60) if (secs / 60.0 - int(secs / 60.0)) > 0.2 else max(1, int(secs // 60))))
    if mins < 60:
        return f"about {mins} min"
    return f"about {mins // 60} h {mins % 60} min"


def _curve_time(curve: list[tuple[int, float]] | tuple[tuple[int, float], ...], count: float) -> float:
    prev_c = 0.0
    prev_t = 0.0
    for pt_c, pt_t in curve:
        if count <= pt_c:
            if pt_c == prev_c:
                return float(pt_t)
            return prev_t + (pt_t - prev_t) * ((count - prev_c) / (pt_c - prev_c))
        prev_c = float(pt_c)
        prev_t = float(pt_t)
    return prev_t


class ShaderProgressTracker:
    """Pure log-line parser for Victory / Superbuild-VI / XCL shader & pipeline precompile progress + ETA."""

    def __init__(self, runner_id: str, *, history: dict[str, Any] | None = None, now: float | None = None) -> None:
        t = time.monotonic() if now is None else float(now)
        self.runner_id = runner_id
        self.history = history or {}
        self.t0 = t
        self.phase = "start"
        self.phase_t0 = t
        self.last_line_t = t
        self.count = 0
        self.total = 0
        self.warm_done = 0
        self.warm_total = 0
        self.lib_done = 0
        self.lib_total = 0
        self.compiles = 0
        self.native_eta = ""
        self.curve: list[tuple[int, float]] = []
        self.last_frame = 0
        self.world_reached = False
        self.recorded: dict[str, Any] = {}

    def _set_phase(self, new_phase: str, now: float) -> None:
        if self.phase == new_phase:
            return
        self.close_phase(now)
        self.phase = new_phase
        self.phase_t0 = now
        self.native_eta = ""

    def close_phase(self, now: float | None = None) -> None:
        t = time.monotonic() if now is None else float(now)
        secs = max(0.0, t - self.phase_t0)
        if self.phase == "shaders" and self.count >= 1000 and len(self.curve) >= 2:
            self.recorded["shaders"] = {
                "total": self.count,
                "secs": round(secs, 1),
                "curve": [[c, round(s, 1)] for c, s in self.curve[-40:]],
            }
        elif self.phase == "warm" and self.warm_done >= 50 and secs > 5:
            self.recorded["warm"] = {"total": self.warm_total, "done": self.warm_done, "secs": round(secs, 1)}
        elif self.phase == "library" and self.lib_done >= 50 and secs > 2:
            self.recorded["library"] = {"total": self.lib_total, "done": self.lib_done, "secs": round(secs, 1)}

    def feed_line(self, line: str, now: float | None = None) -> None:
        t = time.monotonic() if now is None else float(now)
        self.last_line_t = t
        s = (line or "").strip()
        if not s:
            return

        m_hdr = re.match(r"^=== (.+?) ===\s*$", s)
        if m_hdr:
            title = m_hdr.group(1)
            if re.search(r"Warming.*pipeline|pipeline library|warm", title, re.I):
                self._set_phase("warm", t)
            elif re.search(r"shader|Precompil|Step 1", title, re.I):
                self._set_phase("shaders", t)
            elif re.search(r"^Building", title, re.I):
                self._set_phase("build", t)
            return

        if re.match(r"^\s*Compiling\s+", s):
            if self.phase != "build":
                self._set_phase("build", t)
            self.compiles += 1
            return

        if re.match(r"^\s*shaders\s+precompiling into cache", s, re.I):
            self._set_phase("shaders", t)
            return

        # "shaders      500 ready (500 new, 0 cached) | elapsed 4s (125/s)"
        m = re.match(r"^\s*shaders\s+(\d+)\s+ready\b.*?(?:\|\s*elapsed\s+([^(\|]+))?", s, re.I)
        if m:
            cnt = int(m.group(1))
            el = parse_duration_seconds(m.group(2)) if m.group(2) else max(1.0, t - self.phase_t0)
            if self.phase != "shaders":
                self._set_phase("shaders", t)
                self.phase_t0 = t - el
            self.count = cnt
            self.curve.append((cnt, el))
            return

        # "shaders      25786 in the cache (...)"
        m = re.match(r"^\s*shaders\s+(\d+)\s+in the cache\b", s, re.I)
        if m:
            if self.phase != "shaders":
                self._set_phase("shaders", t)
            self.count = int(m.group(1))
            self.total = int(m.group(1))
            return

        # "warm-up      500/2000 shaders | elapsed 4s | ETA 12s (125/s)"
        m = re.match(
            r"^\s*warm-up\s+(\d+)/(\d+)\s+shaders(?:\s*\|\s*elapsed\s+([^\|]+))?(?:\|\s*ETA\s+([^(\|]+))?",
            s,
            re.I,
        )
        if m:
            el = parse_duration_seconds(m.group(3)) if m.group(3) else max(1.0, t - self.phase_t0)
            if self.phase != "shaders":
                self._set_phase("shaders", t)
                self.phase_t0 = t - el
            self.count = int(m.group(1))
            self.total = int(m.group(2))
            if m.group(4):
                self.native_eta = m.group(4).strip()
            self.curve.append((self.count, el))
            return

        # Legacy: "21000 shaders in the cache, 83 s" or "12000 shaders so far ... 42 s"
        m = re.match(r"^\s*(\d+)\s+shaders (?:in the cache|so far)(?:\D+(\d+)\s*s)?", s, re.I)
        if m:
            cnt = int(m.group(1))
            el = float(m.group(2)) if m.group(2) else max(1.0, t - self.phase_t0)
            if self.phase != "shaders":
                self._set_phase("shaders", t)
                self.phase_t0 = t - el
            self.count = cnt
            self.curve.append((cnt, el))
            return

        # "8500 pipelines known, 200 made before (...); 1800 to make, 4 processes"
        m = re.match(r"^\s*(\d+)\s+pipelines known,.*?;\s*(\d+)\s+to make", s, re.I)
        if m:
            self._set_phase("warm", t)
            self.warm_done = 0
            self.warm_total = int(m.group(2))
            return

        # "warm         100/1800 (5.6%) | elapsed 15s | ETA 4m 15s (6.7/s)"
        m = re.match(r"^\s*warm\s+(\d+)/(\d+)\s+\([\d.]+%\)\s*\|\s*elapsed\s+([^\|]+)\|\s*ETA\s+([^(\|]+)", s, re.I)
        if m:
            el = parse_duration_seconds(m.group(3))
            if self.phase != "warm":
                self._set_phase("warm", t)
                self.phase_t0 = t - el
            self.warm_done = int(m.group(1))
            self.warm_total = int(m.group(2))
            eta_s = m.group(4).strip()
            if eta_s and eta_s != "--":
                self.native_eta = eta_s
            return

        # "into the pipeline library: 1800 pipelines"
        m = re.match(r"^\s*into the pipeline library:\s*(\d+)\s+pipelines", s, re.I)
        if m:
            self._set_phase("library", t)
            self.lib_done = 0
            self.lib_total = int(m.group(1))
            return

        # "library      200/1800 (11.1%) | elapsed 3s | ETA 24s"
        m = re.match(r"^\s*library\s+(\d+)/(\d+)\s+\([\d.]+%\)\s*\|\s*elapsed\s+([^\|]+)\|\s*ETA\s+([^(\|]+)", s, re.I)
        if m:
            el = parse_duration_seconds(m.group(3))
            if self.phase != "library":
                self._set_phase("library", t)
                self.phase_t0 = t - el
            self.lib_done = int(m.group(1))
            self.lib_total = int(m.group(2))
            eta_s = m.group(4).strip()
            if eta_s and eta_s != "--":
                self.native_eta = eta_s
            return

        # Generic XCL counter: "1234/5678 (42 s)"
        m = re.match(r"^\s*(\d+)/(\d+)\s+\((\d+)\s*s\)", s)
        if m:
            if self.phase != "shaders":
                self._set_phase("shaders", t)
                self.phase_t0 = t - float(m.group(3))
            self.count = int(m.group(1))
            self.total = int(m.group(2))
            self.curve.append((self.count, float(m.group(3))))
            return

        # Frame presentation: "frame presented ... frame=1024 draws=10752"
        m = re.search(r"frame presented .*?frame=(\d+)\s+draws=(\d+)", s, re.I)
        if m:
            fr = int(m.group(1))
            dr = float(m.group(2))
            span = fr - self.last_frame
            self.last_frame = fr
            if span > 0 and (dr / span) >= 800.0 and not self.world_reached:
                self.world_reached = True
                self._set_phase("world", t)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        t = time.monotonic() if now is None else float(now)
        el = max(0.0, t - self.phase_t0)
        label = "Starting..."
        eta = ""
        percent: float | None = None
        quality = ""

        if self.phase == "shaders":
            own = self.history.get(f"{self.runner_id}|shaders")
            tot = float(self.total) if self.total > 0 else float((own or {}).get("total") or SHADER_SEED_TOTAL)
            if self.total == 0 and self.count > tot * 0.98:
                tot = self.count / 0.98
            percent = round(min(99.0, (self.count / tot) * 100.0), 1) if tot > 0 and self.count > 0 else None
            if self.total > 0:
                label = f"Translating shaders ({self.count:,} / {int(tot):,})"
            elif self.count > 0:
                label = f"Translating shaders ({self.count:,} / ~{int(tot):,})"
            else:
                label = "Preparing shader cache..."

            if self.count >= 500:
                if self.native_eta:
                    eta = f"{self.native_eta} left"
                    quality = "estimate"
                else:
                    raw_cv = (own or {}).get("curve")
                    cv = [(int(p[0]), float(p[1])) for p in raw_cv] if isinstance(raw_cv, list) and raw_cv else list(SHADER_SEED_CURVE)
                    tp = _curve_time(cv, self.count)
                    end_t = cv[-1][1]
                    ratio = max(0.25, min(6.0, el / tp)) if tp > 5.0 else 1.0
                    rem = max(0.0, end_t - tp) * ratio
                    eta = f"{format_eta(rem)} left"
                    quality = "learned" if (own and raw_cv) else "estimate"
        elif self.phase == "warm":
            if self.warm_total > 0:
                percent = round(min(99.0, (self.warm_done / self.warm_total) * 100.0), 1)
                label = f"Warming GPU pipelines ({self.warm_done:,} / {self.warm_total:,})"
                if self.native_eta:
                    eta = f"{self.native_eta} left"
                    quality = "estimate"
                elif self.warm_done >= 25 and el > 2.0:
                    rem = max(0, self.warm_total - self.warm_done) / (self.warm_done / el)
                    eta = f"{format_eta(rem)} left"
                    quality = "estimate"
            else:
                label = "Checking GPU pipeline book..."
        elif self.phase == "library":
            if self.lib_total > 0:
                percent = round(min(100.0, (self.lib_done / self.lib_total) * 100.0), 1)
                label = f"Packing pipeline library ({self.lib_done:,} / {self.lib_total:,})"
                if self.native_eta:
                    eta = f"{self.native_eta} left"
                    quality = "estimate"
            else:
                label = "Packing host pipeline library..."
        elif self.phase == "build":
            label = f"Building release binary ({self.compiles} crates compiled)"
        elif self.phase == "world":
            label = "World rendering active"
            percent = 100.0
            eta = "ready"

        return {
            "phase": self.phase,
            "label": label,
            "percent": percent,
            "eta": eta,
            "quality": quality,
            "elapsed_sec": round(max(0.0, t - self.t0), 1),
            "count": self.count if self.phase == "shaders" else (self.warm_done if self.phase == "warm" else self.lib_done),
            "total": self.total if self.phase == "shaders" else (self.warm_total if self.phase == "warm" else self.lib_total),
        }


def save_timing_history(path: Path, hw_id: str, runner_id: str, recorded: dict[str, Any]) -> None:
    if not hw_id or not runner_id or not recorded:
        return
    try:
        all_data: dict[str, Any] = {"machines": {}}
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("machines"), dict):
                all_data = raw
        mach = all_data["machines"].setdefault(hw_id, {})
        for phase, rec in recorded.items():
            mach[f"{runner_id}|{phase}"] = rec
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(all_data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_timing_history(path: Path, hw_id: str) -> dict[str, Any]:
    if not hw_id or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mach = (raw.get("machines") or {}).get(hw_id)
        return dict(mach) if isinstance(mach, dict) else {}
    except Exception:
        return {}


def use_low_ram_profile(
    *,
    ram_gb: float,
    console_profile: str = "auto",
    low_ram_setting: str = "auto",
) -> bool:
    """Determines whether VICTORY_LOW_RAM / --low-ram should be enabled (16 GB RAM safe default)."""
    mode = (low_ram_setting or "auto").strip().lower()
    if mode in {"on", "true", "1", "ultralow"}:
        return True
    if mode in {"off", "false", "0"}:
        return False
    con = (console_profile or "auto").strip().lower()
    return ram_gb > 0.0 and ram_gb <= 20.0 and con != "series-x"


def build_superbuild_play_args(
    game_exe: str,
    *,
    vendor: str,
    older_rtx: bool,
    ram_gb: float,
    console_profile: str = "auto",
    low_ram_mode: str = "auto",
    dlss_mode: str = "auto",
    fg_type: str = "auto",
    fps_limit: str = "60",
) -> list[str]:
    """Builds exact Superbuild-VI (`victory.exe play ...`) CLI arguments matching Lumen's hardware-tuned presets."""
    is_nv = vendor.lower() == "nvidia"
    low_mode = (low_ram_mode or "auto").strip().lower()
    con = (console_profile or "auto").strip().lower()
    if con == "auto":
        con = "series-s" if (ram_gb > 0 and ram_gb <= 20.0) else "series-x"

    fps_val = fps_limit if (fps_limit or "").isdigit() else "1000"
    fg_choice = (fg_type or "auto").strip().lower()
    if fg_choice == "auto":
        fg_choice = "fsr" if older_rtx else "auto"

    if low_mode == "ultralow":
        dlss_u = "performance" if is_nv else "off"
        return [
            "play",
            game_exe,
            "--low-ram",
            "--series-s",
            "--dlss",
            dlss_u,
            "--no-framegen-ready",
            "--fps",
            fps_val,
            "--set",
            "VICTORY_LOW_RAM=1",
            "--set",
            "VICTORY_TEXTURE_ROOM_PERCENT=75",
        ]

    use_low = use_low_ram_profile(ram_gb=ram_gb, console_profile=con, low_ram_setting=low_mode)
    dlss_choice = (dlss_mode or "auto").strip().lower()
    if dlss_choice == "auto":
        dlss_choice = ("balanced" if (use_low or con == "series-s") else "quality") if is_nv else "off"
    elif not is_nv:
        dlss_choice = "off"

    args = ["play", game_exe]
    if use_low:
        args.append("--low-ram")
    args.append("--series-s" if con == "series-s" else "--series-x")
    args.extend(["--dlss", dlss_choice])
    if is_nv and fg_choice != "off":
        args.extend(["--fg-type", fg_choice])
    else:
        args.append("--no-framegen-ready")
    args.extend(["--fps", fps_val])
    if use_low:
        args.extend(["--set", "VICTORY_LOW_RAM=1"])
    return args


def build_shared_profile_env(
    base_env: dict[str, str],
    *,
    spec_env: dict[str, str],
    game_dir: Path,
    victory_home: Path | None = None,
    resolution: str = "1920x1080",
    render_scale: str = "1.0",
    ray_tracing: bool = False,
    fps_limit: str = "60",
    ram_gb: float = 0.0,
    console_profile: str = "auto",
    low_ram_mode: str = "auto",
    is_superbuild: bool = False,
    play_args: list[str] | None = None,
    runtime_id: str = "",
) -> dict[str, str]:
    """Produces an identical Victory environment dictionary for BOTH Play and Precompile.

    Guarantees 100% shader cache-key parity and strips stale shell VICTORY_* variables
    not present in KEPT_VICTORY_ENV.
    """
    env = {
        k: v
        for k, v in base_env.items()
        if not (k.startswith("VICTORY_") and k not in KEPT_VICTORY_ENV)
    }
    env["VICTORY_GAME_DIR"] = str(game_dir)
    if victory_home is not None:
        env["VICTORY_HOME"] = str(victory_home)

    for k, v in spec_env.items():
        env[k] = v

    if resolution and "x" in resolution:
        env["VICTORY_OUTPUT"] = resolution
        env["VICTORY_DISPLAY_MODE"] = resolution
    if render_scale and render_scale != "1.0":
        env["VICTORY_RENDER_SCALE"] = render_scale
    env["VICTORY_RAYTRACING"] = "1" if ray_tracing else "0"
    if (fps_limit or "").isdigit():
        env["VICTORY_FPS_LIMIT"] = fps_limit
        if runtime_id == "cool_fox" and int(fps_limit) > 0:
            env["VICTORY_FRAME_INTERVAL_MS"] = f"{1000.0 / int(fps_limit):.3f}"

    # Shared Superbuild-VI / Wave32 cache-key parity env vars (from Lumen Get-ProfileEnv)
    if is_superbuild:
        env["VICTORY_EARLY_DEPTH"] = "1"
        env["VICTORY_HOST_MAX_WAVE"] = "32"
        if use_low_ram_profile(ram_gb=ram_gb, console_profile=console_profile, low_ram_setting=low_ram_mode):
            env["VICTORY_LOW_RAM"] = "1"
        if play_args:
            for i in range(len(play_args) - 1):
                if play_args[i] == "--set" and "=" in play_args[i + 1]:
                    pk, pv = play_args[i + 1].split("=", 1)
                    if pk.startswith("VICTORY_"):
                        env[pk] = pv

    return env


def inspect_upscaler_sdk_state(
    data_dir: Path,
    *,
    vendor: str,
    older_rtx: bool = False,
    low_ram_mode: str = "auto",
) -> dict[str, Any]:
    """Checks portable NVIDIA Streamline (DLSS) and AMD FidelityFX (FSR 3 FG) DLL readiness."""
    v = (vendor or "unknown").lower()
    nv_dir = data_dir / "nvidia-sdk"
    amd_dir = data_dir / "amd-fidelityfx"
    need_dlss = v == "nvidia"
    need_fsr = (older_rtx or v == "amd") and (low_ram_mode or "").lower() != "ultralow"
    dlss_ready = (nv_dir / "Streamline" / "bin" / "x64" / "sl.interposer.dll").is_file()
    fsr_ready = (
        (amd_dir / "amd_fidelityfx_loader_dx12.dll").is_file()
        and (amd_dir / "amd_fidelityfx_framegeneration_dx12.dll").is_file()
    )
    ok = ((not need_dlss) or dlss_ready) and ((not need_fsr) or fsr_ready)
    return {
        "data_dir": str(data_dir),
        "need_dlss": need_dlss,
        "need_fsr": need_fsr,
        "dlss_ready": dlss_ready,
        "fsr_ready": fsr_ready,
        "ready": ok,
        "applicable": bool(need_dlss or need_fsr),
    }


def fetch_d3d12_agility_sdk(
    cache_dir: Path,
    *,
    dest_dir: Path | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Downloads Microsoft D3D12 Agility SDK (1.620.2 x64) once into cache_dir and optionally copies to dest_dir."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    core_dll = cache_dir / "D3D12Core.dll"
    downloaded = False

    if not core_dll.is_file():
        tmp_pkg = cache_dir / f"agility-{D3D12_AGILITY_VERSION}.nupkg"
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                resp = client.get(D3D12_AGILITY_NUGET_URL, headers={"User-Agent": "SixDeck-Agility/1.0"})
                resp.raise_for_status()
                tmp_pkg.write_bytes(resp.content)
            with zipfile.ZipFile(tmp_pkg, "r") as zf:
                extracted = 0
                for member in zf.namelist():
                    norm = member.replace("\\", "/")
                    if norm in {"build/native/bin/x64/D3D12Core.dll", "build/native/bin/x64/d3d12SDKLayers.dll"}:
                        out_name = Path(norm).name
                        (cache_dir / out_name).write_bytes(zf.read(member))
                        extracted += 1
                if extracted == 0 or not core_dll.is_file():
                    return {"ok": False, "error": "D3D12Core.dll was not found inside the NuGet package."}
            downloaded = True
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            tmp_pkg.unlink(missing_ok=True)

    copied_to = ""
    if dest_dir is not None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for dll in cache_dir.glob("*.dll"):
            (dest_dir / dll.name).write_bytes(dll.read_bytes())
        copied_to = str(dest_dir)

    return {
        "ok": True,
        "version": D3D12_AGILITY_VERSION,
        "downloaded": downloaded,
        "cache_dir": str(cache_dir),
        "copied_to": copied_to,
        "message": f"D3D12 Agility SDK {D3D12_AGILITY_VERSION} ready ({cache_dir}).",
    }
