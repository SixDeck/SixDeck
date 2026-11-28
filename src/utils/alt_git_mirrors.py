from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

import feedparser
import httpx

from src.sandbox.vm_downloader import DownloadReport, quarantine_download, resolve_download_url

GITLAB_HOSTS = {"gitgud.io", "www.gitgud.io"}
GITLAB_REPO_RE = re.compile(r"^https?://([^/]*(?:gitlab|gitgud\.io)[^/]*)/([^/]+(?:/[^/]+)+?)(?:\.git)?/?$", re.IGNORECASE)
FORGEJO_GITEA_RE = re.compile(r"^https?://([^/]+)/([^/]+)/([^/?#]+?)(?:\.git)?/?$", re.IGNORECASE)
SOURCEHUT_REPO_RE = re.compile(r"^https?://git\.sr\.ht/(~[^/]+)/([^/?#]+?)(?:\.git)?/?$", re.IGNORECASE)


def detect_forge_kind(url: str, gitea_hosts: list[str] | tuple[str, ...] = ()) -> str:
    """Identifies the git forge type for a repository URL."""
    cleaned = (url or "").strip()
    host = (urlparse(cleaned).netloc or "").lower().split(":")[0]
    if host in {"github.com", "www.github.com"}:
        return "github"
    if host == "codeberg.org":
        return "codeberg"
    if host == "git.sr.ht":
        return "sourcehut"
    if "gitlab" in host or host in GITLAB_HOSTS:
        return "gitlab"
    gitea_set = {
        (urlparse(g if "://" in g else f"https://{g}").netloc or g).lower().split(":")[0]
        for g in gitea_hosts
        if g
    }
    if host in gitea_set:
        return "gitea"
    return "generic"


def resolve_forge_archive_url(
    url: str,
    *,
    branch: str = "HEAD",
    gitea_hosts: list[str] | tuple[str, ...] = (),
) -> str:
    """Resolves a repository root URL on GitHub, Codeberg, GitLab, SourceHut, or Gitea to a downloadable archive URL."""
    cleaned = (url or "").strip()
    kind = detect_forge_kind(cleaned, gitea_hosts=gitea_hosts)

    if kind == "github":
        return resolve_download_url(cleaned)

    if kind == "sourcehut":
        m = SOURCEHUT_REPO_RE.match(cleaned)
        if m:
            owner, repo = m.groups()
            return f"https://git.sr.ht/{owner}/{repo}/archive/{branch}.tar.gz"
        return cleaned

    if kind == "gitlab":
        if "/-/" in cleaned:
            return cleaned
        m = GITLAB_REPO_RE.match(cleaned)
        if m:
            host, path = m.groups()
            repo_name = path.rsplit("/", 1)[-1]
            ref = "main" if branch == "HEAD" else branch
            return f"https://{host}/{path}/-/archive/{ref}/{repo_name}-{ref}.zip"
        return cleaned

    if kind in {"codeberg", "gitea"}:
        if "/releases/download/" in cleaned or "/archive/" in cleaned:
            return cleaned
        m = FORGEJO_GITEA_RE.match(cleaned)
        if m:
            host, owner, repo = m.groups()
            return f"https://{host}/{owner}/{repo}/archive/{branch}.zip"
        return cleaned

    return resolve_download_url(cleaned)


async def fetch_forge_latest_info(
    url: str,
    client: httpx.AsyncClient,
    *,
    gitea_hosts: list[str] | tuple[str, ...] = (),
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetches latest release / commit metadata from Codeberg (Forgejo), GitLab, SourceHut, or self-hosted Gitea."""
    cleaned = (url or "").strip()
    kind = detect_forge_kind(cleaned, gitea_hosts=gitea_hosts)
    headers = {"User-Agent": "SixDeck-AltGit/1.0"}

    if kind in {"codeberg", "gitea"}:
        m = FORGEJO_GITEA_RE.match(cleaned)
        if not m:
            return {"ok": False, "forge": kind}
        host, owner, repo = m.groups()
        rel_tag = ""
        rel_notes = ""
        commits: list[dict[str, str]] = []
        try:
            r_rel = await client.get(f"https://{host}/api/v1/repos/{owner}/{repo}/releases", params={"limit": 1}, headers=headers, timeout=timeout)
            if r_rel.status_code == 200 and isinstance(r_rel.json(), list) and r_rel.json():
                d = r_rel.json()[0]
                rel_tag = str(d.get("tag_name") or d.get("name") or "")
                rel_notes = str(d.get("body") or "")[:1000]
        except Exception:
            pass
        try:
            r_com = await client.get(f"https://{host}/api/v1/repos/{owner}/{repo}/commits", params={"limit": 5}, headers=headers, timeout=timeout)
            if r_com.status_code == 200 and isinstance(r_com.json(), list):
                for c in r_com.json()[:5]:
                    ci = c.get("commit") or {}
                    msg = str(ci.get("message") or "").strip().splitlines()[0][:140]
                    dt = str((ci.get("author") or {}).get("date") or "")[:16].replace("T", " ")
                    commits.append({"sha": str(c.get("sha") or "")[:7], "message": msg, "date": dt, "author": str((ci.get("author") or {}).get("name") or "")})
        except Exception:
            pass
        return {"ok": True, "forge": kind, "repo": f"{owner}/{repo}", "release_tag": rel_tag, "release_notes": rel_notes, "commits": commits}

    if kind == "gitlab":
        m = GITLAB_REPO_RE.match(cleaned)
        if not m:
            return {"ok": False, "forge": kind}
        host, path = m.groups()
        proj_ref = quote(path, safe="")
        rel_tag = ""
        rel_notes = ""
        commits = []
        try:
            r_rel = await client.get(f"https://{host}/api/v4/projects/{proj_ref}/releases", params={"per_page": 1}, headers=headers, timeout=timeout)
            # Fallback for GitLab/GitGud hosts that unescape %2F: resolve numeric project ID via owner search
            if r_rel.status_code != 200 and "/" in path:
                owner, repo_name = path.split("/", 1)
                r_find = await client.get(
                    f"https://{host}/api/v4/users/{quote(owner, safe='')}/projects",
                    params={"search": repo_name},
                    headers=headers,
                    timeout=timeout,
                )
                if r_find.status_code == 200 and isinstance(r_find.json(), list):
                    match = next(
                        (p for p in r_find.json() if str(p.get("path_with_namespace") or "").lower() == path.lower()),
                        None,
                    )
                    if match and match.get("id"):
                        proj_ref = str(match["id"])
                        r_rel = await client.get(
                            f"https://{host}/api/v4/projects/{proj_ref}/releases",
                            params={"per_page": 1},
                            headers=headers,
                            timeout=timeout,
                        )
            if r_rel.status_code == 200 and isinstance(r_rel.json(), list) and r_rel.json():
                d = r_rel.json()[0]
                rel_tag = str(d.get("tag_name") or d.get("name") or "")
                rel_notes = str(d.get("description") or "")[:1000]
        except Exception:
            pass
        try:
            r_com = await client.get(f"https://{host}/api/v4/projects/{proj_ref}/repository/commits", params={"per_page": 5}, headers=headers, timeout=timeout)
            if r_com.status_code == 200 and isinstance(r_com.json(), list):
                for c in r_com.json()[:5]:
                    commits.append(
                        {
                            "sha": str(c.get("short_id") or c.get("id") or "")[:7],
                            "message": str(c.get("title") or "").strip()[:140],
                            "date": str(c.get("committed_date") or "")[:16].replace("T", " "),
                            "author": str(c.get("author_name") or ""),
                        }
                    )
        except Exception:
            pass
        return {"ok": True, "forge": kind, "repo": path, "release_tag": rel_tag, "release_notes": rel_notes, "commits": commits}

    if kind == "sourcehut":
        m = SOURCEHUT_REPO_RE.match(cleaned)
        if not m:
            return {"ok": False, "forge": kind}
        owner, repo = m.groups()
        commits = []
        try:
            r_rss = await client.get(f"https://git.sr.ht/{owner}/{repo}/log/rss.xml", headers=headers, timeout=timeout)
            if r_rss.status_code == 200 and r_rss.text:
                feed = feedparser.parse(r_rss.text)
                for e in (feed.entries or [])[:5]:
                    link = str(e.get("link") or "")
                    sha = link.rsplit("/", 1)[-1][:7] if "/commit/" in link else ""
                    commits.append(
                        {
                            "sha": sha,
                            "message": str(e.get("title") or "").strip()[:140],
                            "date": str(e.get("published") or "")[:16],
                            "author": str(e.get("author") or owner),
                        }
                    )
        except Exception:
            pass
        return {"ok": True, "forge": kind, "repo": f"{owner}/{repo}", "release_tag": "", "release_notes": "", "commits": commits}

    return {"ok": False, "forge": kind}


def get_mirrors_for_url(url: str, mirrors_map: dict[str, list[str]]) -> list[str]:
    """Looks up configured fallback mirror URLs matching a repository slug or URL prefix."""
    cleaned = (url or "").strip().lower()
    if not cleaned or not mirrors_map:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for key, mirror_list in mirrors_map.items():
        k_low = (key or "").strip().lower()
        if not k_low or not isinstance(mirror_list, list):
            continue
        if k_low in cleaned:
            for m_url in mirror_list:
                ms = str(m_url or "").strip()
                if ms and ms.lower() != cleaned and ms not in seen:
                    seen.add(ms)
                    out.append(ms)
    return out


async def download_with_mirror_fallback(
    primary_url: str,
    mirrors: list[str],
    target_dir: Path,
    client: httpx.AsyncClient,
    *,
    gitea_hosts: list[str] | tuple[str, ...] = (),
    virustotal_api_key: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[DownloadReport, str, bool]:
    """Downloads from primary_url and automatically falls back through configured Alt-Git mirrors on failure.

    Returns (DownloadReport, used_url, used_fallback_mirror).
    """
    candidates = [primary_url, *[m for m in mirrors if m and m != primary_url]]
    last_exc: Exception | None = None
    for idx, raw_url in enumerate(candidates):
        resolved = resolve_forge_archive_url(raw_url, gitea_hosts=gitea_hosts)
        try:
            rep = await quarantine_download(
                resolved,
                target_dir,
                client,
                virustotal_api_key=virustotal_api_key,
                on_progress=on_progress,
            )
            return rep, raw_url, idx > 0
        except Exception as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc
