from __future__ import annotations

import getpass
import json
import os
import platform
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
WIN_USER_PATH_RE = re.compile(r"([A-Za-z]:\\Users\\)([^\\/\s\"']+)", re.IGNORECASE)
POSIX_HOME_PATH_RE = re.compile(r"(/(?:home|Users)/)([^/\s\"']+)")
SECRET_KV_RE = re.compile(
    r"(?i)\b(GITHUB_TOKEN|VIRUSTOTAL_API_KEY|DISCORD_WEBHOOK_URL|STEAMGRIDDB_API_KEY|API_KEY|ACCESS_TOKEN|AUTHORIZATION|BEARER|PASSWORD|SECRET)\b(\s*[:=]\s*)([^\s\"'&;,]+)"
)
GITHUB_PAT_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")
DISCORD_WEBHOOK_RE = re.compile(r"https?://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+")


def anonymize_text(
    text: str,
    *,
    extra_secrets: list[str] | None = None,
    username: str | None = None,
) -> str:
    """Strips ANSI codes and strictly masks Windows/Linux usernames, home paths, and tokens/secrets."""
    cleaned = ANSI_RE.sub("", text or "")
    cleaned = WIN_USER_PATH_RE.sub(r"\1***", cleaned)
    cleaned = POSIX_HOME_PATH_RE.sub(r"\1***", cleaned)
    cleaned = GITHUB_PAT_RE.sub("***REDACTED_TOKEN***", cleaned)
    cleaned = DISCORD_WEBHOOK_RE.sub("https://discord.com/api/webhooks/***/***", cleaned)
    cleaned = SECRET_KV_RE.sub(r"\1\2***REDACTED***", cleaned)

    # Mask explicit user home & current username if detected
    detected_users: set[str] = set()
    if username:
        detected_users.add(username)
    for env_key in ("USERNAME", "USER"):
        val = (os.environ.get(env_key) or "").strip()
        if len(val) >= 3:
            detected_users.add(val)
    try:
        gp = getpass.getuser()
        if gp and len(gp) >= 3:
            detected_users.add(gp)
    except Exception:
        pass

    for u in detected_users:
        cleaned = re.sub(rf"([\\/]){re.escape(u)}(?=[\\/\s\"']|$)", r"\1***", cleaned, flags=re.IGNORECASE)

    for sec in extra_secrets or []:
        s = (sec or "").strip()
        if len(s) >= 6:
            cleaned = cleaned.replace(s, "***REDACTED***")

    return cleaned


def detect_gpu_and_driver() -> dict[str, str]:
    """Detects local GPU model and driver version safely without popping CMD windows."""
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=no_win,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout.strip())
                items = data if isinstance(data, list) else [data]
                names = [str(i.get("Name") or "").strip() for i in items if isinstance(i, dict) and i.get("Name")]
                drivers = [str(i.get("DriverVersion") or "").strip() for i in items if isinstance(i, dict) and i.get("DriverVersion")]
                return {
                    "gpu_name": " / ".join(names) or "Unknown GPU",
                    "driver_version": " / ".join(drivers) or "Unknown",
                }
        except Exception:
            pass
    else:
        try:
            proc = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                vga = [ln.split(":", 2)[-1].strip() for ln in proc.stdout.splitlines() if "VGA" in ln or "3D" in ln]
                if vga:
                    return {"gpu_name": " / ".join(vga), "driver_version": platform.release()}
        except Exception:
            pass
    return {"gpu_name": "Unknown GPU", "driver_version": "Unknown"}


def generate_support_bundle(
    output_dir: Path,
    *,
    settings: dict[str, Any],
    system_memory: dict[str, Any],
    mods_installed: list[dict[str, Any]],
    mod_conflicts: list[dict[str, Any]],
    game_log_path: Path | None = None,
    launcher_logs: list[str] | None = None,
    last_crash: dict[str, Any] | None = None,
    runtime_version: str = "",
    portable_mode: bool = False,
    extra_secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Generates both an anonymized GitHub Issue Markdown summary and a sanitized Support Bundle .zip."""
    output_dir.mkdir(parents=True, exist_ok=True)
    gpu_info = detect_gpu_and_driver()
    now = datetime.now(timezone.utc)
    ts_tag = now.strftime("%Y%m%d_%H%M%S")
    zip_path = output_dir / f"sixdeck_support_bundle_{ts_tag}.zip"

    # Collect & clean game logs
    game_log_lines: list[str] = []
    if game_log_path and game_log_path.is_file():
        try:
            size = game_log_path.stat().st_size
            with game_log_path.open("rb") as fh:
                if size > 262_144:
                    fh.seek(size - 262_144)
                raw = fh.read().decode("utf-8", errors="replace")
            game_log_lines = [anonymize_text(ln, extra_secrets=extra_secrets) for ln in raw.splitlines()[-300:]]
        except OSError:
            game_log_lines = ["(Unable to read game log file)"]

    clean_launcher_logs = [
        anonymize_text(ln, extra_secrets=extra_secrets) for ln in (launcher_logs or [])[-120:]
    ]

    # Anonymize settings & mods
    safe_settings = {
        k: (anonymize_text(str(v), extra_secrets=extra_secrets) if isinstance(v, str) else v)
        for k, v in settings.items()
        if not any(sk in k.lower() for sk in ("token", "secret", "password", "api_key", "webhook"))
    }
    active_mods = [
        {
            "order": idx + 1,
            "id": str(m.get("id") or ""),
            "name": str(m.get("name") or ""),
            "version": str(m.get("version") or "local"),
            "enabled": bool(m.get("enabled", True)),
            "risk": str(m.get("risk") or "assets"),
        }
        for idx, m in enumerate(mods_installed)
    ]

    env_summary = {
        "generated_at_utc": now.isoformat(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()[:60]})",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "portable_mode": portable_mode,
        "gpu": gpu_info["gpu_name"],
        "gpu_driver": gpu_info["driver_version"],
        "gpu_profile": safe_settings.get("gpu_profile", "NVIDIA"),
        "system_memory": system_memory,
        "runtime": safe_settings.get("selected_runtime", "unknown"),
        "runtime_version": runtime_version or "unknown",
        "vanilla_mode": bool(safe_settings.get("vanilla_mode", False)),
        "mods_active_count": sum(1 for m in active_mods if m["enabled"]),
        "mods_conflicts_count": len(mod_conflicts),
    }

    mods_md = (
        "\n".join(
            f"  {m['order']}. `[{'ON' if m['enabled'] else 'OFF'}]` {m['name']} ({m['version']}, {m['risk']})"
            for m in active_mods
        )
        or "  - None"
    )
    crash_excerpt = ""
    if last_crash and last_crash.get("excerpt"):
        crash_excerpt = anonymize_text(str(last_crash["excerpt"]), extra_secrets=extra_secrets)
    elif game_log_lines:
        crash_excerpt = "\n".join(game_log_lines[-35:])

    markdown_lines = [
        "## SixDeck Support Report",
        "",
        "### Environment",
        f"- **OS:** {env_summary['os']} ({env_summary['arch']})",
        f"- **GPU:** {env_summary['gpu']} (Driver: `{env_summary['gpu_driver']}`)",
        f"- **GPU Profile:** `{env_summary['gpu_profile']}` | **Portable Mode:** `{portable_mode}`",
        f"- **RAM / Commit:** {system_memory.get('free_ram_gb', 0)} / {system_memory.get('total_ram_gb', 0)} GB RAM free · "
        f"{system_memory.get('free_commit_gb', 0)} / {system_memory.get('total_commit_gb', 0)} GB Commit free",
        f"- **Runtime:** `{env_summary['runtime']}` (`{env_summary['runtime_version']}`)",
        f"- **Graphics:** `{safe_settings.get('resolution', '1920x1080')}` · Scale `{safe_settings.get('render_scale', '1.0')}` · "
        f"FPS `{safe_settings.get('fps_limit', '60')}` · Console `{safe_settings.get('console_profile', 'auto')}` · "
        f"RT `{safe_settings.get('ray_tracing', False)}`",
        f"- **Vanilla Mode:** `{env_summary['vanilla_mode']}` | **RPF Conflicts:** `{len(mod_conflicts)}`",
        "",
        "### Mods (Load Order)",
        mods_md,
        "",
    ]
    if mod_conflicts:
        markdown_lines.append("### Detected Mod Conflicts")
        for c in mod_conflicts[:15]:
            markdown_lines.append(
                f"- `{c.get('target', '')}`: {', '.join(c.get('mods') or [])} (winner: **{c.get('winner', '')}**)"
            )
        markdown_lines.append("")

    if crash_excerpt:
        markdown_lines.extend(
            [
                "### Recent Logs (Anonymized)",
                "```text",
                crash_excerpt[-4000:],
                "```",
            ]
        )

    markdown_report = anonymize_text("\n".join(markdown_lines), extra_secrets=extra_secrets)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("REPORT.md", markdown_report)
        zf.writestr(
            "environment.json",
            anonymize_text(json.dumps(env_summary, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
        )
        zf.writestr(
            "settings_sanitized.json",
            anonymize_text(json.dumps(safe_settings, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
        )
        zf.writestr(
            "mods.json",
            anonymize_text(
                json.dumps({"mods": active_mods, "conflicts": mod_conflicts}, indent=2, ensure_ascii=False),
                extra_secrets=extra_secrets,
            ),
        )
        zf.writestr("game_logs_clean.txt", "\n".join(game_log_lines) or "(No game logs)")
        zf.writestr("launcher_logs_clean.txt", "\n".join(clean_launcher_logs) or "(No launcher logs)")

    return {
        "ok": True,
        "zip_path": str(zip_path),
        "zip_name": zip_path.name,
        "markdown": markdown_report,
        "environment": env_summary,
    }
