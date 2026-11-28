from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from src.sandbox.vm_downloader import detect_available_backends
from src.utils.hw_advisor import (
    detect_avx2,
    find_linux_compat_runner,
    get_system_memory_cross_platform,
    is_process_elevated,
    resolve_gdk_title_exe,
)


@dataclass
class BootCheckItem:
    name: str
    status: str  # "PASS" | "WARN" | "FAIL"
    detail: str
    track: str = "both"  # "recomp" | "xbox" | "both"


@dataclass
class BootTestReport:
    readiness_pct: int
    overall_status: str  # "READY" | "PARTIAL" | "BLOCKED"
    checks: list[BootCheckItem] = field(default_factory=list)
    compiled_sources: int = 0
    failed_sources: int = 0
    summary: str = ""
    recomp_readiness_pct: int = 0
    xbox_readiness_pct: int = 0


def _resolve_exe(cmd: str, fallback_paths: tuple[Path, ...] = ()) -> str | None:
    found = shutil.which(cmd)
    if found:
        return found
    for p in fallback_paths:
        if p.exists():
            return str(p)
    return None


def _check_cmd_version(
    cmd: str, args: list[str], fallback_paths: tuple[Path, ...] = ()
) -> tuple[bool, str, str | None]:
    exe = _resolve_exe(cmd, fallback_paths)
    if not exe:
        return False, "Not installed", None
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = ((proc.stdout or proc.stderr or "").strip().splitlines() or ["OK"])[0]
        return proc.returncode == 0, first[:90], exe
    except Exception as exc:
        return False, str(exc)[:80], exe


def run_boot_and_compile_test(
    sandbox_root: Path,
    *,
    dump_folder: Path | None = None,
) -> BootTestReport:
    """Exécute un test complet de préparation pour les 2 voies (Recomp PC & Ému Xbox Series).

    Ne lance jamais de binaire non fiable.
    - Voie Recomp PC : Vulkan, VCRedist, CMake, LLVM/Clang, Ninja, Rust, symbols.toml/RPF9, syntaxe C++20.
    - Voie Ému Xbox Series : DirectX 12 (d3d12.dll), Sandboxie-Plus, Neonbridge / d3d12_xs.dll, dump game_scarlett_master.exe.
    """
    checks: list[BootCheckItem] = []

    # 1. GPU & C++ Runtimes (PC Recomp + Console Emu)
    vulkan_dll = Path(r"C:\Windows\System32\vulkan-1.dll")
    checks.append(
        BootCheckItem(
            "Vulkan Runtime (GPU)",
            "PASS" if vulkan_dll.exists() else "FAIL",
            str(vulkan_dll) if vulkan_dll.exists() else "vulkan-1.dll not found",
            track="recomp",
        )
    )

    dx12_dll = Path(r"C:\Windows\System32\d3d12.dll")
    checks.append(
        BootCheckItem(
            "DirectX 12 Runtime (d3d12.dll)",
            "PASS" if dx12_dll.exists() else "FAIL",
            str(dx12_dll) if dx12_dll.exists() else "d3d12.dll not found",
            track="xbox",
        )
    )

    vc_dll = Path(r"C:\Windows\System32\vcruntime140_1.dll")
    checks.append(
        BootCheckItem(
            "Visual C++ Runtime (x64)",
            "PASS" if vc_dll.exists() else "FAIL",
            "Installed" if vc_dll.exists() else "VCRedist missing",
            track="both",
        )
    )

    # 1b. Hardware & System Safeguards (AVX2, Elevation/Shader Cache, Pagefile/Swap, Linux Proton)
    has_avx2 = detect_avx2()
    checks.append(
        BootCheckItem(
            "CPU AVX2 Support",
            "PASS" if has_avx2 else "FAIL",
            "AVX2 supported" if has_avx2 else "CPU lacks AVX2 (required by GDK compatibility layers)",
            track="xbox",
        )
    )

    elevated = is_process_elevated()
    checks.append(
        BootCheckItem(
            "User Privilege (Shader Cache Sharing)",
            "WARN" if elevated else "PASS",
            "Running as Admin/Root — NVIDIA isolates elevated shader caches; relaunch normally"
            if elevated
            else "Standard user (shader caches shared normally)",
            track="xbox",
        )
    )

    mem_stats = get_system_memory_cross_platform()
    ram_gb = float(mem_stats.get("total_ram_gb") or 0.0)
    pf_gb = float(mem_stats.get("pagefile_gb") or 0.0)
    if ram_gb > 0:
        low_pf = bool(mem_stats.get("low_pagefile", False))
        checks.append(
            BootCheckItem(
                "System RAM & Virtual Memory (Pagefile / Swap)",
                "WARN" if low_pf else "PASS",
                f"{ram_gb:.1f} GB RAM · {pf_gb:.1f} GB pagefile/swap"
                + (" (16+ GB recommended on <=20 GB PCs)" if low_pf else " (OK)"),
                track="both",
            )
        )

    if os.name != "nt":
        proton = find_linux_compat_runner()
        checks.append(
            BootCheckItem(
                "Linux Windows Runtime (Proton / umu-run / Wine)",
                "PASS" if proton else "WARN",
                f"{proton['kind']} ({proton['path']})" if proton else "Not detected (install umu-run, Steam Proton, or Wine)",
                track="xbox",
            )
        )

    # 2. Optional Sandbox Isolation & Compiled Direct Runtimes (cool_fox / ScarlettPort / VIctory / xcl)
    backends = detect_available_backends()
    has_sandbox = bool(backends.get("sandboxie") or backends.get("windows_sandbox"))
    sb_detail = (
        f"Sandboxie-Plus ({backends['sandboxie']})"
        if backends.get("sandboxie")
        else (f"Windows Sandbox ({backends['windows_sandbox']})" if backends.get("windows_sandbox") else "Optional — not installed")
    )
    checks.append(
        BootCheckItem(
            "Sandbox Isolation (Optional)",
            "PASS" if has_sandbox else "WARN",
            sb_detail,
            track="xbox",
        )
    )

    runtime_roots = [sandbox_root]
    for parent_cand in (sandbox_root.parent / "GTA6_Lab" / "workspaces", Path(__file__).resolve().parents[2] / "runtimes"):
        if parent_cand.exists():
            runtime_roots.append(parent_cand)
    compiled_runtimes: list[Path] = []
    for rroot in runtime_roots:
        if not rroot.exists():
            continue
        for pat in ("victory.exe", "victory-*.exe", "xcl.exe"):
            for fp in rroot.rglob(pat):
                if fp.is_file():
                    compiled_runtimes.append(fp)
    if compiled_runtimes:
        rt_status = "PASS"
        rt_detail = f"{len(compiled_runtimes)} compiled runtime(s) ready ({compiled_runtimes[0].parent.parent.name}/{compiled_runtimes[0].name})"
    else:
        rt_status = "FAIL"
        rt_detail = "No compiled direct runtime (victory.exe / xcl.exe) found"
    checks.append(
        BootCheckItem(
            "Compiled Direct Runtime (victory / xcl)",
            rt_status,
            rt_detail,
            track="xbox",
        )
    )

    # 3. Chaîne de compilation Recomp PC
    home = Path.home()
    local_app = home / "AppData" / "Local"
    clang_exe: str | None = None

    tool_defs: tuple[tuple[str, str, list[str], bool, tuple[Path, ...]], ...] = (
        (
            "CMake (Build System)",
            "cmake",
            ["--version"],
            True,
            (Path(r"C:\Program Files\CMake\bin\cmake.exe"),),
        ),
        (
            "LLVM / Clang (Recomp C++)",
            "clang++",
            ["--version"],
            True,
            (
                Path(r"C:\Program Files\LLVM\bin\clang++.exe"),
                Path(r"C:\Program Files (x86)\LLVM\bin\clang++.exe"),
            ),
        ),
        (
            "Ninja (Build Rapide)",
            "ninja",
            ["--version"],
            False,
            (
                local_app / "Microsoft" / "WinGet" / "Links" / "ninja.exe",
                *tuple((local_app / "Microsoft" / "WinGet" / "Packages").glob("Ninja-build.Ninja*/ninja.exe")),
            ),
        ),
        (
            "Rust / Cargo",
            "cargo",
            ["--version"],
            False,
            (home / ".cargo" / "bin" / "cargo.exe",),
        ),
    )

    for label, cmd, args, critical, fallbacks in tool_defs:
        ok, ver, resolved = _check_cmd_version(cmd, args, fallbacks)
        if cmd == "clang++" and ok:
            clang_exe = resolved
        checks.append(
            BootCheckItem(
                label,
                "PASS" if ok else ("FAIL" if critical else "WARN"),
                ver,
                track="recomp",
            )
        )

    # 4. Inventaire & intégrité des fichiers collectés (by_role + quarantine)
    collected_files: list[Path] = []
    for sub in ("by_role", "quarantine", "vm_incoming"):
        p = sandbox_root / sub
        if p.exists():
            collected_files.extend(fp for fp in p.rglob("*") if fp.is_file())

    if not collected_files:
        checks.append(
            BootCheckItem(
                "Collected Files",
                "FAIL",
                f"No files found in {sandbox_root}",
                track="both",
            )
        )
    else:
        empty_files = [f.name for f in collected_files if f.stat().st_size == 0]
        checks.append(
            BootCheckItem(
                "Collected Files Integrity",
                "WARN" if empty_files else "PASS",
                f"{len(collected_files)} file(s) present"
                + (f" ({len(empty_files)} empty)" if empty_files else ", all non-empty"),
                track="both",
            )
        )

    # 5. Vérification de présence des 3 piliers critiques
    by_role_dir = sandbox_root / "by_role"
    has_runtime = any(
        "Emulateur" in p.name or "Portage" in p.name or "Recompilateur" in p.name
        for p in (by_role_dir.iterdir() if by_role_dir.exists() else [])
        if p.is_dir() and any(p.rglob("*"))
    )
    has_symbols_or_rpf = any(
        "Symboles" in p.name or "RPF" in p.name
        for p in (by_role_dir.iterdir() if by_role_dir.exists() else [])
        if p.is_dir() and any(p.rglob("*"))
    )
    checks.append(
        BootCheckItem(
            "Pillar 1: Runtime / Emulator / Recomp",
            "PASS" if (has_runtime or compiled_runtimes) else "FAIL",
            "Compiled runtime or components present"
            if (has_runtime or compiled_runtimes)
            else "Missing runtime component",
            track="both",
        )
    )
    checks.append(
        BootCheckItem(
            "Pillar 2: symbols.toml / RPF9 Tools",
            "PASS" if has_symbols_or_rpf else "FAIL",
            "Components present in by_role/" if has_symbols_or_rpf else "Missing in by_role/",
            track="recomp",
        )
    )

    # Pillar 3: User-supplied local dump (MicrosoftGame.Config / game_scarlett_master.exe / eboot.bin / *.rpf)
    dump_found = False
    dump_detail = "No local dump detected — use 'Import Dump' in Settings to select your dump folder."
    candidate_dump_dirs = [d for d in (dump_folder, sandbox_root / "local_dump") if d]
    for ddir in candidate_dump_dirs:
        if ddir.exists():
            resolved_gdk = resolve_gdk_title_exe(ddir, allow_fallback=False)
            if resolved_gdk:
                dump_found = True
                exe_name = Path(resolved_gdk["exe"]).name
                dump_detail = f"Verified via {resolved_gdk['source']}: {exe_name} in {ddir}"
                break
            matches = [
                f.name
                for f in ddir.rglob("*")
                if f.is_file()
                and (
                    f.name.lower() in {"game_scarlett_master.exe", "eboot.bin", "microsoftgame.config"}
                    or f.suffix.lower() in {".rpf", ".elf", ".sprx", ".xvd"}
                )
            ]
            if matches:
                dump_found = True
                dump_detail = f"{len(matches)} dump file(s) verified in {ddir} ({', '.join(matches[:3])})"
                break
    checks.append(
        BootCheckItem(
            "Pillar 3: Local Dump (game_scarlett_master.exe)",
            "PASS" if dump_found else "FAIL",
            dump_detail,
            track="both",
        )
    )

    # 6. Test syntaxe C++ (clang++ -fsyntax-only) & intégrité ZIP sans exécution
    compiled_ok = 0
    compiled_fail = 0
    html_skipped = 0
    zip_ok = 0
    zip_bad = 0

    for fp in collected_files[:30]:
        try:
            head = fp.read_bytes()[:512]
        except OSError:
            continue

        if head.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(fp, "r") as zf:
                    if zf.testzip() is None:
                        zip_ok += 1
                    else:
                        zip_bad += 1
            except Exception:
                zip_bad += 1

        clean_name = fp.name[:-11] if fp.name.lower().endswith(".quarantine") else fp.name
        if clang_exe and clean_name.lower().endswith((".cpp", ".cc", ".c")) and b"\x00" not in head:
            head_low = head.lstrip().lower()
            if head_low.startswith((b"<!doctype html", b"<html")):
                html_skipped += 1
                continue
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".cpp", mode="wb", delete=False
                ) as tf:
                    tf.write(fp.read_bytes()[:200_000])
                    tmp_cpp = Path(tf.name)
                try:
                    proc = subprocess.run(
                        [clang_exe, "-fsyntax-only", "-std=c++20", str(tmp_cpp)],
                        capture_output=True,
                        timeout=8,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if proc.returncode == 0:
                        compiled_ok += 1
                    else:
                        compiled_fail += 1
                finally:
                    tmp_cpp.unlink(missing_ok=True)
            except Exception:
                compiled_fail += 1

    if zip_ok or zip_bad:
        checks.append(
            BootCheckItem(
                "ZIP Archive Integrity",
                "PASS" if zip_bad == 0 else "WARN",
                f"{zip_ok} valid archive(s), {zip_bad} corrupted",
                track="both",
            )
        )

    if compiled_ok or compiled_fail or html_skipped:
        parts = []
        if compiled_ok:
            parts.append(f"{compiled_ok} OK")
        if compiled_fail:
            parts.append(f"{compiled_fail} with missing external headers")
        if html_skipped:
            parts.append(f"{html_skipped} HTML page(s) skipped (re-download)")
        checks.append(
            BootCheckItem(
                "C++20 Syntax Check (clang++ -fsyntax-only)",
                "PASS" if (compiled_fail == 0 and html_skipped == 0) else "WARN",
                ", ".join(parts),
                track="recomp",
            )
        )

    def _calc_pct(subset: list[BootCheckItem]) -> int:
        if not subset:
            return 0
        ok_cnt = sum(1 for c in subset if c.status == "PASS")
        return int(round((ok_cnt / len(subset)) * 100))

    recomp_subset = [c for c in checks if c.track in ("recomp", "both")]
    xbox_subset = [c for c in checks if c.track in ("xbox", "both")]

    readiness = _calc_pct(checks)
    recomp_pct = _calc_pct(recomp_subset)
    xbox_pct = _calc_pct(xbox_subset)

    has_fail = any(c.status == "FAIL" for c in checks)
    overall = "READY" if (not has_fail and readiness >= 90) else ("PARTIAL" if readiness >= 50 else "BLOCKED")

    summary = (
        f"PC Recomp Ready: {recomp_pct}% | Console Emu Ready: {xbox_pct}% "
        f"(Overall: {readiness}%). "
        + (
            "Environment is ready to launch."
            if overall == "READY"
            else "Some optional or required checks need attention (see table below)."
        )
    )

    return BootTestReport(
        readiness_pct=readiness,
        overall_status=overall,
        checks=checks,
        compiled_sources=compiled_ok,
        failed_sources=compiled_fail,
        summary=summary,
        recomp_readiness_pct=recomp_pct,
        xbox_readiness_pct=xbox_pct,
    )
