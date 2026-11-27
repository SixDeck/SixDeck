from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx

from src.config import AppConfig, RSSFeedConfig
from src.models import ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback

logger = logging.getLogger(__name__)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(text: str) -> str:
    cleaned = HTML_TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_entry_date(entry: Any) -> datetime | None:
    # Accès direct via dict pour éviter le DeprecationWarning de feedparser sur updated_parsed
    for field in ("published_parsed", "updated_parsed"):
        if dict.__contains__(entry, field):
            parsed = dict.__getitem__(entry, field)
            if parsed is not None:
                try:
                    ts = calendar.timegm(parsed)
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    continue
    return None


class RSSScraper(BaseScraper):
    """Scraper pour l'API ArXiv (cs.SE, cs.PL, cs.CR) et les flux RSS/Atom techniques."""

    name = "rss"

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        super().__init__(config, client)

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        rss_cfg = self.config.sources.rss
        if not rss_cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.config.general.lookback_days
        )

        self._emit_status("Lecture ArXiv & flux RSS...", on_status)
        tasks = []
        if rss_cfg.arxiv.enabled:
            for query in rss_cfg.arxiv.queries:
                tasks.append(
                    self._fetch_arxiv(
                        query,
                        rss_cfg.arxiv.categories,
                        rss_cfg.arxiv.max_results,
                        cutoff,
                    )
                )

        for feed_cfg in rss_cfg.feeds:
            tasks.append(self._fetch_rss_feed(feed_cfg, cutoff))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        items: list[ScrapedItem] = []
        for batch in results:
            if isinstance(batch, Exception):
                logger.warning("[rss] Erreur flux : %s", batch)
                continue
            fresh = []
            for item in batch:
                if item.url not in seen_urls:
                    seen_urls.add(item.url)
                    items.append(item)
                    fresh.append(item)
            if fresh:
                await self._emit_batch(fresh, on_batch)

        self._emit_status(f"Cycle terminé ({len(items)} bruts)", on_status)
        return items

    async def _fetch_arxiv(
        self,
        query: str,
        categories: list[str],
        max_results: int,
        cutoff: datetime,
    ) -> list[ScrapedItem]:
        cat_clause = " OR ".join(f"cat:{c}" for c in categories)
        search_query = f"({cat_clause}) AND all:({query})" if cat_clause else f"all:({query})"

        resp = await self._request(
            "GET",
            ARXIV_API_URL,
            params={
                "search_query": search_query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={"User-Agent": "gta6-port-tracker/2.0"},
        )
        if resp is None or resp.status_code != 200:
            return []

        parsed = await asyncio.to_thread(feedparser.parse, resp.text)
        items: list[ScrapedItem] = []

        for entry in parsed.entries:
            title = _clean_text(entry.get("title", ""))
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            pub_dt = _parse_entry_date(entry)
            if pub_dt and pub_dt < cutoff:
                continue

            summary = _clean_text(entry.get("summary", ""))
            authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
            author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
            tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.ARXIV, link),
                    source=SourceType.ARXIV,
                    title=f"[ArXiv] {title}",
                    url=link,
                    summary=summary[:700],
                    author=author_str or "ArXiv",
                    published_at=pub_dt,
                    updated_at=pub_dt,
                    tags=tags,
                    extra={"arxiv_query": query},
                )
            )
        return items

    async def _fetch_rss_feed(
        self, feed_cfg: RSSFeedConfig, cutoff: datetime
    ) -> list[ScrapedItem]:
        resp = await self._request(
            "GET",
            feed_cfg.url,
            headers={"User-Agent": "gta6-port-tracker/2.0"},
        )
        if resp is None or resp.status_code != 200:
            return []

        parsed = await asyncio.to_thread(feedparser.parse, resp.text)
        items: list[ScrapedItem] = []

        for entry in parsed.entries[:25]:
            title = _clean_text(entry.get("title", ""))
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            pub_dt = _parse_entry_date(entry)
            if pub_dt and pub_dt < cutoff:
                continue

            summary = _clean_text(entry.get("summary") or entry.get("description") or "")
            author = entry.get("author") or feed_cfg.name
            tags = [feed_cfg.name] + [
                t.get("term", "") for t in entry.get("tags", []) if t.get("term")
            ]

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.RSS, link),
                    source=SourceType.RSS,
                    title=f"[{feed_cfg.name}] {title}",
                    url=link,
                    summary=summary[:600],
                    author=author,
                    published_at=pub_dt,
                    updated_at=pub_dt,
                    tags=tags[:8],
                    extra={"feed_name": feed_cfg.name},
                )
            )
        return items
