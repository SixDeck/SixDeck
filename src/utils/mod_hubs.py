from __future__ import annotations

import html
import re
from typing import Any

import feedparser
import httpx

TAG_RE = re.compile(r"<[^>]+>")
GAME_RELEVANCE_RE = re.compile(
    r"(?i)\b(gta\s*6|gta\s*vi|gtavi|grand\s*theft\s*auto\s*(?:vi|6)|leonida|rage\s*9|rpf\s*9|scarlettport|sixdeck|openrage|bcpack|ttpf)\b"
)


def is_game_relevant_mod(name: str, desc: str = "", url: str = "") -> bool:
    """Checks that a hub-discovered mod is relevant to GTA VI / RAGE 9 / RPF9."""
    blob = f"{name} {desc} {url}"
    return bool(GAME_RELEVANCE_RE.search(blob))


def _clean_text(raw: str, limit: int = 200) -> str:
    txt = html.unescape(TAG_RE.sub(" ", raw or ""))
    return re.sub(r"\s+", " ", txt).strip()[:limit]


def _slug_id(prefix: str, value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return f"{prefix}-{clean or 'item'}"


async def fetch_nexus_mods(
    game_domain: str,
    api_key: str,
    client: httpx.AsyncClient,
    *,
    feed_kind: str = "latest_updated",
    limit: int = 12,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Optional Nexus Mods v1 API adapter (requires user's NEXUS_API_KEY)."""
    domain = (game_domain or "").strip()
    key = (api_key or "").strip()
    if not domain or not key:
        return []
    kind = feed_kind if feed_kind in {"latest_added", "latest_updated", "trending"} else "latest_updated"
    url = f"https://api.nexusmods.com/v1/games/{domain}/mods/{kind}.json"
    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"apikey": key, "Accept": "application/json", "User-Agent": "SixDeck-Launcher/1.0"},
        )
        if resp.status_code != 200:
            return []
        entries = resp.json()
    except Exception:
        return []
    if not isinstance(entries, list):
        return []

    items: list[dict[str, Any]] = []
    for mod in entries[:limit]:
        if not isinstance(mod, dict) or not mod.get("mod_id"):
            continue
        mod_id = str(mod["mod_id"])
        name = str(mod.get("name") or f"Nexus Mod #{mod_id}").strip()
        items.append(
            {
                "id": _slug_id("nexus", f"{domain}-{mod_id}"),
                "name": name,
                "author": str(mod.get("author") or "Nexus Mods"),
                "version": str(mod.get("version") or "latest"),
                "category": "Modding Hub",
                "source_hub": "Nexus Mods",
                "risk": "assets",
                "desc": _clean_text(str(mod.get("summary") or "")),
                "url": f"https://www.nexusmods.com/{domain}/mods/{mod_id}",
                "image": str(mod.get("picture_url") or ""),
                "accent": "linear-gradient(135deg,#78350f,#1e1b4b)",
            }
        )
    return items


async def fetch_gamebanana_mods(
    search_query: str,
    client: httpx.AsyncClient,
    *,
    game_id: int | None = None,
    limit: int = 12,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Optional GameBanana API v11 adapter (public JSON API, no API key required)."""
    params: dict[str, Any] = {"_nPerpage": min(limit, 25), "_nPage": 1}
    if game_id and int(game_id) > 0:
        endpoint = "https://gamebanana.com/apiv11/Mod/Index"
        params["_aFilters[Generic_Game]"] = int(game_id)
    else:
        q = (search_query or "").strip()
        if not q:
            return []
        endpoint = "https://gamebanana.com/apiv11/Util/Search/Results"
        params["_sSearchString"] = q
        params["_sModelName"] = "Mod"

    try:
        resp = await client.get(
            endpoint,
            params=params,
            follow_redirects=True,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "SixDeck-Launcher/1.0"},
        )
        if resp.status_code != 200:
            return []
        payload = resp.json()
    except Exception:
        return []

    records = payload.get("_aRecords") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []

    items: list[dict[str, Any]] = []
    for rec in records[:limit]:
        if not isinstance(rec, dict) or not rec.get("_idRow"):
            continue
        rid = str(rec["_idRow"])
        name = str(rec.get("_sName") or f"GameBanana Mod #{rid}").strip()
        prof_url = str(rec.get("_sProfileUrl") or f"https://gamebanana.com/mods/{rid}")
        submitter = rec.get("_aSubmitter") if isinstance(rec.get("_aSubmitter"), dict) else {}
        desc_txt = _clean_text(str(rec.get("_sText") or rec.get("_sDescription") or f"GameBanana mod by {submitter.get('_sName') or 'community'}"))
        if not game_id and not is_game_relevant_mod(name, desc_txt, prof_url):
            continue
        preview = rec.get("_aPreviewMedia") if isinstance(rec.get("_aPreviewMedia"), dict) else {}
        imgs = preview.get("_aImages") if isinstance(preview.get("_aImages"), list) else []
        img_url = ""
        if imgs and isinstance(imgs[0], dict):
            base = str(imgs[0].get("_sBaseUrl") or "")
            f530 = str(imgs[0].get("_sFile530") or imgs[0].get("_sFile") or "")
            if base and f530:
                img_url = f"{base}/{f530}"
        items.append(
            {
                "id": _slug_id("gb", rid),
                "name": name,
                "author": str(submitter.get("_sName") or "GameBanana"),
                "version": str(rec.get("_sVersion") or "latest"),
                "category": "Modding Hub",
                "source_hub": "GameBanana",
                "risk": "assets",
                "desc": _clean_text(str(rec.get("_sText") or rec.get("_sDescription") or f"GameBanana mod by {submitter.get('_sName') or 'community'}")),
                "url": prof_url,
                "image": img_url,
                "accent": "linear-gradient(135deg,#854d0e,#17122b)",
            }
        )
    return items


def parse_modding_rss(xml_text: str, *, portal_name: str = "Mod Portal RSS", limit: int = 10) -> list[dict[str, Any]]:
    """Parses a specialized modding portal RSS/Atom feed into standardized Workshop entries."""
    feed = feedparser.parse(xml_text or "")
    items: list[dict[str, Any]] = []
    for entry in (feed.entries or [])[:limit]:
        title = _clean_text(str(entry.get("title") or ""), limit=110)
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue
        img = ""
        media = entry.get("media_thumbnail") or entry.get("media_content") or []
        if isinstance(media, list) and media and isinstance(media[0], dict):
            img = str(media[0].get("url") or "")
        items.append(
            {
                "id": _slug_id("rss", f"{portal_name}-{title[:40]}"),
                "name": title,
                "author": str(entry.get("author") or portal_name),
                "version": "latest",
                "category": "Modding Hub",
                "source_hub": portal_name,
                "risk": "assets",
                "desc": _clean_text(str(entry.get("summary") or entry.get("description") or "")),
                "url": link,
                "image": img,
                "accent": "linear-gradient(135deg,#312e81,#161129)",
            }
        )
    return items


async def fetch_modding_rss_feeds(
    feeds: list[dict[str, str]],
    client: httpx.AsyncClient,
    *,
    limit_per_feed: int = 8,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetches all configured specialized modding portal RSS/Atom feeds."""
    out: list[dict[str, Any]] = []
    for f in feeds or []:
        if not isinstance(f, dict):
            continue
        url = str(f.get("url") or "").strip()
        name = str(f.get("name") or "Mod Portal RSS").strip()
        if not url.startswith(("http://", "https://")):
            continue
        try:
            resp = await client.get(url, follow_redirects=True, timeout=timeout, headers={"User-Agent": "SixDeck-Launcher/1.0"})
            if resp.status_code == 200 and resp.text:
                out.extend(parse_modding_rss(resp.text, portal_name=name, limit=limit_per_feed))
        except Exception:
            continue
    return out
