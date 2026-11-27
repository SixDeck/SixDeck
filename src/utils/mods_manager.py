from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

# Curated open-source Workshop catalog with real preview images from each source repository
WORKSHOP_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "open-vi-rpf9",
        "name": "OpenVI (RPF9 Explorer)",
        "author": "TheoBIET",
        "version": "latest",
        "category": "Archives / RPF9",
        "risk": "assets",
        "desc": "OpenIV-style desktop explorer to browse RPF9 archives, 3D models, and catalog data.",
        "url": "https://github.com/TheoBIET/OpenVI",
        "image": "https://raw.githubusercontent.com/TheoBIET/OpenVI/main/docs/media/explorer.webp",
        "accent": "linear-gradient(135deg,#3b1d5e,#1a1030)",
    },
    {
        "id": "sixng-extractor",
        "name": "SixNG (glTF / Models & Map)",
        "author": "holdmysocks",
        "version": "latest",
        "category": "Models & Map",
        "risk": "assets",
        "desc": "Desktop app & CLI to export rigged glTF 3D models, textures, and map data from your local dump.",
        "url": "https://github.com/holdmysocks/SixNG",
        "image": "https://raw.githubusercontent.com/holdmysocks/SixNG/master/docs/images/model_viewer.png",
        "accent": "linear-gradient(135deg,#133b5c,#0f172a)",
    },
    {
        "id": "forge9-repacker",
        "name": "forge9 (RPF9 Extract & Repack)",
        "author": "nineforge0",
        "version": "latest",
        "category": "Archives / RPF9",
        "risk": "assets",
        "desc": "Rust RPF9 modding toolkit to inspect, extract, and repack archives using keys from your own dump.",
        "url": "https://github.com/nineforge0/forge9",
        "image": "https://opengraph.githubassets.com/1/nineforge0/forge9",
        "accent": "linear-gradient(135deg,#4c1d6e,#140f2d)",
    },
    {
        "id": "rage-tools-vi",
        "name": "Rage Tools VI (Archive & Scripts)",
        "author": "lynxnathan",
        "version": "latest",
        "category": "Audio & Scripts",
        "risk": "code",
        "desc": "Python RPF9 toolkit: rebuilds archive entries from verified payloads, decodes audio and script schemas.",
        "url": "https://github.com/lynxnathan/rage-tools-vi",
        "image": "https://opengraph.githubassets.com/1/lynxnathan/rage-tools-vi",
        "accent": "linear-gradient(135deg,#2e1a47,#12182b)",
    },
    {
        "id": "percystar",
        "name": "PercyStar (RPF9 / Bink2 / Audio)",
        "author": "rpatty-dev",
        "version": "latest",
        "category": "Archives / RPF9",
        "risk": "assets",
        "desc": "Fast Rust RPF9 explorer featuring an integrated Bink2 video player and ADAT Opus audio decoder.",
        "url": "https://github.com/rpatty-dev/PercyStar",
        "image": "https://opengraph.githubassets.com/1/rpatty-dev/PercyStar",
        "accent": "linear-gradient(135deg,#1e3a5f,#0f172a)",
    },
    {
        "id": "rpf9-explorer",
        "name": "rpf9-explorer (All-in-One Viewer)",
        "author": "breadbbbb",
        "version": "latest",
        "category": "Archives / RPF9",
        "risk": "assets",
        "desc": "Windows RPF9 archive browser with integrated 3D model, texture, audio, and video previews.",
        "url": "https://github.com/breadbbbb/rpf9-explorer",
        "image": "https://opengraph.githubassets.com/1/breadbbbb/rpf9-explorer",
        "accent": "linear-gradient(135deg,#3f1d54,#141126)",
    },
    {
        "id": "ttpf-studio",
        "name": "TTPF Studio (Textures & Bink)",
        "author": "woreksami",
        "version": "latest",
        "category": "Textures & Shaders",
        "risk": "assets",
        "desc": "View, export, and replace .ttpf texture packs and play Bink video streams.",
        "url": "https://github.com/woreksami/ttpf-studio",
        "image": "https://raw.githubusercontent.com/woreksami/ttpf-studio/main/assets/icon.png",
        "accent": "linear-gradient(135deg,#5a1d44,#1b1024)",
    },
    {
        "id": "bcpack-rs",
        "name": "BCPack-RS (Texture Codec)",
        "author": "Ahmet-Dedeler",
        "version": "latest",
        "category": "Textures & Shaders",
        "risk": "code",
        "desc": "Clean-room Rust decoder for console BCPack compressed textures (BC1 / BC7).",
        "url": "https://github.com/Ahmet-Dedeler/bcpack-rs",
        "image": "https://opengraph.githubassets.com/1/Ahmet-Dedeler/bcpack-rs",
        "accent": "linear-gradient(135deg,#134e4a,#0f172a)",
    },
    {
        "id": "vice-lens",
        "name": "Vice Lens (Map & Audio Explorer)",
        "author": "Librett0",
        "version": "latest",
        "category": "Models & Map",
        "risk": "assets",
        "desc": "Desktop explorer for map sectors, 3D models, animations, movies, and sound in your local copy.",
        "url": "https://github.com/Librett0/vice-lens",
        "image": "https://opengraph.githubassets.com/1/Librett0/vice-lens",
        "accent": "linear-gradient(135deg,#5c2d16,#1b1218)",
    },
    {
        "id": "rageforge",
        "name": "RageForge (RPF9 / TTPF Suite)",
        "author": "nullbrik",
        "version": "latest",
        "category": "Textures & Shaders",
        "risk": "assets",
        "desc": "Open-source toolkit built to inspect, parse, and analyze RPF9 containers and TTPF texture packs.",
        "url": "https://github.com/nullbrik/RageForge",
        "image": "https://opengraph.githubassets.com/1/nullbrik/RageForge",
        "accent": "linear-gradient(135deg,#4a1942,#161129)",
    },
    {
        "id": "rage9-re",
        "name": "rage9-re (Scripts & Animations)",
        "author": "Ahmet-Dedeler",
        "version": "latest",
        "category": "Audio & Scripts",
        "risk": "code",
        "desc": "Decodes RAGE 9 script PSO metadata and character/prop animation archives with glTF export.",
        "url": "https://github.com/Ahmet-Dedeler/rage9-re",
        "image": "https://opengraph.githubassets.com/1/Ahmet-Dedeler/rage9-re",
        "accent": "linear-gradient(135deg,#1e293b,#311042)",
    },
    {
        "id": "awc-audio-extractor",
        "name": "AWC Audio Extractor",
        "author": "Albotterin",
        "version": "latest",
        "category": "Audio & Scripts",
        "risk": "assets",
        "desc": "Extracts music, radio, and sound effect streams (.awc) directly from local RPF9 archives.",
        "url": "https://github.com/Albotterin/AWC_from_RPF9_Extractor",
        "image": "https://opengraph.githubassets.com/1/Albotterin/AWC_from_RPF9_Extractor",
        "accent": "linear-gradient(135deg,#164e63,#17122b)",
    },
    {
        "id": "openrage",
        "name": "OpenRAGE (Multi-Engine Suite)",
        "author": "Two-Play",
        "version": "latest",
        "category": "Archives / RPF9",
        "risk": "code",
        "desc": "Cross-platform open-source RAGE engine modding & archive toolkit for Windows and Linux.",
        "url": "https://github.com/Two-Play/openrage",
        "image": "https://opengraph.githubassets.com/1/Two-Play/openrage",
        "accent": "linear-gradient(135deg,#312e81,#111827)",
    },
    {
        "id": "6stars-world-viewer",
        "name": "6stars (World & Weather Viewer)",
        "author": "meetox80",
        "version": "latest",
        "category": "Models & Map",
        "risk": "assets",
        "desc": "Standalone engine that renders the map geometry, vegetation, and weather cycles from local archives.",
        "url": "https://github.com/meetox80/6stars",
        "image": "https://opengraph.githubassets.com/1/meetox80/6stars",
        "accent": "linear-gradient(135deg,#3b1d5e,#0f2938)",
    },
)

GH_SLUG_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE)
MD_IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)|<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
OG_IMG_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)


def resolve_source_image(url: str) -> str:
    """Extracts a real preview/screenshot image from a GitHub repo README or external mod page (og:image)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    m = GH_SLUG_RE.search(url)
    try:
        with httpx.Client(follow_redirects=True, timeout=8.0) as client:
            if m:
                owner, repo = m.group(1), m.group(2).removesuffix(".git")
                for br in ("main", "master"):
                    resp = client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/README.md")
                    if resp.status_code == 200 and resp.text:
                        for a, b in MD_IMG_RE.findall(resp.text):
                            u = (a or b).strip()
                            if not u or any(s in u.lower() for s in ("shields.io", "badge", "workflows", ".svg")):
                                continue
                            if not u.startswith("http"):
                                u = f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/{u.lstrip('./')}"
                            return u
                        break
                return f"https://opengraph.githubassets.com/1/{owner}/{repo}"
            # Non-GitHub mod source (Nexus Mods, GameBanana, ModDB, etc.) -> og:image
            resp = client.get(url, headers={"User-Agent": "VI-Community-Launcher/1.0"})
            if resp.status_code == 200 and resp.text:
                og = OG_IMG_RE.search(resp.text[:30000])
                if og:
                    return og.group(1).strip()
    except Exception:
        pass
    return ""


def _find_local_mod_image(entry: Path) -> str:
    """Looks for a local thumbnail image inside a mod folder (preview/icon/thumb/cover)."""
    if not entry.is_dir():
        return ""
    for pat in ("preview.*", "thumb.*", "cover.*", "icon.*", "*.png", "*.jpg", "*.webp"):
        for img in entry.glob(pat):
            if img.is_file() and img.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                return f"/api/mod-image?path={img}"
    return ""


class ModsManager:
    """Local Mods manager (ON/OFF toggles, load order, preview images, Workshop catalog)."""

    def __init__(self, state_path: Path, mods_dir: Path) -> None:
        self.state_path = state_path
        self.mods_dir = mods_dir
        self.mods_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict[str, Any]]:
        if not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save(self, mods: list[dict[str, Any]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(mods, indent=2, ensure_ascii=False), encoding="utf-8")

    def sync_disk_mods(self) -> list[dict[str, Any]]:
        """Synchronizes folders/archives in Mods/ with mods_state.json and ensures preview images."""
        mods = self._load()
        ws_by_id = {w["id"]: w for w in WORKSHOP_CATALOG}
        by_id = {m.get("id"): m for m in mods if isinstance(m, dict) and m.get("id")}
        changed = False

        # Backfill/refresh real source images on already-registered mods
        for m in mods:
            mid = m.get("id")
            if mid in ws_by_id and m.get("image") != ws_by_id[mid]["image"]:
                m["image"] = ws_by_id[mid]["image"]
                changed = True
            elif not m.get("image"):
                if m.get("url"):
                    src_img = resolve_source_image(str(m["url"]))
                    if src_img:
                        m["image"] = src_img
                        changed = True
                if not m.get("image") and m.get("path"):
                    loc_img = _find_local_mod_image(Path(str(m["path"])))
                    if loc_img:
                        m["image"] = loc_img
                        changed = True

        if self.mods_dir.exists():
            for entry in sorted(self.mods_dir.iterdir()):
                if entry.name.startswith(".") or entry.name.lower() == "quarantine":
                    continue
                mid = entry.stem.lower().replace(" ", "-")
                if mid not in by_id:
                    is_code = entry.suffix.lower() in {".dll", ".asi", ".lua", ".py"}
                    rec = {
                        "id": mid,
                        "name": entry.stem,
                        "version": "local",
                        "category": "Code / Script" if is_code else "Local Mod",
                        "risk": "code" if is_code else "assets",
                        "enabled": True,
                        "path": str(entry),
                        "url": "",
                        "image": _find_local_mod_image(entry),
                    }
                    mods.append(rec)
                    by_id[mid] = rec
                    changed = True

        if changed:
            self._save(mods)
        return mods

    def get_state(self, portwatch_tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        installed = self.sync_disk_mods()
        inst_ids = {m.get("id") for m in installed}
        known_urls = {w["url"].rstrip("/").lower() for w in WORKSHOP_CATALOG}
        workshop = [
            {**w, "installed": w["id"] in inst_ids}
            for w in WORKSHOP_CATALOG
        ]
        # Dynamically append additional verified tools discovered via PortWatch VI
        skip_kinds = {"coordination hub", "curated list", "format notes", "format research", "front end"}
        for t in (portwatch_tools or []):
            url = str(t.get("url") or "").strip()
            repo = str(t.get("repo") or "").strip()
            kind = str(t.get("kind") or "Community Tool").strip()
            if not url or url.rstrip("/").lower() in known_urls or kind.lower() in skip_kinds:
                continue
            known_urls.add(url.rstrip("/").lower())
            raw_name = str(t.get("name") or repo.split("/")[-1]).strip()
            clean_name = re.sub(r"(?i)\b(?:grand\s*theft\s*auto\s*vi|gta\s*vi|gta\s*6|gta6)\b[-_\s]*", "VI ", raw_name).strip()
            clean_desc = re.sub(r"(?i)\b(?:grand\s*theft\s*auto\s*vi|gta\s*vi|gta\s*6|gta6)\b", "VI", str(t.get("desc") or "")).strip()
            wid = "pw-" + re.sub(r"[^a-z0-9]+", "-", (repo or raw_name).lower()).strip("-")
            cat = "Archives / RPF9"
            klow = kind.lower()
            if any(k in klow for k in ("texture", "shader", "codec")):
                cat = "Textures & Shaders"
            elif any(k in klow for k in ("world", "map", "model", "asset")):
                cat = "Models & Map"
            elif any(k in klow for k in ("audio", "script", "data")):
                cat = "Audio & Scripts"
            img = f"https://opengraph.githubassets.com/1/{repo}" if repo and "/" in repo else ""
            workshop.append(
                {
                    "id": wid,
                    "name": clean_name or raw_name,
                    "author": repo.split("/")[0] if "/" in repo else "Community",
                    "version": "latest",
                    "category": cat,
                    "risk": "code" if "script" in klow or "tool" in klow else "assets",
                    "desc": clean_desc,
                    "url": url,
                    "image": img,
                    "accent": "linear-gradient(135deg,#2a1847,#11162a)",
                    "installed": wid in inst_ids,
                }
            )
        return {
            "mods_dir": str(self.mods_dir),
            "installed": installed,
            "workshop": workshop,
        }

    def toggle(self, mod_id: str, enabled: bool) -> dict[str, Any]:
        mods = self.sync_disk_mods()
        for m in mods:
            if m.get("id") == mod_id:
                m["enabled"] = bool(enabled)
                self._save(mods)
                return {"ok": True, "mod": m}
        return {"ok": False, "error": "Mod not found."}

    def move(self, mod_id: str, direction: str) -> dict[str, Any]:
        mods = self.sync_disk_mods()
        idx = next((i for i, m in enumerate(mods) if m.get("id") == mod_id), -1)
        if idx < 0:
            return {"ok": False, "error": "Mod not found."}
        if direction == "up" and idx > 0:
            mods[idx - 1], mods[idx] = mods[idx], mods[idx - 1]
        elif direction == "down" and idx < len(mods) - 1:
            mods[idx + 1], mods[idx] = mods[idx], mods[idx + 1]
        self._save(mods)
        return {"ok": True}

    def register_installed_workshop_mod(
        self,
        workshop_id: str,
        saved_path: str,
        *,
        fallback_entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        w = next((x for x in WORKSHOP_CATALOG if x["id"] == workshop_id), None) or fallback_entry
        if not w:
            return {"ok": False, "error": "Unknown Workshop mod."}
        mods = self.sync_disk_mods()
        existing = next((m for m in mods if m.get("id") == workshop_id), None)
        if existing:
            existing["path"] = saved_path
            existing["enabled"] = True
            existing["image"] = w["image"]
        else:
            mods.append(
                {
                    "id": w["id"],
                    "name": w["name"],
                    "version": w["version"],
                    "category": w["category"],
                    "risk": w["risk"],
                    "enabled": True,
                    "path": saved_path,
                    "url": w["url"],
                    "image": w["image"],
                }
            )
        self._save(mods)
        return {"ok": True}

    def open_folder(self) -> dict[str, Any]:
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(self.mods_dir))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(self.mods_dir)])
        return {"ok": True}
