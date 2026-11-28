from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
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
        "registry": "crates.io",
        "package": "bcpack-rs",
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
        "requires": {"mods": ["openrage"], "min_runtime": "", "notes": "Requires OpenRAGE base definitions"},
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
        "requires": {"mods": ["open-vi-rpf9"], "min_runtime": "", "notes": "Requires OpenVI (RPF9 Explorer) to browse RPF9 audio packs"},
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

    def set_mods_dir(self, mods_dir: Path) -> None:
        """Updates the target Mods directory (called when downloads_dir changes)."""
        self.mods_dir = mods_dir
        self.mods_dir.mkdir(parents=True, exist_ok=True)

    def set_state_path(self, state_path: Path) -> None:
        """Switches the active mods_state.json file (used when changing isolated profiles)."""
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_semver(ver: str) -> tuple[int, ...]:
        nums = [int(x) for x in re.findall(r"\d+", str(ver or ""))]
        return tuple(nums[:4]) if nums else ()

    @classmethod
    def _read_mod_requires(cls, mod_entry: dict[str, Any]) -> dict[str, Any]:
        """Reads prerequisite dependencies from catalog metadata or a mod folder's mod.json."""
        req = mod_entry.get("requires") if isinstance(mod_entry.get("requires"), dict) else None
        if req is None:
            mid = str(mod_entry.get("id") or "")
            cat_match = next((w for w in WORKSHOP_CATALOG if w["id"] == mid), None)
            if cat_match and isinstance(cat_match.get("requires"), dict):
                req = cat_match["requires"]
        if req is None:
            raw_path = str(mod_entry.get("path") or "").strip()
            if raw_path:
                mj = Path(raw_path) / "mod.json"
                if mj.is_file():
                    try:
                        data = json.loads(mj.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and isinstance(data.get("requires"), dict):
                            req = data["requires"]
                    except Exception:
                        pass
        if not isinstance(req, dict):
            return {"mods": [], "min_runtime": "", "notes": ""}
        mods_req = [str(x).strip() for x in (req.get("mods") or []) if str(x).strip()]
        return {
            "mods": mods_req,
            "min_runtime": str(req.get("min_runtime") or "").strip(),
            "notes": str(req.get("notes") or "").strip(),
        }

    def evaluate_dependencies(
        self,
        mods: list[dict[str, Any]] | None = None,
        *,
        runtime_version: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Evaluates prerequisite mod/hook/runtime dependencies for every installed mod."""
        installed = mods if mods is not None else self.sync_disk_mods()
        by_id = {str(m.get("id") or ""): m for m in installed if isinstance(m, dict) and m.get("id")}
        ws_names = {w["id"]: w["name"] for w in WORKSHOP_CATALOG}
        cur_rt_ver = self._parse_semver(runtime_version)

        required_by: dict[str, list[str]] = {}
        raw_reqs: dict[str, dict[str, Any]] = {}
        for m in installed:
            mid = str(m.get("id") or "")
            req = self._read_mod_requires(m)
            raw_reqs[mid] = req
            if m.get("enabled", True):
                for dep_id in req["mods"]:
                    required_by.setdefault(dep_id, []).append(str(m.get("name") or mid))

        out: dict[str, dict[str, Any]] = {}
        for m in installed:
            mid = str(m.get("id") or "")
            req = raw_reqs[mid]
            missing_mods: list[dict[str, str]] = []
            for dep_id in req["mods"]:
                dep_rec = by_id.get(dep_id)
                dep_name = str((dep_rec or {}).get("name") or ws_names.get(dep_id) or dep_id)
                if dep_rec is None:
                    missing_mods.append({"id": dep_id, "name": dep_name, "reason": "not installed"})
                elif not dep_rec.get("enabled", True):
                    missing_mods.append({"id": dep_id, "name": dep_name, "reason": "disabled"})

            missing_runtime = ""
            min_rt = req["min_runtime"]
            if min_rt:
                min_tuple = self._parse_semver(min_rt)
                if min_tuple and cur_rt_ver and cur_rt_ver < min_tuple:
                    missing_runtime = f"Requires runtime >= {min_rt} (active: {runtime_version or 'unknown'})"

            satisfied = not missing_mods and not missing_runtime
            parts: list[str] = []
            if missing_mods:
                parts.append(
                    "Missing: " + ", ".join(f"{x['name']} ({x['reason']})" for x in missing_mods)
                )
            if missing_runtime:
                parts.append(missing_runtime)
            out[mid] = {
                "requires": req,
                "satisfied": satisfied,
                "missing_mods": missing_mods,
                "missing_runtime": missing_runtime,
                "required_by": required_by.get(mid, []),
                "summary": " · ".join(parts) if parts else ("Dependencies OK" if (req["mods"] or min_rt) else ""),
            }
        return out
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

    def get_state(
        self,
        portwatch_tools: list[dict[str, Any]] | None = None,
        hub_items: list[dict[str, Any]] | None = None,
        registry_versions: dict[str, dict[str, Any]] | None = None,
        game_dir: Path | None = None,
        stage_dir: Path | None = None,
        vanilla: bool = False,
        runtime_version: str = "",
    ) -> dict[str, Any]:
        installed = self.sync_disk_mods()
        inst_ids = {m.get("id") for m in installed}
        known_urls = {w["url"].rstrip("/").lower() for w in WORKSHOP_CATALOG}
        reg_map = registry_versions or {}
        workshop: list[dict[str, Any]] = []
        for w in WORKSHOP_CATALOG:
            entry = {**w, "installed": w["id"] in inst_ids}
            reg_info = reg_map.get(w["id"]) or (reg_map.get(f"{w.get('registry')}:{w.get('package')}") if w.get("package") else None)
            if isinstance(reg_info, dict) and reg_info.get("version"):
                entry["version"] = f"v{reg_info['version']}" if not str(reg_info["version"]).startswith("v") else str(reg_info["version"])
                entry["source_hub"] = str(reg_info.get("registry") or w.get("registry") or "").upper()
                if reg_info.get("download_url"):
                    entry["download_url"] = str(reg_info["download_url"])
            workshop.append(entry)
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
        # Dynamically append items from Modding Hubs (Nexus Mods, GameBanana, RSS portals) & Package Registries
        seen_ids = {w["id"] for w in workshop}
        for hi in (hub_items or []):
            if not isinstance(hi, dict):
                continue
            hid = str(hi.get("id") or "").strip()
            hurl = str(hi.get("url") or "").strip()
            if not hid or not hurl or hid in seen_ids or hurl.rstrip("/").lower() in known_urls:
                continue
            seen_ids.add(hid)
            known_urls.add(hurl.rstrip("/").lower())
            workshop.append({**hi, "installed": hid in inst_ids})

        resolved_stage = stage_dir or (self.mods_dir.parent / "mod_stage")
        link_info = self.verify_game_links(game_dir, resolved_stage, vanilla=vanilla, mods=installed)
        per_mod_links = link_info.get("per_mod") or {}
        dep_graph = self.evaluate_dependencies(installed, runtime_version=runtime_version)
        enriched_installed = [
            {
                **m,
                "game_link": per_mod_links.get(str(m.get("id") or ""), {"status": "ready", "label": "Ready"}),
                "dependencies": dep_graph.get(str(m.get("id") or ""), {"satisfied": True, "missing_mods": [], "required_by": []}),
            }
            for m in installed
        ]
        # Annotate Workshop cards with missing prerequisite status
        enabled_ids = {str(m.get("id") or "") for m in installed if m.get("enabled", True)}
        ws_names = {w["id"]: w["name"] for w in WORKSHOP_CATALOG}
        for w in workshop:
            req = self._read_mod_requires(w)
            missing = [
                {"id": d, "name": ws_names.get(d, d), "reason": "not installed" if d not in inst_ids else "disabled"}
                for d in req["mods"]
                if d not in enabled_ids
            ]
            w["requires"] = req
            w["missing_dependencies"] = missing
        return {
            "mods_dir": str(self.mods_dir),
            "installed": enriched_installed,
            "conflicts": self.detect_rpf_conflicts(installed),
            "game_link_status": link_info,
            "workshop": workshop,
        }

    @staticmethod
    def list_mod_target_files(mod_entry: dict[str, Any]) -> list[str]:
        """Lists relative target paths (especially .rpf archives and assets) provided by a mod without modifying anything."""
        raw_path = str(mod_entry.get("path") or "").strip()
        if not raw_path:
            return []
        p = Path(raw_path)
        if not p.exists():
            return []
        targets: list[str] = []
        if p.is_dir():
            for fp in sorted(p.rglob("*")):
                if not fp.is_file() or fp.name.startswith("."):
                    continue
                rel = fp.relative_to(p).as_posix()
                targets.append(rel)
        elif p.is_file():
            # Inspect ZIP archives (including .zip.quarantine) without extracting
            try:
                with p.open("rb") as fh:
                    sig = fh.read(4)
                if sig == b"PK\x03\x04":
                    with zipfile.ZipFile(p, "r") as zf:
                        names = [info.filename for info in zf.infolist() if not info.is_dir()]
                        # Strip a single top-level wrapper folder (common in GitHub archive zips)
                        top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
                        strip_prefix = f"{next(iter(top_dirs))}/" if (len(top_dirs) == 1 and all("/" in n for n in names)) else ""
                        for n in names:
                            rel = n[len(strip_prefix) :] if strip_prefix and n.startswith(strip_prefix) else n
                            if rel and not rel.startswith("."):
                                targets.append(rel)
                    return targets
            except Exception:
                pass
            clean_name = p.name[:-11] if p.name.lower().endswith(".quarantine") else p.name
            targets.append(clean_name)
        return targets

    def detect_rpf_conflicts(self, mods: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Detects conflicts when two or more active mods modify the same .rpf archive or target relative path."""
        items = mods if mods is not None else self.sync_disk_mods()
        target_map: dict[str, list[dict[str, str]]] = {}
        rpf_name_map: dict[str, list[dict[str, str]]] = {}

        for m in items:
            if not isinstance(m, dict) or not m.get("enabled", True):
                continue
            mid = str(m.get("id") or "")
            mname = str(m.get("name") or mid)
            seen_for_mod: set[str] = set()
            seen_rpf_for_mod: set[str] = set()
            for rel in self.list_mod_target_files(m):
                norm = rel.replace("\\", "/").lstrip("./").lower()
                if not norm or norm in seen_for_mod:
                    continue
                seen_for_mod.add(norm)
                target_map.setdefault(norm, []).append({"id": mid, "name": mname, "raw": rel})
                if norm.endswith(".rpf"):
                    rpf_base = Path(norm).name
                    if rpf_base not in seen_rpf_for_mod:
                        seen_rpf_for_mod.add(rpf_base)
                        rpf_name_map.setdefault(rpf_base, []).append({"id": mid, "name": mname, "raw": rel})

        conflicts: list[dict[str, Any]] = []
        reported_keys: set[str] = set()

        for norm, owners in target_map.items():
            if len(owners) > 1:
                reported_keys.add(norm)
                conflicts.append(
                    {
                        "target": owners[-1]["raw"],
                        "is_rpf": norm.endswith(".rpf"),
                        "mods": [o["name"] for o in owners],
                        "mod_ids": [o["id"] for o in owners],
                        "winner": owners[-1]["name"],
                    }
                )

        for rpf_base, owners in rpf_name_map.items():
            if len(owners) > 1:
                # Avoid duplicate if already reported under identical exact relative path
                exact_keys = {o["raw"].replace("\\", "/").lstrip("./").lower() for o in owners}
                if len(exact_keys) > 1 or not any(k in reported_keys for k in exact_keys):
                    conflicts.append(
                        {
                            "target": rpf_base,
                            "is_rpf": True,
                            "mods": [o["name"] for o in owners],
                            "mod_ids": [o["id"] for o in owners],
                            "winner": owners[-1]["name"],
                        }
                    )

        conflicts.sort(key=lambda c: (0 if c["is_rpf"] else 1, c["target"].lower()))
        return conflicts

    @staticmethod
    def _link_or_copy_file(src: Path, dst: Path) -> str:
        """Non-destructively links a file using symlink -> hardlink -> copy2 fallback."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        try:
            os.symlink(src.resolve(), dst)
            return "symlink"
        except (OSError, NotImplementedError):
            pass
        try:
            os.link(src.resolve(), dst)
            return "hardlink"
        except (OSError, NotImplementedError):
            pass
        shutil.copy2(src, dst)
        return "copy"

    GAME_ASSET_EXTS: frozenset[str] = frozenset(
        {
            ".rpf", ".ytd", ".ydr", ".yft", ".ymap", ".ytyp", ".awc",
            ".meta", ".xml", ".ini", ".cfg", ".dds", ".ttpf", ".lua", ".asi", ".dll",
        }
    )

    @classmethod
    def _mod_has_game_payload(cls, mod_entry: dict[str, Any]) -> bool:
        """Returns True if a mod provides game-injectable asset/script files (vs an external standalone tool repo)."""
        raw_path = str(mod_entry.get("path") or "").strip()
        if not raw_path:
            return False
        p = Path(raw_path)
        if not p.exists():
            return False
        if p.is_dir():
            return any(fp.is_file() and not fp.name.startswith(".") for fp in p.rglob("*"))
        if p.is_file():
            targets = cls.list_mod_target_files(mod_entry)
            return any(Path(t).suffix.lower() in cls.GAME_ASSET_EXTS for t in targets)
        return False

    @staticmethod
    def _cleanup_game_link(game_dir: Path | None) -> None:
        """Removes only SixDeck-managed link/overlay inside game_dir, never touching original game files."""
        if game_dir is None or not game_dir.is_dir():
            return
        link_path = game_dir / "sixdeck_mods"
        if link_path.is_symlink():
            try:
                link_path.unlink()
            except OSError:
                pass
        elif link_path.is_dir() and (link_path / ".sixdeck_managed").is_file():
            shutil.rmtree(link_path, ignore_errors=True)

    def deploy_non_destructive(self, stage_dir: Path, *, vanilla: bool = False) -> dict[str, Any]:
        """Deploys active mods in load order into an isolated staging directory without touching original dump files.

        Uses symlinks first, falling back to hardlinks then clean copies if OS privileges restrict symlinks.
        In Vanilla mode, immediately purges the staging directory so zero mods are loaded.
        """
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)

        if vanilla:
            return {
                "ok": True,
                "mode": "vanilla",
                "stage_dir": str(stage_dir),
                "deployed_files": 0,
                "deployed_mod_ids": [],
                "methods": {"symlink": 0, "hardlink": 0, "copy": 0},
                "conflicts": [],
            }

        installed = self.sync_disk_mods()
        conflicts = self.detect_rpf_conflicts(installed)
        stage_dir.mkdir(parents=True, exist_ok=True)
        methods = {"symlink": 0, "hardlink": 0, "copy": 0}
        deployed_files = 0
        deployed_mod_ids: list[str] = []

        for m in installed:
            if not isinstance(m, dict) or not m.get("enabled", True):
                continue
            mid = str(m.get("id") or "")
            raw_path = str(m.get("path") or "").strip()
            if not raw_path:
                continue
            src_root = Path(raw_path)
            if not src_root.exists():
                continue
            mod_deployed = 0
            if src_root.is_dir():
                for fp in sorted(src_root.rglob("*")):
                    if not fp.is_file() or fp.name.startswith("."):
                        continue
                    rel = fp.relative_to(src_root)
                    method = self._link_or_copy_file(fp, stage_dir / rel)
                    methods[method] += 1
                    deployed_files += 1
                    mod_deployed += 1
            elif src_root.is_file():
                # Safely extract only game asset/script files from ZIP archives (including .zip.quarantine)
                extracted_any = False
                try:
                    with src_root.open("rb") as fh:
                        sig = fh.read(4)
                    if sig == b"PK\x03\x04":
                        with zipfile.ZipFile(src_root, "r") as zf:
                            names = [info.filename for info in zf.infolist() if not info.is_dir()]
                            top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
                            strip_prefix = f"{next(iter(top_dirs))}/" if (len(top_dirs) == 1 and all("/" in n for n in names)) else ""
                            for info in zf.infolist():
                                if info.is_dir():
                                    continue
                                rel_str = info.filename[len(strip_prefix) :] if strip_prefix and info.filename.startswith(strip_prefix) else info.filename
                                rel_path = Path(rel_str)
                                if rel_path.is_absolute() or ".." in rel_path.parts:
                                    continue
                                if rel_path.suffix.lower() not in self.GAME_ASSET_EXTS:
                                    continue
                                dst = stage_dir / rel_path
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                dst.write_bytes(zf.read(info))
                                methods["copy"] += 1
                                deployed_files += 1
                                mod_deployed += 1
                                extracted_any = True
                except Exception:
                    extracted_any = False
                if not extracted_any and not src_root.name.lower().endswith(".quarantine"):
                    method = self._link_or_copy_file(src_root, stage_dir / src_root.name)
                    methods[method] += 1
                    deployed_files += 1
                    mod_deployed += 1
            if mod_deployed > 0 and mid:
                deployed_mod_ids.append(mid)

        manifest = {
            "mode": "modded",
            "deployed_files": deployed_files,
            "deployed_mod_ids": deployed_mod_ids,
            "conflicts": conflicts,
        }
        (stage_dir / "sixdeck_mods_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        return {
            "ok": True,
            "mode": "modded",
            "stage_dir": str(stage_dir),
            "deployed_files": deployed_files,
            "deployed_mod_ids": deployed_mod_ids,
            "methods": methods,
            "conflicts": conflicts,
        }

    def link_mods_to_game(
        self,
        game_dir: Path | None,
        stage_dir: Path,
        *,
        vanilla: bool = False,
    ) -> dict[str, Any]:
        """Deploys active mods to stage_dir and links <game_dir>/sixdeck_mods non-destructively (never alters original dump)."""
        dep = self.deploy_non_destructive(stage_dir, vanilla=vanilla)
        self._cleanup_game_link(game_dir)

        game_linked = False
        link_method = "none"
        link_path = (game_dir / "sixdeck_mods") if (game_dir and game_dir.is_dir()) else None

        if not vanilla and link_path is not None and dep.get("deployed_files", 0) > 0:
            try:
                os.symlink(stage_dir.resolve(), link_path, target_is_directory=True)
                game_linked = True
                link_method = "symlink"
            except (OSError, NotImplementedError):
                # Fallback: managed overlay directory using hardlinks/copies + marker file for safe Vanilla cleanup
                link_path.mkdir(parents=True, exist_ok=True)
                (link_path / ".sixdeck_managed").write_text("SixDeck managed non-destructive mod overlay\n", encoding="utf-8")
                for fp in stage_dir.rglob("*"):
                    if not fp.is_file():
                        continue
                    rel = fp.relative_to(stage_dir)
                    self._link_or_copy_file(fp, link_path / rel)
                game_linked = True
                link_method = "managed_overlay"

        verify = self.verify_game_links(game_dir, stage_dir, vanilla=vanilla)
        return {
            **dep,
            "game_linked": game_linked,
            "link_method": link_method,
            "game_link_path": str(link_path) if link_path else "",
            "game_link_status": verify,
        }

    def verify_game_links(
        self,
        game_dir: Path | None,
        stage_dir: Path,
        *,
        vanilla: bool = False,
        mods: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Verifies whether installed mods are properly linked to the configured game dump directory."""
        installed = mods if mods is not None else self.sync_disk_mods()
        game_exists = bool(game_dir and game_dir.is_dir())
        has_exe = bool(game_exists and game_dir and (game_dir / "game_scarlett_master.exe").is_file())
        link_path = (game_dir / "sixdeck_mods") if game_exists and game_dir else None

        link_active = False
        if link_path is not None:
            if link_path.is_symlink():
                try:
                    link_active = link_path.resolve() == stage_dir.resolve() and stage_dir.exists()
                except OSError:
                    link_active = False
            elif link_path.is_dir() and (link_path / ".sixdeck_managed").is_file():
                link_active = True

        staged_ids: set[str] = set()
        manifest_path = stage_dir / "sixdeck_mods_manifest.json"
        if manifest_path.is_file():
            try:
                mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
                staged_ids = {str(x) for x in (mdata.get("deployed_mod_ids") or [])}
            except Exception:
                staged_ids = set()

        per_mod: dict[str, dict[str, Any]] = {}
        linked_cnt = 0
        ready_cnt = 0
        tools_cnt = 0
        broken_cnt = 0

        for m in installed:
            mid = str(m.get("id") or "")
            enabled = bool(m.get("enabled", True))
            raw_path = str(m.get("path") or "").strip()
            exists = bool(raw_path and Path(raw_path).exists())
            has_payload = self._mod_has_game_payload(m) if exists else False
            file_count = len(self.list_mod_target_files(m)) if exists else 0

            if not enabled:
                per_mod[mid] = {"status": "disabled", "label": "Disabled", "detail": "Toggle ON to activate", "file_count": file_count}
            elif vanilla:
                per_mod[mid] = {"status": "vanilla", "label": "Vanilla Mode", "detail": "Unlinked while Vanilla Mode is active", "file_count": file_count}
            elif not exists:
                broken_cnt += 1
                per_mod[mid] = {"status": "broken", "label": "Missing Files", "detail": "Mod files not found on disk", "file_count": 0}
            elif not has_payload:
                tools_cnt += 1
                per_mod[mid] = {"status": "tool", "label": "External Tool", "detail": "Standalone tool archive (no .rpf/game assets to inject)", "file_count": file_count}
            elif not has_exe:
                ready_cnt += 1
                per_mod[mid] = {"status": "missing_dump", "label": "Game Dump Missing", "detail": "Import your local game dump in Settings to link", "file_count": file_count}
            elif link_active and mid in staged_ids:
                linked_cnt += 1
                per_mod[mid] = {"status": "linked", "label": "Linked to Game", "detail": f"Linked non-destructively in {link_path}", "file_count": file_count}
            else:
                ready_cnt += 1
                per_mod[mid] = {"status": "ready", "label": "Ready to Link", "detail": "Click 'Link to Game' or Launch to stage & link", "file_count": file_count}

        if vanilla:
            overall = "vanilla"
            summary = "Vanilla Mode is active — all mods are unlinked and original game dump files are untouched."
        elif not installed:
            overall = "no_mods"
            summary = "No mods installed yet."
        elif not has_exe:
            overall = "missing_dump"
            summary = f"Game dump not configured ({len(installed)} mod(s) installed) — import your dump in Settings to link mods to the game."
        elif linked_cnt > 0 and ready_cnt == 0 and broken_cnt == 0:
            overall = "linked"
            summary = f"{linked_cnt} active game mod(s) linked to {link_path} (non-destructive overlay)."
        elif ready_cnt > 0:
            overall = "ready"
            summary = f"{linked_cnt} linked · {ready_cnt} active mod(s) waiting to be linked to the game dump."
        elif tools_cnt > 0:
            overall = "tools_only"
            summary = f"{tools_cnt} external Workshop tool(s) installed (no direct game asset files to inject)."
        else:
            overall = "ready"
            summary = f"{len(installed)} mod(s) tracked."

        return {
            "status": overall,
            "summary": summary,
            "game_dir": str(game_dir or ""),
            "game_ready": has_exe,
            "vanilla_mode": vanilla,
            "stage_dir": str(stage_dir),
            "game_link_path": str(link_path) if link_path else "",
            "game_link_active": link_active,
            "linked_count": linked_cnt,
            "ready_count": ready_cnt,
            "tools_count": tools_cnt,
            "broken_count": broken_cnt,
            "per_mod": per_mod,
        }

    def delete_mod(
        self,
        mod_id: str,
        *,
        stage_dir: Path | None = None,
        game_dir: Path | None = None,
        vanilla: bool = False,
    ) -> dict[str, Any]:
        """Removes a mod from mods_state.json, deletes its local files inside mods_dir, and updates game links."""
        mods = self._load()
        target = next((m for m in mods if isinstance(m, dict) and m.get("id") == mod_id), None)
        if not target:
            return {"ok": False, "error": "Mod not found."}

        mod_name = str(target.get("name") or mod_id)
        remaining = [m for m in mods if not (isinstance(m, dict) and m.get("id") == mod_id)]
        self._save(remaining)

        files_removed = False
        raw_path = str(target.get("path") or "").strip()
        if raw_path:
            try:
                p = Path(raw_path).resolve()
                allowed_root = self.mods_dir.resolve()
                if allowed_root in p.parents and p.exists():
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                    files_removed = True
            except OSError:
                files_removed = False

        if stage_dir is not None:
            self.link_mods_to_game(game_dir, stage_dir, vanilla=vanilla)

        return {
            "ok": True,
            "deleted_id": mod_id,
            "deleted_name": mod_name,
            "files_removed": files_removed,
            "message": f"Deleted mod '{mod_name}'" + (" and removed its files." if files_removed else "."),
        }

    def apply_mods_order(self, mods_order: list[dict[str, Any]]) -> dict[str, Any]:
        """Applies load order and enabled states from an imported preset to matching locally installed mods."""
        mods = self.sync_disk_mods()
        if not mods_order:
            return {"ok": True, "matched": 0}
        by_id = {str(m.get("id") or ""): m for m in mods if isinstance(m, dict) and m.get("id")}
        ordered: list[dict[str, Any]] = []
        matched = 0
        seen: set[str] = set()
        for entry in mods_order:
            mid = str(entry.get("id") or "").strip()
            if mid and mid in by_id and mid not in seen:
                rec = by_id[mid]
                if "enabled" in entry:
                    rec["enabled"] = bool(entry["enabled"])
                ordered.append(rec)
                seen.add(mid)
                matched += 1
        for m in mods:
            mid = str(m.get("id") or "")
            if mid not in seen:
                ordered.append(m)
        self._save(ordered)
        return {"ok": True, "matched": matched}

    def toggle(self, mod_id: str, enabled: bool, *, runtime_version: str = "") -> dict[str, Any]:
        mods = self.sync_disk_mods()
        target = next((m for m in mods if m.get("id") == mod_id), None)
        if not target:
            return {"ok": False, "error": "Mod not found."}
        if bool(enabled):
            dep_info = self.evaluate_dependencies(mods, runtime_version=runtime_version).get(mod_id)
            if dep_info and not dep_info.get("satisfied", True):
                return {
                    "ok": False,
                    "blocked_by_dependencies": True,
                    "missing_mods": dep_info.get("missing_mods") or [],
                    "missing_runtime": dep_info.get("missing_runtime") or "",
                    "error": f"Cannot enable '{target.get('name') or mod_id}': {dep_info.get('summary')}",
                }
        else:
            dep_info = self.evaluate_dependencies(mods, runtime_version=runtime_version).get(mod_id)
            req_by = (dep_info or {}).get("required_by") or []
            if req_by:
                return {
                    "ok": False,
                    "blocked_by_dependencies": True,
                    "required_by": req_by,
                    "error": f"Cannot disable '{target.get('name') or mod_id}': required by active mod(s) {', '.join(req_by)}.",
                }
        target["enabled"] = bool(enabled)
        self._save(mods)
        return {"ok": True, "mod": target}

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
