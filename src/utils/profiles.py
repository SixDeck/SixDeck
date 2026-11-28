from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_ID_RE = re.compile(r"[^a-z0-9_-]+")

PROFILE_SETTING_KEYS: tuple[str, ...] = (
    "selected_runtime",
    "gpu_profile",
    "fps_limit",
    "resolution",
    "render_scale",
    "console_profile",
    "ray_tracing",
    "vanilla_mode",
    "show_logs",
    "launch_in_sandboxie",
    "pinned_roles",
    "low_ram_mode",
    "dlss_mode",
    "fg_type",
)

DEFAULT_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "default",
        "name": "Default",
        "description": "Primary launcher profile with your standard settings, saves, and mods.",
        "builtin": True,
        "settings": {},
    },
    {
        "id": "vanilla",
        "name": "Vanilla",
        "description": "Pure unmodded profile with isolated saves and Vanilla Mode enabled.",
        "builtin": True,
        "settings": {
            "vanilla_mode": True,
            "ray_tracing": False,
            "fps_limit": "60",
            "resolution": "1920x1080",
            "render_scale": "1.0",
        },
    },
    {
        "id": "perf-60",
        "name": "Perf 60 FPS",
        "description": "Performance-tuned profile targeting steady 60 FPS (900p, 85% scale, Series S, RT OFF).",
        "builtin": True,
        "settings": {
            "fps_limit": "60",
            "resolution": "1600x900",
            "render_scale": "0.85",
            "console_profile": "series-s",
            "ray_tracing": False,
            "vanilla_mode": False,
        },
    },
    {
        "id": "experimental",
        "name": "Experimental",
        "description": "Isolated sandbox profile for testing bleeding-edge runtimes, mods, and verbose logs.",
        "builtin": True,
        "settings": {
            "show_logs": True,
            "ray_tracing": False,
            "console_profile": "series-x",
            "vanilla_mode": False,
        },
    },
)


def slugify_profile_id(name: str) -> str:
    slug = PROFILE_ID_RE.sub("-", (name or "").strip().lower()).strip("-_")
    return slug[:40] or "profile"


class ProfileManager:
    """Manages isolated launcher profiles/modpacks (runtime, graphics settings, mods state, saves)."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.store_path = base_dir / "data" / "profiles.json"
        self.profiles_root = base_dir / "data" / "profiles"

    def _load_store(self) -> dict[str, Any]:
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                    return data
            except Exception:
                pass
        return {
            "active_profile": "default",
            "profiles": [dict(p) for p in DEFAULT_PROFILES],
        }

    def _save_store(self, data: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_profiles(self) -> dict[str, Any]:
        store = self._load_store()
        active = str(store.get("active_profile") or "default")
        profiles = store.get("profiles") or [dict(p) for p in DEFAULT_PROFILES]
        if not any(p.get("id") == active for p in profiles):
            active = "default"
        return {"active_profile": active, "profiles": profiles}

    def get_profile(self, identifier: str) -> dict[str, Any] | None:
        ident = (identifier or "").strip().lower()
        if not ident:
            return None
        for p in self.list_profiles()["profiles"]:
            if str(p.get("id") or "").lower() == ident or str(p.get("name") or "").strip().lower() == ident:
                return p
        return None

    def mods_state_path_for(self, profile_id: str, default_mods_state: Path) -> Path:
        pid = slugify_profile_id(profile_id)
        if pid == "default":
            return default_mods_state
        p = self.profiles_root / pid / "mods_state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def saves_dir_for(self, profile_id: str, appdata_root: Path) -> Path:
        pid = slugify_profile_id(profile_id)
        if pid == "default":
            d = appdata_root / "6ixpack" / "save"
        else:
            d = appdata_root / "6ixpack" / "profiles" / pid / "save"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def stage_dir_for(self, profile_id: str, downloads_dir: Path) -> Path:
        pid = slugify_profile_id(profile_id)
        if pid == "default":
            return downloads_dir / "mod_stage"
        return downloads_dir / "mod_stage" / pid

    def save_profile_settings(self, profile_id: str, settings: dict[str, Any]) -> None:
        store = self._load_store()
        pid = slugify_profile_id(profile_id)
        snap = {k: settings[k] for k in PROFILE_SETTING_KEYS if k in settings}
        for p in store["profiles"]:
            if p.get("id") == pid:
                p["settings"] = snap
                p["updated_at"] = datetime.now(timezone.utc).isoformat()
                break
        self._save_store(store)

    def create_profile(
        self,
        name: str,
        *,
        description: str = "",
        base_settings: dict[str, Any] | None = None,
        clone_mods_from: Path | None = None,
    ) -> dict[str, Any]:
        clean_name = (name or "").strip()[:48]
        if not clean_name:
            return {"ok": False, "error": "Profile name cannot be empty."}
        pid = slugify_profile_id(clean_name)
        store = self._load_store()
        if any(p.get("id") == pid for p in store["profiles"]):
            return {"ok": False, "error": f"A profile with ID '{pid}' already exists."}

        snap = {k: base_settings[k] for k in PROFILE_SETTING_KEYS if base_settings and k in base_settings}
        new_prof = {
            "id": pid,
            "name": clean_name,
            "description": (description or "Custom isolated profile (settings, mods & saves).").strip()[:180],
            "builtin": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "settings": snap,
        }
        store["profiles"].append(new_prof)
        self._save_store(store)

        if clone_mods_from and clone_mods_from.is_file():
            dst = self.profiles_root / pid / "mods_state.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clone_mods_from, dst)

        return {"ok": True, "profile": new_prof, "message": f"Created profile '{clean_name}'."}

    def set_active_profile(self, identifier: str) -> dict[str, Any]:
        store = self._load_store()
        ident = (identifier or "").strip().lower()
        target = next(
            (
                p
                for p in store["profiles"]
                if str(p.get("id") or "").lower() == ident
                or str(p.get("name") or "").strip().lower() == ident
            ),
            None,
        )
        if not target:
            return {"ok": False, "error": f"Profile '{identifier}' not found."}
        store["active_profile"] = target["id"]
        self._save_store(store)
        return {"ok": True, "profile": target}

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        pid = slugify_profile_id(profile_id)
        if pid == "default":
            return {"ok": False, "error": "The Default profile cannot be deleted."}
        store = self._load_store()
        target = next((p for p in store["profiles"] if p.get("id") == pid), None)
        if not target:
            return {"ok": False, "error": f"Profile '{profile_id}' not found."}
        if target.get("builtin"):
            return {"ok": False, "error": f"Built-in profile '{target['name']}' cannot be deleted."}
        store["profiles"] = [p for p in store["profiles"] if p.get("id") != pid]
        if store.get("active_profile") == pid:
            store["active_profile"] = "default"
        self._save_store(store)

        pdir = self.profiles_root / pid
        if pdir.is_dir():
            shutil.rmtree(pdir, ignore_errors=True)
        return {"ok": True, "deleted_id": pid, "active_profile": store["active_profile"], "message": f"Deleted profile '{target['name']}'."}
