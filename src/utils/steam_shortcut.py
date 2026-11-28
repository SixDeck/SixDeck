from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx


def compute_steam_shortcut_appid(exe_path: str, app_name: str) -> int:
    """Computes the deterministic 32-bit unsigned shortcut AppID used by Steam Big Picture & grids."""
    key = f"{exe_path}{app_name}".encode("utf-8")
    crc = zlib.crc32(key) & 0xFFFFFFFF
    return (crc | 0x80000000) & 0xFFFFFFFF


def _vdf_str(key: str, val: str) -> bytes:
    return b"\x01" + key.encode("utf-8") + b"\x00" + val.encode("utf-8") + b"\x00"


def _vdf_int(key: str, val: int) -> bytes:
    return b"\x02" + key.encode("utf-8") + b"\x00" + struct.pack("<I", val & 0xFFFFFFFF)


def _encode_shortcut_entry(index: int, entry: dict[str, Any]) -> bytes:
    buf = bytearray()
    buf.extend(b"\x00" + str(index).encode("utf-8") + b"\x00")
    buf.extend(_vdf_int("appid", int(entry.get("appid", 0))))
    buf.extend(_vdf_str("AppName", str(entry.get("AppName", "SixDeck"))))
    buf.extend(_vdf_str("Exe", str(entry.get("Exe", ""))))
    buf.extend(_vdf_str("StartDir", str(entry.get("StartDir", ""))))
    buf.extend(_vdf_str("icon", str(entry.get("icon", ""))))
    buf.extend(_vdf_str("ShortcutPath", str(entry.get("ShortcutPath", ""))))
    buf.extend(_vdf_str("LaunchOptions", str(entry.get("LaunchOptions", ""))))
    buf.extend(_vdf_int("IsHidden", int(entry.get("IsHidden", 0))))
    buf.extend(_vdf_int("AllowDesktopConfig", int(entry.get("AllowDesktopConfig", 1))))
    buf.extend(_vdf_int("AllowOverlay", int(entry.get("AllowOverlay", 1))))
    buf.extend(_vdf_int("OpenVR", int(entry.get("OpenVR", 0))))
    buf.extend(_vdf_int("Devkit", int(entry.get("Devkit", 0))))
    buf.extend(_vdf_str("DevkitGameID", str(entry.get("DevkitGameID", ""))))
    buf.extend(_vdf_int("LastPlayTime", int(entry.get("LastPlayTime", 0))))
    buf.extend(b"\x00tags\x00")
    for tidx, tag in enumerate(entry.get("tags") or []):
        buf.extend(_vdf_str(str(tidx), str(tag)))
    buf.extend(b"\x08")
    buf.extend(b"\x08")
    return bytes(buf)


def find_steam_userdata_dirs() -> list[Path]:
    """Locates local Steam userdata/<id>/config directories on Windows and Linux."""
    candidates: list[Path] = []
    if os.name == "nt":
        for root in (
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "userdata",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam" / "userdata",
        ):
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir() and child.name.isdigit():
                        cfg = child / "config"
                        if cfg.is_dir():
                            candidates.append(cfg)
    else:
        home = Path.home()
        for root in (home / ".steam" / "steam" / "userdata", home / ".local" / "share" / "Steam" / "userdata"):
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir() and child.name.isdigit():
                        cfg = child / "config"
                        if cfg.is_dir():
                            candidates.append(cfg)
    return candidates


def _fetch_steamgriddb_artwork(api_key: str, query: str, grid_dir: Path, appid: int) -> list[str]:
    """Optionally fetches SteamGridDB cover/hero artwork when the user provides a SteamGridDB API key."""
    if not api_key or not api_key.strip():
        return []
    saved: list[str] = []
    headers = {"Authorization": f"Bearer {api_key.strip()}", "User-Agent": "SixDeck/1.0"}
    try:
        with httpx.Client(follow_redirects=True, timeout=8.0) as client:
            r_search = client.get(
                f"https://www.steamgriddb.com/api/v2/search/autocomplete/{query}",
                headers=headers,
            )
            if r_search.status_code != 200:
                return []
            results = (r_search.json() or {}).get("data") or []
            if not results:
                return []
            game_id = results[0].get("id")
            if not game_id:
                return []

            grid_dir.mkdir(parents=True, exist_ok=True)
            for endpoint, suffix in (
                (f"https://www.steamgriddb.com/api/v2/grids/game/{game_id}", f"{appid}p.png"),
                (f"https://www.steamgriddb.com/api/v2/heroes/game/{game_id}", f"{appid}_hero.png"),
            ):
                r_art = client.get(endpoint, headers=headers)
                if r_art.status_code != 200:
                    continue
                art_items = (r_art.json() or {}).get("data") or []
                if art_items and art_items[0].get("url"):
                    img_resp = client.get(str(art_items[0]["url"]))
                    if img_resp.status_code == 200 and img_resp.content:
                        dest = grid_dir / suffix
                        dest.write_bytes(img_resp.content)
                        saved.append(str(dest))
    except Exception:
        pass
    return saved


def export_steam_shortcut(
    *,
    app_name: str,
    exe_path: Path,
    start_dir: Path,
    icon_path: Path | None = None,
    steam_config_dir: Path | None = None,
    steamgriddb_api_key: str | None = None,
    artwork_query: str = "SixDeck",
) -> dict[str, Any]:
    """Exports or appends a non-Steam shortcut entry in Steam's binary shortcuts.vdf (plus optional SteamGridDB art)."""
    cfg_dir = steam_config_dir
    if cfg_dir is None:
        found = find_steam_userdata_dirs()
        if not found:
            return {
                "ok": False,
                "error": "No local Steam userdata directory found. Install/sign into Steam first.",
            }
        cfg_dir = found[0]

    cfg_dir.mkdir(parents=True, exist_ok=True)
    vdf_path = cfg_dir / "shortcuts.vdf"
    quoted_exe = f'"{exe_path}"'
    quoted_dir = f'"{start_dir}"'
    appid = compute_steam_shortcut_appid(quoted_exe, app_name)

    entry = {
        "appid": appid,
        "AppName": app_name,
        "Exe": quoted_exe,
        "StartDir": quoted_dir,
        "icon": str(icon_path) if (icon_path and icon_path.exists()) else "",
        "tags": ["SixDeck"],
    }

    if vdf_path.exists():
        raw = vdf_path.read_bytes()
        # Avoid duplicating if already present
        if app_name.encode("utf-8") in raw and str(exe_path).encode("utf-8") in raw:
            art = _fetch_steamgriddb_artwork(steamgriddb_api_key or "", artwork_query, cfg_dir / "grid", appid)
            return {
                "ok": True,
                "already_exists": True,
                "vdf_path": str(vdf_path),
                "appid": appid,
                "artwork_files": art,
                "message": f"'{app_name}' is already present in Steam shortcuts ({vdf_path.name}). Restart Steam to refresh.",
            }
        # Append before the final closing 0x08 0x08
        if raw.endswith(b"\x08\x08"):
            # Count existing top-level shortcut entries heuristically
            next_idx = raw.count(b"\x01AppName\x00")
            new_raw = raw[:-2] + _encode_shortcut_entry(next_idx, entry) + b"\x08\x08"
            vdf_path.write_bytes(new_raw)
        else:
            vdf_path.write_bytes(b"\x00shortcuts\x00" + _encode_shortcut_entry(0, entry) + b"\x08\x08")
    else:
        vdf_path.write_bytes(b"\x00shortcuts\x00" + _encode_shortcut_entry(0, entry) + b"\x08\x08")

    art = _fetch_steamgriddb_artwork(steamgriddb_api_key or "", artwork_query, cfg_dir / "grid", appid)
    return {
        "ok": True,
        "already_exists": False,
        "vdf_path": str(vdf_path),
        "appid": appid,
        "artwork_files": art,
        "message": f"Steam shortcut exported to {vdf_path} (restart Steam to see '{app_name}').",
    }
