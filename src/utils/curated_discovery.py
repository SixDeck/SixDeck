from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_REPO_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

EXCLUDED_OWNERS = {
    "topics",
    "features",
    "sponsors",
    "marketplace",
    "settings",
    "notifications",
    "explore",
    "trending",
    "collections",
    "events",
    "orgs",
    "about",
    "pricing",
    "login",
    "signup",
    "site",
    "security",
    "apps",
}

CURATED_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "Ahmet-Dedeler/awesome-gta6-reverse-engineering",
        "https://raw.githubusercontent.com/Ahmet-Dedeler/awesome-gta6-reverse-engineering/main/README.md",
    ),
    (
        "pagiii/gta6-tracker (server.js)",
        "https://raw.githubusercontent.com/pagiii/gta6-tracker/main/server.js",
    ),
    (
        "pagiii/gta6-tracker (README)",
        "https://raw.githubusercontent.com/pagiii/gta6-tracker/main/README.md",
    ),
    (
        "portwatch-vi.xyz",
        "https://portwatch-vi.xyz/",
    ),
    (
        "4lwayz/portwatch-vi (README)",
        "https://raw.githubusercontent.com/4lwayz/portwatch-vi/main/README.md",
    ),
    (
        "4lwayz/portwatch-vi (tracker.json)",
        "https://raw.githubusercontent.com/4lwayz/portwatch-vi/main/data/tracker.json",
    ),
    (
        "vuyp.github.io/gta6-pc-race",
        "https://vuyp.github.io/gta6-pc-race/",
    ),
    (
        "vuyp/gta6-pc-race (data.json)",
        "https://raw.githubusercontent.com/vuyp/gta6-pc-race/main/src/data.json",
    ),
)

# Verified open-source community repositories filling Symbols/Config, Shaders/GPU, and Dumper/ELF-SPRX roles.
# Strictly clean-room open-source tools (zero copyrighted binaries, keys, or game dumps).
CURATED_COMPONENT_SOURCES: tuple[dict[str, Any], ...] = (
    # --- 4. Symbols / Config ---
    {
        "repo": "DurtyFree/gta-vi-data-dumps",
        "category": "decomp",
        "status": "Open-Source Metadata & Config",
        "summary": "[Symbols / Config] Parsed GTA VI metadata dumps (18 JSON/INI files, ~285k records) for symbol, hash, and config cross-referencing.",
        "language": "JSON",
        "score": 86,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["symbols", "config", "gta-vi-data-dumps", "RAGE9"],
    },
    {
        "repo": "ahmed605/GTA6-StringsDatabase",
        "category": "decomp",
        "status": "Open-Source Hash / Strings DB",
        "summary": "[Symbols / Config] MIT-licensed database of ~89k archive entry names & strings for RPF9 hash lookup and symbol mapping.",
        "language": "Text",
        "score": 84,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["symbols", "config", "GTA6-StringsDatabase", "hash lookup"],
    },
    {
        "repo": "liam-mcelhaney122/gta6-modding-sdk",
        "category": "decomp",
        "status": "Native Handlers SDK (6,829)",
        "summary": "[Symbols / Config] Open-source build 1.0.2.1 SDK with C/C++/Python/Luau native handler signatures and symbol headers.",
        "language": "C++",
        "score": 85,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["symbols", "config", "gta6-modding-sdk", "native handlers"],
    },
    # --- 6. Shaders / GPU ---
    {
        "repo": "kalijerry/victory-xcl-graft",
        "category": "emulation",
        "status": "RDNA2 → DXIL Shader Bridge",
        "summary": "[Shaders / GPU] Out-of-process bridge invoking XCL as a fallback RDNA2 → DXIL shader translator for VIctory shaders on Windows D3D12.",
        "language": "Python",
        "score": 88,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "dxil", "rdna2", "victory-xcl-graft"],
    },
    {
        "repo": "alexdeliu/rdna2-conformance",
        "category": "emulation",
        "status": "D3D12 Shader Conformance Probes",
        "summary": "[Shaders / GPU] Windows D3D12 GPU driver conformance probes validating RDNA2 → DXIL shader translators (VIctory, XCL & forks).",
        "language": "Rust",
        "score": 85,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "dxil", "rdna2", "rdna2-conformance"],
    },
    {
        "repo": "booomxd/rdna2-dis",
        "category": "emulation",
        "status": "RDNA2 (GFX10.3) Disassembler",
        "summary": "[Shaders / GPU] Open-source RDNA2 (gfx10.3) GPU shader disassembler and control-flow visualizer generated from AMD ISA specs.",
        "language": "Rust",
        "score": 84,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "rdna2", "gfx10.3", "rdna2-dis"],
    },
    {
        "repo": "wifi-revenge/rdna-isa-ref",
        "category": "emulation",
        "status": "RDNA / GFX10.3 ISA Tables",
        "summary": "[Shaders / GPU] Dependency-free Rust crate and JSON opcode tables for RDNA1/RDNA2 (gfx1010/gfx1030) shader lifters.",
        "language": "Rust",
        "score": 82,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "rdna2", "rdna-isa-ref"],
    },
    {
        "repo": "5headsmartbtw/leonida-framelab",
        "category": "emulation",
        "status": "Vulkan / D3D12 Frame SDK",
        "summary": "[Shaders / GPU] Experimental Windows Vulkan / D3D12 rendering SDK with C ABI & Rust bindings for temporal frame metadata.",
        "language": "C++",
        "score": 83,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "vulkan", "dx12", "leonida-framelab"],
    },
    {
        "repo": "HansKristian-Work/dxil-spirv",
        "category": "emulation",
        "status": "DXIL → SPIR-V Translator",
        "summary": "[Shaders / GPU] Open-source DXIL (Shader Model 6.x) to Vulkan SPIR-V shader translator used by vkd3d-proton and Vulkan runtimes.",
        "language": "C++",
        "score": 86,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["shaders", "dxil", "spir-v", "dxil-spirv"],
    },
    # --- 7. Dumper / ELF-SPRX ---
    {
        "repo": "emoose/xvdtool",
        "category": "dumps",
        "status": "Xbox Series XVD Inspector / Unpacker",
        "summary": "[Dumper / ELF-SPRX] Open-source C# CLI & LibXboxOne library to inspect, verify, and unpack legally owned Xbox One / Series .xvd packages.",
        "language": "C#",
        "score": 88,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["dumper", "xvd", "xvdtool", "scarlett"],
    },
    {
        "repo": "kry-vex-gta/XBOX-Inspector",
        "category": "dumps",
        "status": "Xbox Series Build Inspector",
        "summary": "[Dumper / ELF-SPRX] Read-only Rust desktop tool that inventories an unpacked Xbox Series build and checks RPF9 & dump structure.",
        "language": "Rust",
        "score": 85,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["dumper", "xbox-inspector", "scarlett", "rpf9"],
    },
    {
        "repo": "EchoStretch/ps5-app-dumper",
        "category": "dumps",
        "status": "PS5 App / ELF-SPRX Dumper",
        "summary": "[Dumper / ELF-SPRX] Open-source PS5 ELF payload to dump legally owned PS5 applications, ELFs, and SPRX libraries to USB.",
        "language": "C",
        "score": 86,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["dumper", "ps5", "elf", "sprx", "ps5-app-dumper"],
    },
    {
        "repo": "idlesauce/ps5-self-pager",
        "category": "dumps",
        "status": "PS5 SELF → ELF Dumper",
        "summary": "[Dumper / ELF-SPRX] Open-source PS5 SELF decryption/dump helper enabling plain ELF dumps of legally owned PS5 binaries.",
        "language": "C",
        "score": 84,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["dumper", "ps5", "self", "elf", "ps5-self-pager"],
    },
    {
        "repo": "julienlgy/PS5-SELF-TO-ELF",
        "category": "dumps",
        "status": "PS5 SELF → ELF Host Converter",
        "summary": "[Dumper / ELF-SPRX] Clean host-side Python tool converting unencrypted PS5 eboot.bin / .prx SELF containers into standard ELF.",
        "language": "Python",
        "score": 83,
        "gpus": ["NVIDIA", "AMD"],
        "tags": ["dumper", "ps5", "eboot.bin", "ps5-self-to-elf"],
    },
)


def build_curated_component_items() -> list[Any]:
    """Builds verified open-source ScrapedItems for Symbols/Config, Shaders/GPU, and Dumper/ELF-SPRX."""
    from datetime import datetime, timezone
    from src.models import Category, GitHubMetadata, ScrapedItem, SourceType

    now_iso = datetime.now(timezone.utc).isoformat()
    cat_map = {
        "decomp": Category.DECOMP,
        "emulation": Category.EMULATION,
        "dumps": Category.DUMPS,
        "ai_re": Category.AI_RE,
    }
    items: list[ScrapedItem] = []
    for entry in CURATED_COMPONENT_SOURCES:
        repo = str(entry["repo"])
        owner = repo.split("/")[0]
        gpus = list(entry.get("gpus") or ["NVIDIA", "AMD"])
        items.append(
            ScrapedItem(
                id=ScrapedItem.build_id(SourceType.GITHUB_REPO, repo.lower()),
                source=SourceType.GITHUB_REPO,
                category=cat_map.get(str(entry.get("category") or "decomp"), Category.DECOMP),
                playability_status=str(entry.get("status") or "Open-Source Tool"),
                title=repo,
                url=f"https://github.com/{repo}",
                summary=str(entry.get("summary") or ""),
                author=owner,
                published_at=now_iso,
                updated_at=now_iso,
                relevance_score=int(entry.get("score") or 82),
                tags=list(entry.get("tags") or []),
                github_meta=GitHubMetadata(
                    repo_full_name=repo,
                    primary_language=str(entry.get("language") or "Rust"),
                    owner_login=owner,
                    inspected=False,
                ),
                extra={
                    "curated_verified": True,
                    "portwatch_verified": True,
                    "portwatch_gpu": gpus,
                    "portwatch_score": float(entry.get("score") or 82),
                },
            )
        )
    return items

# Extrait aussi les slugs "owner/repo" cités dans server.js de pagiii/gta6-tracker
QUOTED_SLUG_RE = re.compile(r"['\"]([A-Za-z0-9_.-]{2,39}/[A-Za-z0-9_.-]{2,80})['\"]")


def extract_github_repos_from_text(text: str, *, include_quoted_slugs: bool = False) -> list[str]:
    """Extrait les dépôts GitHub uniques (owner/repo) depuis un README ou fichier JS/JSON."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(owner: str, repo: str) -> None:
        o = owner.strip()
        r = repo.strip().rstrip(".,;:)\"'`#")
        if r.lower().endswith(".git"):
            r = r[:-4]
        if not o or not r or o.lower() in EXCLUDED_OWNERS:
            return
        if r.lower() in {"issues", "pulls", "releases", "wiki", "actions", "blob", "tree", "raw"}:
            return
        if "." in r and r.lower().endswith((".js", ".json", ".css", ".html", ".png", ".svg", ".md", ".ico")):
            return
        slug = f"{o}/{r}"
        key = slug.lower()
        if key not in seen:
            seen.add(key)
            found.append(slug)

    for m in GITHUB_REPO_RE.finditer(text):
        _add(m.group(1), m.group(2))

    if include_quoted_slugs:
        for m in QUOTED_SLUG_RE.finditer(text):
            parts = m.group(1).split("/", 1)
            if len(parts) == 2 and not parts[0].startswith(("http", "api", "public", "static", "text", "application")):
                _add(parts[0], parts[1])

    return found


def load_discovered_repos(cache_path: Path) -> list[str]:
    if not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str) and "/" in x]
    except Exception:
        pass
    return []


def save_discovered_repos(cache_path: Path, repos: list[str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(sorted(set(repos), key=str.lower), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


async def discover_curated_repos(
    client: httpx.AsyncClient,
    *,
    already_watched: list[str],
    cache_path: Path,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Lit awesome-gta6-reverse-engineering et pagiii/gta6-tracker pour découvrir de nouveaux dépôts."""
    existing_lower = {r.lower() for r in already_watched}
    cached = load_discovered_repos(cache_path)
    cached_lower = {r.lower() for r in cached}

    newly_found: list[str] = []
    sources_checked = 0

    for label, url in CURATED_SOURCES:
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": "gta6-port-tracker/3.0"},
                timeout=timeout,
            )
            if resp.status_code != 200 or not resp.text:
                continue
            sources_checked += 1
            include_slugs = url.endswith(("server.js", "tracker.json"))
            repos = extract_github_repos_from_text(resp.text, include_quoted_slugs=include_slugs)
            for slug in repos:
                low = slug.lower()
                if low not in existing_lower and low not in cached_lower:
                    newly_found.append(slug)
                    cached.append(slug)
                    cached_lower.add(low)
        except Exception as exc:
            logger.debug("[curated] Échec lecture %s : %s", label, exc)

    if newly_found:
        save_discovered_repos(cache_path, cached)

    return {
        "ok": True,
        "sources_checked": sources_checked,
        "newly_discovered": newly_found,
        "total_discovered": len(cached),
        "all_discovered": cached,
    }


async def fetch_whats_new(
    url: str,
    *,
    github_token: str | None = None,
    timeout: float = 12.0,
    installed_since: str | None = None,
    gitea_hosts: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Récupère la version proposée (tag ou SHA) et les commits via flux Atom natifs GitHub (zéro quota) ou Alt-Git."""
    from src.utils.alt_git_mirrors import detect_forge_kind, fetch_forge_latest_info
    from src.utils.github_atom import fetch_github_commits_atom, fetch_github_releases_atom

    m = GITHUB_REPO_RE.search(url or "")
    if not m:
        forge = detect_forge_kind(url or "", gitea_hosts=gitea_hosts)
        if forge in {"codeberg", "gitlab", "sourcehut", "gitea"}:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                info = await fetch_forge_latest_info(url, client, gitea_hosts=gitea_hosts, timeout=timeout)
            if info.get("ok"):
                commits = info.get("commits") or []
                rel_tag = str(info.get("release_tag") or "")
                new_ver = rel_tag or (f"commit {commits[0]['sha']}" if commits else f"Latest ({forge})")
                return {
                    "ok": True,
                    "repo": str(info.get("repo") or ""),
                    "forge": forge,
                    "new_version": new_ver,
                    "release_tag": rel_tag,
                    "release_name": rel_tag,
                    "release_date": commits[0]["date"] if commits else "",
                    "release_notes": str(info.get("release_notes") or ""),
                    "commits": commits[:6],
                    "commits_since_install": [],
                }
        return {
            "ok": True,
            "repo": "",
            "new_version": "Latest archive",
            "release_name": "",
            "release_date": "",
            "release_notes": "Detailed preview available for GitHub (Atom), Codeberg, GitLab, SourceHut, and Gitea.",
            "commits": [],
            "commits_since_install": [],
        }

    repo = f"{m.group(1)}/{m.group(2)}"
    release_tag = ""
    release_name = ""
    release_date = ""
    release_notes = ""
    commits: list[dict[str, str]] = []
    source_mode = "github-atom"

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        # 1. Flux Atom natifs GitHub (/releases.atom & /commits/<branch>.atom) — gratuits, zéro quota REST API
        atom_releases = await fetch_github_releases_atom(repo, client, timeout=timeout)
        if atom_releases:
            top = atom_releases[0]
            release_tag = top.get("tag", "")
            release_name = top.get("name", "") or release_tag
            release_date = top.get("date", "")
            release_notes = top.get("notes", "")

        atom_commits = await fetch_github_commits_atom(repo, client, timeout=timeout, limit=8)
        if atom_commits:
            commits = [
                {
                    "message": c.get("message", ""),
                    "date": c.get("date", ""),
                    "author": c.get("author", ""),
                    "sha": c.get("sha", ""),
                }
                for c in atom_commits
            ]

        # 2. Fallback REST API uniquement si les flux Atom n'ont rien renvoyé (ex. branche non standard)
        if (not release_tag or not commits) and github_token:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gta6-port-tracker/3.0",
                "Authorization": f"Bearer {github_token}",
            }
            if not release_tag:
                try:
                    r_rel = await client.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers)
                    if r_rel.status_code == 200:
                        d = r_rel.json()
                        release_tag = str(d.get("tag_name") or "").strip()
                        release_name = str(d.get("name") or release_tag or "").strip()
                        release_date = str(d.get("published_at") or "")[:16].replace("T", " ")
                        release_notes = str(d.get("body") or "").strip()[:1200]
                        source_mode = "github-rest"
                except Exception:
                    pass
            if not commits:
                try:
                    r_com = await client.get(
                        f"https://api.github.com/repos/{repo}/commits",
                        params={"per_page": 8},
                        headers=headers,
                    )
                    if r_com.status_code == 200 and isinstance(r_com.json(), list):
                        for c in r_com.json()[:8]:
                            ci = c.get("commit") or {}
                            msg = (ci.get("message") or "").strip().splitlines()[0][:140]
                            dt = str((ci.get("author") or {}).get("date") or "")[:16].replace("T", " ")
                            author = str(((c.get("author") or {}).get("login")) or (ci.get("author") or {}).get("name") or "")
                            if msg:
                                commits.append({"message": msg, "date": dt, "author": author, "sha": str(c.get("sha") or "")[:7]})
                        source_mode = "github-rest"
                except Exception:
                    pass

    if release_tag:
        new_version = f"{release_tag} ({release_date[:10]})" if release_date else release_tag
    elif commits:
        new_version = f"commit {commits[0]['sha']} ({commits[0]['date'][:10]})"
    else:
        new_version = "Latest revision (HEAD)"

    since_clean = (installed_since or "").strip()[:16].replace("T", " ")
    commits_since_install = [
        c for c in commits if since_clean and c.get("date") and c["date"] > since_clean
    ]

    return {
        "ok": True,
        "repo": repo,
        "source_mode": source_mode,
        "new_version": new_version,
        "release_tag": release_tag,
        "release_name": release_name,
        "release_date": release_date,
        "release_notes": release_notes,
        "commits": commits[:6],
        "commits_since_install": commits_since_install,
    }
