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
)

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
) -> dict[str, Any]:
    """Récupère la version proposée (tag ou SHA), les notes de release et les commits depuis la version installée."""
    m = GITHUB_REPO_RE.search(url or "")
    if not m:
        return {
            "ok": True,
            "repo": "",
            "new_version": "Dernière archive",
            "release_name": "",
            "release_date": "",
            "release_notes": "Aperçu détaillé disponible uniquement pour les dépôts GitHub.",
            "commits": [],
            "commits_since_install": [],
        }

    repo = f"{m.group(1)}/{m.group(2)}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gta6-port-tracker/3.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    release_tag = ""
    release_name = ""
    release_date = ""
    release_notes = ""
    commits: list[dict[str, str]] = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        try:
            r_rel = await client.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers)
            if r_rel.status_code == 200:
                d = r_rel.json()
                release_tag = str(d.get("tag_name") or "").strip()
                release_name = str(d.get("name") or release_tag or "").strip()
                release_date = str(d.get("published_at") or "")[:16].replace("T", " ")
                release_notes = str(d.get("body") or "").strip()[:1200]
        except Exception:
            pass

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
        except Exception:
            pass

    if release_tag:
        new_version = f"{release_tag} ({release_date[:10]})" if release_date else release_tag
    elif commits:
        new_version = f"commit {commits[0]['sha']} ({commits[0]['date'][:10]})"
    else:
        new_version = "Dernière version (HEAD)"

    since_clean = (installed_since or "").strip()[:16].replace("T", " ")
    commits_since_install = [
        c for c in commits if since_clean and c.get("date") and c["date"] > since_clean
    ]

    return {
        "ok": True,
        "repo": repo,
        "new_version": new_version,
        "release_tag": release_tag,
        "release_name": release_name,
        "release_date": release_date,
        "release_notes": release_notes,
        "commits": commits[:6],
        "commits_since_install": commits_since_install,
    }
