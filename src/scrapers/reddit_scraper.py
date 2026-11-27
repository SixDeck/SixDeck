from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from src.config import AppConfig
from src.models import ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback

logger = logging.getLogger(__name__)


class RedditScraper(BaseScraper):
    """Scraper Reddit basé sur les endpoints JSON publics (flux + recherche paginée)."""

    name = "reddit"

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        super().__init__(config, client)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.config.reddit_user_agent,
            "Accept": "application/json",
        }

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        reddit_cfg = self.config.sources.reddit
        if not reddit_cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.config.general.lookback_days
        )

        all_items: list[ScrapedItem] = []
        seen_ids: set[str] = set()

        for subreddit in reddit_cfg.subreddits:
            # 1. Flux récent paginé (after cursor)
            after: str | None = None
            for page in range(1, reddit_cfg.max_pages_per_query + 1):
                self._emit_status(
                    f"r/{subreddit} ({reddit_cfg.sort} p.{page})", on_status
                )
                batch, after = await self._fetch_listing(
                    f"https://www.reddit.com/r/{subreddit}/{reddit_cfg.sort}.json",
                    subreddit,
                    {
                        "limit": min(reddit_cfg.limit_per_subreddit, 100),
                        "raw_json": 1,
                        **({"after": after} if after else {}),
                    },
                    cutoff,
                )
                fresh = [it for it in batch if it.id not in seen_ids]
                seen_ids.update(it.id for it in fresh)
                if fresh:
                    all_items.extend(fresh)
                    await self._emit_batch(fresh, on_batch)
                await asyncio.sleep(0.7)
                if not after:
                    break

            # 2. Recherche ciblée GTA 6 dans le subreddit
            for query in reddit_cfg.search_queries:
                self._emit_status(f"r/{subreddit} (recherche GTA 6)", on_status)
                batch, _ = await self._fetch_listing(
                    f"https://www.reddit.com/r/{subreddit}/search.json",
                    subreddit,
                    {
                        "q": query,
                        "restrict_sr": 1,
                        "sort": "new",
                        "t": "year",
                        "limit": min(reddit_cfg.limit_per_subreddit, 100),
                        "raw_json": 1,
                    },
                    cutoff,
                )
                fresh = [it for it in batch if it.id not in seen_ids]
                seen_ids.update(it.id for it in fresh)
                if fresh:
                    all_items.extend(fresh)
                    await self._emit_batch(fresh, on_batch)
                await asyncio.sleep(0.7)

        self._emit_status(f"Cycle terminé ({len(all_items)} bruts)", on_status)
        return all_items

    async def _fetch_listing(
        self,
        url: str,
        subreddit: str,
        params: dict,
        cutoff: datetime,
    ) -> tuple[list[ScrapedItem], str | None]:
        resp = await self._request(
            "GET",
            url,
            params=params,
            headers=self._headers(),
        )
        if resp is None or resp.status_code != 200:
            return [], None

        try:
            payload = resp.json()
        except ValueError:
            return [], None

        data = payload.get("data") or {}
        children = data.get("children") or []
        after = data.get("after")
        items: list[ScrapedItem] = []

        for child in children:
            post = child.get("data") or {}
            post_id = post.get("id")
            title = (post.get("title") or "").strip()
            permalink = post.get("permalink", "")
            if not post_id or not title or not permalink:
                continue

            created_utc = post.get("created_utc")
            published_dt = (
                datetime.fromtimestamp(created_utc, tz=timezone.utc)
                if isinstance(created_utc, (int, float))
                else None
            )
            if published_dt and published_dt < cutoff:
                continue

            selftext = (post.get("selftext") or "").strip()
            external_url = post.get("url") or ""
            reddit_url = f"https://www.reddit.com{permalink}"

            summary_parts = []
            if selftext:
                summary_parts.append(selftext[:500])
            if external_url and external_url != reddit_url and not external_url.startswith(
                "https://www.reddit.com"
            ):
                summary_parts.append(f"Lien externe: {external_url}")

            tags = [f"r/{subreddit}"]
            flair = post.get("link_flair_text")
            if isinstance(flair, str) and flair.strip():
                tags.append(flair.strip())

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.REDDIT, post_id),
                    source=SourceType.REDDIT,
                    title=f"[r/{subreddit}] {title}",
                    url=reddit_url,
                    summary="\n".join(summary_parts),
                    author=post.get("author") or "unknown",
                    published_at=published_dt,
                    updated_at=published_dt,
                    tags=tags,
                    score_engagement=int(post.get("ups") or post.get("score") or 0),
                    comments_count=int(post.get("num_comments") or 0),
                    extra={"subreddit": subreddit, "external_url": external_url},
                )
            )

        return items, after if isinstance(after, str) else None
