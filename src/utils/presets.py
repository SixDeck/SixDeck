from __future__ import annotations

import base64
import json
import zlib
from typing import Any

import httpx

PRESET_SCHEMA = "sixdeck-preset-v1"
SHARE_PREFIX = "SIXDECK1:"

ALLOWED_PRESET_SETTING_KEYS: tuple[str, ...] = (
    "selected_runtime",
    "gpu_profile",
    "fps_limit",
    "resolution",
    "render_scale",
    "console_profile",
    "ray_tracing",
    "vanilla_mode",
    "launch_in_sandboxie",
    "show_logs",
    "low_ram_mode",
    "dlss_mode",
    "fg_type",
)

BUILTIN_COMMUNITY_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "nvidia-balanced-1080p",
        "name": "NVIDIA Balanced (1080p · 60 FPS)",
        "gpu": "NVIDIA",
        "author": "SixDeck Community",
        "description": "Recommended for NVIDIA 8 GB+ VRAM: cool_fox Direct, Series X profile, 1080p 60 FPS, Ray Tracing OFF.",
        "settings": {
            "selected_runtime": "cool_fox (Direct)",
            "gpu_profile": "NVIDIA",
            "fps_limit": "60",
            "resolution": "1920x1080",
            "render_scale": "1.0",
            "console_profile": "series-x",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "nvidia-low-vram-720p",
        "name": "NVIDIA Low VRAM / 16 GB RAM (720p · 75%)",
        "gpu": "NVIDIA",
        "author": "SixDeck Community",
        "description": "Optimized for 6-8 GB GPUs and 16 GB RAM: Series S profile, 720p, 75% Render Scale, steady 30 FPS cap.",
        "settings": {
            "selected_runtime": "cool_fox (Direct)",
            "gpu_profile": "NVIDIA",
            "fps_limit": "30",
            "resolution": "1280x720",
            "render_scale": "0.75",
            "console_profile": "series-s",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "amd-radeon-1080p",
        "name": "AMD Radeon Optimized (1080p · 60 FPS)",
        "gpu": "AMD",
        "author": "SixDeck Community",
        "description": "Recommended for AMD Radeon (RX 6000/7000): AMD component filter, ScarlettPort/VIctory, 1080p 60 FPS.",
        "settings": {
            "selected_runtime": "ScarlettPort (Direct)",
            "gpu_profile": "AMD",
            "fps_limit": "60",
            "resolution": "1920x1080",
            "render_scale": "1.0",
            "console_profile": "auto",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "amd-performance-900p",
        "name": "AMD High-FPS Performance (900p · 85%)",
        "gpu": "AMD",
        "author": "SixDeck Community",
        "description": "Prioritizes frame rate and memory stability on AMD GPUs: 900p, 85% Render Scale, Series S profile.",
        "settings": {
            "selected_runtime": "VIctory (Direct)",
            "gpu_profile": "AMD",
            "fps_limit": "60",
            "resolution": "1600x900",
            "render_scale": "0.85",
            "console_profile": "series-s",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "superbuild-16gb-fast",
        "name": "Superbuild-VI · 16 GB Fast (Series S + DLSS Balanced + FSR 3 FG)",
        "gpu": "NVIDIA",
        "author": "Lumen / RedSyl",
        "description": "Tuned for 16 GB RAM and RTX 20/30+ GPUs: Series S + Low-RAM mode, DLSS Balanced, FSR 3 Frame Gen on RTX 20/30.",
        "settings": {
            "selected_runtime": "Superbuild-VI (Direct)",
            "gpu_profile": "NVIDIA",
            "fps_limit": "Unlimited",
            "resolution": "1920x1080",
            "render_scale": "1.0",
            "console_profile": "auto",
            "low_ram_mode": "auto",
            "dlss_mode": "balanced",
            "fg_type": "auto",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "superbuild-ultralow-ram",
        "name": "Superbuild-VI · Ultra-Low RAM (16 GB Tight · 75% Texture Cap)",
        "gpu": "ALL",
        "author": "Lumen / RedSyl",
        "description": "Minimum memory commit for tight 16 GB systems: Series S + Ultra-Low RAM, DLSS Performance (NVIDIA), Frame Gen OFF, 75% texture room cap.",
        "settings": {
            "selected_runtime": "Superbuild-VI (Direct)",
            "gpu_profile": "ALL",
            "fps_limit": "Unlimited",
            "resolution": "1280x720",
            "render_scale": "0.85",
            "console_profile": "series-s",
            "low_ram_mode": "ultralow",
            "dlss_mode": "performance",
            "fg_type": "off",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "superbuild-series-x",
        "name": "Superbuild-VI · Series X Quality (24+ GB RAM · 1440p)",
        "gpu": "ALL",
        "author": "Lumen / RedSyl",
        "description": "Higher visual quality profile for 24+ GB RAM systems: Series X, 1440p output, DLSS Performance + Frame Generation.",
        "settings": {
            "selected_runtime": "Superbuild-VI (Direct)",
            "gpu_profile": "ALL",
            "fps_limit": "Unlimited",
            "resolution": "2560x1440",
            "render_scale": "1.0",
            "console_profile": "series-x",
            "low_ram_mode": "off",
            "dlss_mode": "performance",
            "fg_type": "auto",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
        "mods_order": [],
    },
    {
        "id": "vanilla-diagnostic",
        "name": "Clean Vanilla Diagnostic (All GPUs)",
        "gpu": "ALL",
        "author": "SixDeck Community",
        "description": "Vanilla mode without mods, live logs enabled, and 720p output to quickly isolate crashes or rendering bugs.",
        "settings": {
            "gpu_profile": "ALL",
            "fps_limit": "60",
            "resolution": "1280x720",
            "render_scale": "1.0",
            "console_profile": "auto",
            "ray_tracing": False,
            "vanilla_mode": True,
            "show_logs": True,
        },
        "mods_order": [],
    },
)


def sanitize_preset_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Filters incoming preset settings to safe, known launcher keys only."""
    cleaned: dict[str, Any] = {}
    for k in ALLOWED_PRESET_SETTING_KEYS:
        if k in raw and raw[k] is not None:
            cleaned[k] = raw[k]
    return cleaned


def export_preset(
    settings: dict[str, Any],
    mods_list: list[dict[str, Any]],
    *,
    name: str = "My SixDeck Preset",
    author: str = "Community User",
    description: str = "",
) -> dict[str, Any]:
    """Exports a lightweight preset JSON + compact shareable string (for Discord / GitHub)."""
    clean_settings = sanitize_preset_settings(settings)
    mods_order = [
        {"id": str(m.get("id") or ""), "enabled": bool(m.get("enabled", True))}
        for m in mods_list
        if isinstance(m, dict) and m.get("id")
    ]
    preset = {
        "schema": PRESET_SCHEMA,
        "name": (name or "My SixDeck Preset").strip()[:80],
        "author": (author or "Community User").strip()[:60],
        "gpu": str(clean_settings.get("gpu_profile") or "ALL").upper(),
        "description": (description or "").strip()[:240],
        "settings": clean_settings,
        "mods_order": mods_order,
    }
    raw_json = json.dumps(preset, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    share_code = SHARE_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw_json, level=9)).decode("ascii")
    return {
        "preset": preset,
        "json_text": json.dumps(preset, indent=2, ensure_ascii=False),
        "share_code": share_code,
    }


def parse_preset_input(raw_text: str) -> dict[str, Any]:
    """Parses either raw JSON text or a compact `SIXDECK1:...` shareable string into a validated preset dict."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Preset payload is empty.")

    if text.startswith(SHARE_PREFIX):
        encoded = text[len(SHARE_PREFIX) :].strip()
        try:
            raw_bytes = zlib.decompress(base64.urlsafe_b64decode(encoded.encode("ascii")))
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid SixDeck share string: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON preset: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Preset must be a JSON object.")

    raw_settings = data.get("settings") if isinstance(data.get("settings"), dict) else data
    clean_settings = sanitize_preset_settings(raw_settings)
    if not clean_settings:
        raise ValueError("Preset contains no recognized SixDeck settings.")

    mods_order: list[dict[str, Any]] = []
    if isinstance(data.get("mods_order"), list):
        for entry in data["mods_order"]:
            if isinstance(entry, str) and entry.strip():
                mods_order.append({"id": entry.strip(), "enabled": True})
            elif isinstance(entry, dict) and entry.get("id"):
                mods_order.append({"id": str(entry["id"]).strip(), "enabled": bool(entry.get("enabled", True))})

    return {
        "schema": str(data.get("schema") or PRESET_SCHEMA),
        "name": str(data.get("name") or "Imported Preset").strip()[:80],
        "author": str(data.get("author") or "Community").strip()[:60],
        "gpu": str(data.get("gpu") or clean_settings.get("gpu_profile") or "ALL").upper(),
        "description": str(data.get("description") or "").strip()[:240],
        "settings": clean_settings,
        "mods_order": mods_order,
    }


def get_presets_for_gpu(
    gpu_profile: str = "ALL",
    extra_presets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Returns built-in + optional community presets ordered with GPU-matching presets first."""
    gpu_up = (gpu_profile or "ALL").strip().upper()
    combined = [dict(p) for p in BUILTIN_COMMUNITY_PRESETS]
    seen_ids = {p["id"] for p in combined}
    for ep in extra_presets or []:
        if not isinstance(ep, dict):
            continue
        eid = str(ep.get("id") or ep.get("name") or "").strip()
        if not eid or eid in seen_ids:
            continue
        try:
            parsed = parse_preset_input(json.dumps(ep))
            parsed["id"] = eid
            combined.append(parsed)
            seen_ids.add(eid)
        except ValueError:
            continue

    def _rank(p: dict[str, Any]) -> tuple[int, str]:
        pg = str(p.get("gpu") or "ALL").upper()
        if gpu_up != "ALL" and pg == gpu_up:
            return (0, p["name"])
        if pg == "ALL":
            return (1, p["name"])
        return (2, p["name"])

    combined.sort(key=_rank)
    return combined


async def fetch_remote_presets(url: str, *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Fetches a community presets JSON feed (list of presets or {'presets': [...]}) from a remote URL."""
    cleaned = (url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        return []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(cleaned, headers={"User-Agent": "SixDeck-Presets/1.0"})
        resp.raise_for_status()
        payload = resp.json()
    raw_list = payload.get("presets") if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        return []
    valid: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list[:40]):
        if not isinstance(item, dict):
            continue
        try:
            parsed = parse_preset_input(json.dumps(item))
            parsed["id"] = str(item.get("id") or f"remote-{idx}")
            parsed["remote"] = True
            valid.append(parsed)
        except ValueError:
            continue
    return valid
