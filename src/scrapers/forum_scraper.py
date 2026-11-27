from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import AppConfig
from src.models import ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback
from src.utils.link_extractor import extract_candidate_links, verify_direct_links

logger = logging.getLogger(__name__)

WHITESPACE_RE = re.compile(r"\s+")


def _extract_forum_content(html: str) -> tuple[str, str, datetime | None]:
    """Extrait (titre, texte_technique, dernière_date) pour XenForo, phpBB, Discourse, etc."""
    soup = BeautifulSoup(html, "html.parser")

    # Titre du thread
    title = ""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    # Date du dernier message (<time datetime="...">)
    latest_dt: datetime | None = None
    for time_tag in soup.find_all("time"):
        dt_attr = time_tag.get("datetime") or time_tag.get("data-time")
        if not dt_attr:
            continue
        try:
            if str(dt_attr).isdigit():
                parsed = datetime.fromtimestamp(int(dt_attr), tz=timezone.utc)
            else:
                parsed = datetime.fromisoformat(str(dt_attr).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            if latest_dt is None or parsed > latest_dt:
                latest_dt = parsed
        except (ValueError, OverflowError):
            continue

    # Corps des messages (XenForo, phpBB, Discourse, générique)
    post_blocks: list[str] = []
    selectors = (
        ".bbWrapper",        # XenForo (GBAtemp, GTAForums, ResHax)
        ".message-body",     # XenForo fallback
        ".cooked",           # Discourse
        ".postbody .content",  # phpBB (ZenHAX)
        "article",
    )
    for selector in selectors:
        nodes = soup.select(selector)
        if nodes:
            for node in nodes[:10]:
                txt = WHITESPACE_RE.sub(" ", node.get_text(" ", strip=True)).strip()
                if txt:
                    post_blocks.append(txt)
            break

    if not post_blocks:
        main = soup.find("main") or soup.body
        if main:
            post_blocks.append(
                WHITESPACE_RE.sub(" ", main.get_text(" ", strip=True)).strip()[:2000]
            )

    combined_text = "\n".join(post_blocks)[:3000]
    return title, combined_text, latest_dt


class ForumScraper(BaseScraper):
    """Parse les threads de forums (XenForo/phpBB/Discourse) et extrait tous les liens directs."""

    name = "forum"

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient,
        *,
        discovered_urls: set[str] | None = None,
    ) -> None:
        super().__init__(config, client)
        self.discovered_urls = discovered_urls if discovered_urls is not None else set()
        self._inspected_urls: set[str] = set()

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        cfg = self.config.sources.forums
        if not cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        candidates = list(cfg.seed_urls) + [
            u for u in self.discovered_urls if u not in self._inspected_urls
        ]
        # Dédupliquer
        queue = list(dict.fromkeys(candidates))[: cfg.max_threads_per_cycle]

        if not queue:
            self._emit_status("En attente de threads découverts", on_status)
            return []

        items: list[ScrapedItem] = []
        for idx, url in enumerate(queue, start=1):
            host = urlparse(url).netloc or "forum"
            self._emit_status(f"Analyse thread {idx}/{len(queue)} ({host})", on_status)
            item = await self.inspect_thread(url)
            self._inspected_urls.add(url)
            if item is not None:
                items.append(item)
                await self._emit_batch([item], on_batch)
            await self._jitter(scale=1.0)

        self._emit_status(f"Cycle terminé ({len(items)} threads analysés)", on_status)
        return items

    async def inspect_thread(self, url: str) -> ScrapedItem | None:
        resp = await self._request(
            "GET",
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        if resp is None or resp.status_code != 200:
            return None

        html = resp.text
        title, text_body, latest_dt = _extract_forum_content(html)
        host = urlparse(url).netloc or "forum"
        if not title:
            title = url

        # Extraction de tous les liens directs présents dans les posts (Git, Releases, Archive.org, .zip/.7z/.toml/.cpp...)
        raw_links = extract_candidate_links(
            text_body,
            html=html,
            base_url=url,
            target_extensions=self.config.target_extensions,
            include_repos=True,
            max_links=10,
        )
        verified_links = await verify_direct_links(
            raw_links,
            self.client,
            timeout=self.config.general.head_timeout,
            max_to_check=6,
        )

        return ScrapedItem(
            id=ScrapedItem.build_id(SourceType.FORUM, url),
            source=SourceType.FORUM,
            title=f"[Forum:{host}] {title[:140]}",
            url=url,
            summary=text_body[:700],
            author=host,
            published_at=latest_dt or datetime.now(timezone.utc),
            updated_at=latest_dt or datetime.now(timezone.utc),
            tags=["forum", host],
            direct_links=verified_links,
            extra={"forum_host": host, "links_found": len(verified_links)},
        )

