from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.utils.alt_git_mirrors import detect_forge_kind, resolve_forge_archive_url
from src.utils.hw_advisor import (
    decode_exit_code,
    detect_hardware_profile,
    get_system_memory_cross_platform,
    resolve_gdk_title_exe,
    score_runtime_for_hardware,
)
from src.utils.presets import BUILTIN_COMMUNITY_PRESETS, get_presets_for_gpu, parse_preset_input
from src.utils.shader_progress import (
    ShaderProgressTracker,
    build_shared_profile_env,
    build_superbuild_play_args,
    inspect_upscaler_sdk_state,
    load_timing_history,
    save_timing_history,
)
from src.utils.sys_optimizer import wrap_command_for_linux_compat


def test_resolve_gdk_title_exe_microsoft_game_config(tmp_path: Path) -> None:
    game_dir = tmp_path / "MyGDKTitle"
    game_dir.mkdir()
    (game_dir / "dev_tool.exe").write_bytes(b"MZ" + b"\x00" * 128)
    (game_dir / "title_scarlett.exe").write_bytes(b"MZ" + b"\x00" * 128)
    (game_dir / "MicrosoftGame.Config").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <Game configVersion="1">
          <ExecutableList>
            <Executable Name="dev_tool.exe" IsDevOnly="true" TargetDeviceFamily="Scarlett" />
            <Executable Name="title_scarlett.exe" TargetDeviceFamily="Scarlett" />
          </ExecutableList>
        </Game>""",
        encoding="utf-8",
    )

    res = resolve_gdk_title_exe(game_dir, allow_fallback=False)
    assert res is not None
    assert res["source"] == "MicrosoftGame.Config"
    assert Path(res["exe"]).name == "title_scarlett.exe"


def test_resolve_gdk_title_exe_blocks_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "evil.exe"
    outside.write_bytes(b"MZ" + b"\x00" * 64)
    game_dir = tmp_path / "SafeTitle"
    game_dir.mkdir()
    (game_dir / "MicrosoftGame.Config").write_text(
        """<Game><ExecutableList><Executable Name="../evil.exe" /></ExecutableList></Game>""",
        encoding="utf-8",
    )
    assert resolve_gdk_title_exe(game_dir, allow_fallback=False) is None


def test_resolve_gdk_title_exe_known_and_fallback(tmp_path: Path) -> None:
    g1 = tmp_path / "Dump1"
    g1.mkdir()
    (g1 / "game_scarlett_master.exe").write_bytes(b"MZ" + b"\x00" * 64)
    r1 = resolve_gdk_title_exe(g1, allow_fallback=False)
    assert r1 is not None and r1["source"] == "known-exe"

    g2 = tmp_path / "Dump2"
    g2.mkdir()
    (g2 / "victory.exe").write_bytes(b"\x00" * (2 * 1024 * 1024))
    (g2 / "custom_title.exe").write_bytes(b"\x00" * (2 * 1024 * 1024))
    r2 = resolve_gdk_title_exe(g2, allow_fallback=True)
    assert r2 is not None
    assert r2["source"] == "folder-exe"
    assert Path(r2["exe"]).name == "custom_title.exe"


def test_decode_ntstatus_exit_codes() -> None:
    av = decode_exit_code(-1073741819)  # 0xC0000005
    assert av["name"] == "STATUS_ACCESS_VIOLATION"
    assert "0xC0000005" in av["formatted"]
    assert av["hint"]

    oom = decode_exit_code(0xC0000017)
    assert oom["name"] == "STATUS_NO_MEMORY"
    assert "pagefile" in oom["hint"].lower()

    ok = decode_exit_code(0)
    assert ok["formatted"] == "0"
    assert ok["hint"] == ""


def test_cross_platform_memory_and_hardware_profile() -> None:
    mem = get_system_memory_cross_platform()
    assert "total_ram_gb" in mem and "pagefile_gb" in mem and "low_pagefile" in mem
    if os.name != "nt" and Path("/proc/meminfo").is_file():
        assert mem["total_ram_gb"] > 0.0

    hw = detect_hardware_profile()
    assert hw["hw_id"].startswith("hw")
    assert isinstance(hw["warnings"], list)
    assert hw["gpu_vendor"] in {"nvidia", "amd", "intel", "unknown"}


def test_objective_hardware_fit_scorer() -> None:
    hw_16gb_nv = {"gpu_vendor_name": "NVIDIA", "ram_gb": 16.0}
    s_nv = score_runtime_for_hardware(
        "Superbuild-VI (Direct)",
        hw=hw_16gb_nv,
        pw_entry={"score_pct": 82.0, "fps": 22, "gpu": ["NVIDIA", "AMD"]},
        local_record={"fps": 30, "crashed": False},
        installed=True,
        supports_low_ram=True,
    )
    s_amd_only = score_runtime_for_hardware(
        "AMD-Only-Fork",
        hw=hw_16gb_nv,
        pw_entry={"score_pct": 85.0, "fps": 25, "gpu": ["AMD"]},
        installed=False,
        supports_low_ram=False,
    )
    assert s_nv["score"] > s_amd_only["score"]
    assert "low-RAM mode" in s_nv["why"]
    assert "AMD only" in s_amd_only["why"]


def test_shader_progress_tracker_and_timings(tmp_path: Path) -> None:
    tr = ShaderProgressTracker("superbuild-vi", now=100.0)
    tr.feed_line("=== Precompiling shaders (shared runtime env) ===", now=100.0)
    tr.feed_line("shaders      1000 ready (1000 new, 0 cached) | elapsed 10s (100/s)", now=110.0)
    tr.feed_line("shaders      5000 ready (5000 new, 0 cached) | elapsed 40s (125/s)", now=140.0)
    snap_sh = tr.snapshot(now=140.0)
    assert snap_sh["phase"] == "shaders"
    assert snap_sh["count"] == 5000
    assert snap_sh["percent"] is not None and snap_sh["percent"] > 10.0
    assert "left" in snap_sh["eta"]

    tr.feed_line("8500 pipelines known, 200 made before (10s), 0 bad; 1800 to make, 4 processes", now=200.0)
    tr.feed_line("warm         900/1800 (50.0%) | elapsed 30s | ETA 30s (30.0/s)", now=230.0)
    snap_w = tr.snapshot(now=230.0)
    assert snap_w["phase"] == "warm"
    assert snap_w["percent"] == 50.0
    assert snap_w["eta"] == "30s left"

    tr.feed_line("into the pipeline library: 1800 pipelines (loading and slow ones), 4 threads", now=260.0)
    tr.feed_line("library      900/1800 (50.0%) | elapsed 10s | ETA 10s", now=270.0)
    snap_l = tr.snapshot(now=270.0)
    assert snap_l["phase"] == "library"
    assert snap_l["percent"] == 50.0

    tr.close_phase(now=280.0)
    timings_file = tmp_path / "timings.json"
    save_timing_history(timings_file, "hw12345", "superbuild-vi", tr.recorded)
    loaded = load_timing_history(timings_file, "hw12345")
    assert "superbuild-vi|shaders" in loaded
    assert loaded["superbuild-vi|shaders"]["total"] == 5000


def test_superbuild_args_and_shared_env_parity(tmp_path: Path) -> None:
    args_16gb = build_superbuild_play_args(
        r"D:\Game\game_scarlett_master.exe",
        vendor="nvidia",
        older_rtx=True,
        ram_gb=16.0,
        console_profile="auto",
        low_ram_mode="auto",
        dlss_mode="auto",
        fg_type="auto",
        fps_limit="Unlimited",
    )
    assert "--low-ram" in args_16gb
    assert "--series-s" in args_16gb
    assert "--dlss" in args_16gb and "balanced" in args_16gb
    assert "--fg-type" in args_16gb and "fsr" in args_16gb

    args_ultra = build_superbuild_play_args(
        r"D:\Game\game_scarlett_master.exe",
        vendor="nvidia",
        older_rtx=True,
        ram_gb=16.0,
        low_ram_mode="ultralow",
    )
    assert "--no-framegen-ready" in args_ultra
    assert "VICTORY_TEXTURE_ROOM_PERCENT=75" in args_ultra

    # Shared profile env strips stale unknown VICTORY_* vars while keeping cache-key parity vars
    base_env = {"PATH": "/usr/bin", "VICTORY_STALE_JUNK": "999", "VICTORY_SHADER_OPT": "1"}
    env = build_shared_profile_env(
        base_env,
        spec_env={"VICTORY_CACHE_DIR": str(tmp_path / "cache")},
        game_dir=tmp_path,
        victory_home=tmp_path / "home",
        ram_gb=16.0,
        low_ram_mode="ultralow",
        is_superbuild=True,
        play_args=args_ultra,
    )
    assert "VICTORY_STALE_JUNK" not in env
    assert env["VICTORY_SHADER_OPT"] == "1"
    assert env["VICTORY_EARLY_DEPTH"] == "1"
    assert env["VICTORY_HOST_MAX_WAVE"] == "32"
    assert env["VICTORY_LOW_RAM"] == "1"
    assert env["VICTORY_TEXTURE_ROOM_PERCENT"] == "75"


def test_upscaler_sdk_state_detection(tmp_path: Path) -> None:
    st0 = inspect_upscaler_sdk_state(tmp_path, vendor="nvidia", older_rtx=True, low_ram_mode="auto")
    assert st0["need_dlss"] is True
    assert st0["need_fsr"] is True
    assert st0["ready"] is False

    dlss_dll = tmp_path / "nvidia-sdk" / "Streamline" / "bin" / "x64" / "sl.interposer.dll"
    dlss_dll.parent.mkdir(parents=True)
    dlss_dll.write_bytes(b"MZ")
    amd_dir = tmp_path / "amd-fidelityfx"
    amd_dir.mkdir(parents=True)
    (amd_dir / "amd_fidelityfx_loader_dx12.dll").write_bytes(b"MZ")
    (amd_dir / "amd_fidelityfx_framegeneration_dx12.dll").write_bytes(b"MZ")

    st1 = inspect_upscaler_sdk_state(tmp_path, vendor="nvidia", older_rtx=True, low_ram_mode="auto")
    assert st1["dlss_ready"] is True
    assert st1["fsr_ready"] is True
    assert st1["ready"] is True


def test_gitgud_forge_detection_and_archive_url() -> None:
    assert detect_forge_kind("https://gitgud.io/RedSyl/Superbuild-VI") == "gitlab"
    assert detect_forge_kind("https://gitgud.io/RedSyl/Lumen.git") == "gitlab"
    arch = resolve_forge_archive_url("https://gitgud.io/RedSyl/Superbuild-VI")
    assert arch == "https://gitgud.io/RedSyl/Superbuild-VI/-/archive/main/Superbuild-VI-main.zip"


def test_superbuild_presets_present_and_parseable() -> None:
    ids = {p["id"] for p in BUILTIN_COMMUNITY_PRESETS}
    assert {"superbuild-16gb-fast", "superbuild-ultralow-ram", "superbuild-series-x"}.issubset(ids)
    ranked = get_presets_for_gpu("NVIDIA")
    assert any(p["id"] == "superbuild-16gb-fast" for p in ranked)
    parsed = parse_preset_input(
        '{"name":"Test SB","settings":{"low_ram_mode":"ultralow","dlss_mode":"performance","fg_type":"off"}}'
    )
    assert parsed["settings"]["low_ram_mode"] == "ultralow"
    assert parsed["settings"]["fg_type"] == "off"


def test_linux_proton_command_wrapping(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Linux/POSIX compatibility wrapping test")
    cmd, env, kind = wrap_command_for_linux_compat(
        ["/opt/victory.exe", "play", "/game/title.exe"],
        {"PATH": "/usr/bin"},
        compat_runner={"kind": "proton", "path": "/opt/proton"},
        compat_home=tmp_path,
    )
    assert kind == "proton"
    assert cmd[:2] == ["/opt/proton", "run"]
    assert env["PROTON_ENABLE_NVAPI"] == "1"
    assert env["DXVK_ENABLE_NVAPI"] == "1"
    assert env["VKD3D_CONFIG"] == "dxr11"
    assert Path(env["STEAM_COMPAT_DATA_PATH"]).is_dir()
