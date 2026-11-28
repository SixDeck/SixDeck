from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

TAG_HTML_RE = re.compile(r"<[^>]+>")
COMMIT_SHA_RE = re.compile(r"/commit/([0-9a-f]{7,40})", re.IGNORECASE)
RELEASE_TAG_RE = re.compile(r"/releases/tag/([^/?#]+)", re.IGNORECASE)


def _strip_html(raw: str) -> str:
    txt = TAG_HTML_RE.sub(" ", raw or "")
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _parse_entry_date(entry: Any) -> str:
    for key in ("updated", "published"):
        val = str(entry.get(key) or "").strip()
        if val:
            return val[:16].replace("T", " ")
    parsed = entry.get("updated_parsed") or entry.get("published_parsed")
    if parsed:
        try:
            dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return ""


def parse_releases_atom(xml_text: str) -> list[dict[str, str]]:
    """Parses a GitHub /releases.atom XML feed into structured release records (zero REST quota)."""
    feed = feedparser.parse(xml_text or "")
    releases: list[dict[str, str]] = []
    for entry in feed.entries or []:
        link = str(entry.get("link") or "").strip()
        title = str(entry.get("title") or "").strip()
        tag = ""
        m = RELEASE_TAG_RE.search(link)
        if m:
            tag = m.group(1)
        elif title:
            tag = title
        summary_raw = ""
        if entry.get("content") and isinstance(entry["content"], list):
            summary_raw = str(entry["content"][0].get("value") or "")
        elif entry.get("summary"):
            summary_raw = str(entry.get("summary") or "")
        releases.append(
            {
                "tag": tag,
                "name": title or tag,
                "url": link,
                "date": _parse_entry_date(entry),
                "author": str(entry.get("author") or "").strip(),
                "notes": _strip_html(summary_raw)[:1200],
            }
        )
    return releases


def parse_commits_atom(xml_text: str) -> list[dict[str, str]]:
    """Parses a GitHub /commits/<branch>.atom XML feed into structured commit records (zero REST quota)."""
    feed = feedparser.parse(xml_text or "")
    commits: list[dict[str, str]] = []
    for entry in feed.entries or []:
        link = str(entry.get("link") or "").strip()
        entry_id = str(entry.get("id") or "")
        sha = ""
        m = COMMIT_SHA_RE.search(link) or COMMIT_SHA_RE.search(entry_id)
        if m:
            sha = m.group(1)
        elif "/" in entry_id:
            tail = entry_id.rsplit("/", 1)[-1]
            if re.fullmatch(r"[0-9a-f]{7,40}", tail, re.IGNORECASE):
                sha = tail
        msg = _strip_html(str(entry.get("title") or "")).splitlines()[0][:160] if entry.get("title") else ""
        if not msg:
            continue
        commits.append(
            {
                "sha": sha[:7],
                "full_sha": sha,
                "message": msg,
                "date": _parse_entry_date(entry),
                "author": str(entry.get("author") or "").strip(),
                "url": link,
            }
        )
    return commits


async def fetch_github_releases_atom(
    repo_slug: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 10.0,
) -> list[dict[str, str]]:
    """Fetches https://github.com/<org>/<repo>/releases.atom without consuming GitHub REST API quota."""
    slug = (repo_slug or "").strip().strip("/")
    if "/" not in slug:
        return []
    url = f"https://github.com/{slug}/releases.atom"
    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                "User-Agent": "SixDeck-AtomWatcher/1.0",
            },
        )
        if resp.status_code == 200 and resp.text:
            return parse_releases_atom(resp.text)
    except Exception:
        pass
    return []


async def fetch_github_commits_atom(
    repo_slug: str,
    client: httpx.AsyncClient,
    *,
    branches: tuple[str, ...] = ("main", "master"),
    timeout: float = 10.0,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Fetches https://github.com/<org>/<repo>/commits/<branch>.atom without consuming GitHub REST API quota."""
    slug = (repo_slug or "").strip().strip("/")
    if "/" not in slug:
        return []
    headers = {
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "User-Agent": "SixDeck-AtomWatcher/1.0",
    }
    for branch in branches:
        url = f"https://github.com/{slug}/commits/{branch}.atom"
        try:
            resp = await client.get(url, follow_redirects=True, timeout=timeout, headers=headers)
            if resp.status_code == 200 and resp.text:
                parsed = parse_commits_atom(resp.text)
                if parsed:
                    return parsed[:limit]
        except Exception:
            continue
    return []
