from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from src.config import AppConfig
from src.models import ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback
from src.utils.link_extractor import (
    extract_candidate_links,
    is_forum_url,
    verify_direct_links,
)

logger = logging.getLogger(__name__)


def _ddgs_search_sync(query: str, max_results: int) -> list[dict]:
    """Exécute une recherche DuckDuckGo de manière synchrone (appelée via to_thread)."""
    try:
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]

        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results)
            return list(results) if results else []
    except Exception as exc:
        logger.debug("[web_dork] Erreur DDGS pour '%s' : %s", query, exc)
        return []


class WebDorkScraper(BaseScraper):
    """Scraper Web global par dorks (DuckDuckGo Search ou SearXNG configurable)."""

    name = "web_dork"

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient,
        *,
        forum_url_sink: set[str] | None = None,
    ) -> None:
        super().__init__(config, client)
        self.forum_url_sink = forum_url_sink if forum_url_sink is not None else set()
        self._dork_cursor: int = 0

    def build_dorks(self) -> list[str]:
        """Génère automatiquement les dorks GTA 6 + Recompilation (forums, hôtes, fichiers)."""
        cfg = self.config.sources.web_dork
        dorks: list[str] = list(cfg.custom_dorks)

        for site in cfg.forum_sites:
            dorks.append(
                f'site:{site} ("GTA 6" OR "GTA6" OR "GTA6Recomp" OR "RAGE9") '
                f'(recomp OR decomp OR "pc port" OR emulator OR shadps4 OR rpf)'
            )

        for host in cfg.host_sites:
            dorks.append(
                f'site:{host} ("GTA 6" OR "GTA6" OR "GTA6Recomp" OR "GTAVIRecomp" OR "RAGE9") '
                f'(recomp OR decomp OR rpf OR elf2cpp OR symbols.toml OR emulator)'
            )

        # Dédupliquer tout en conservant l'ordre
        return list(dict.fromkeys(dorks))

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        cfg = self.config.sources.web_dork
        if not cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        all_dorks = self.build_dorks()
        if not all_dorks:
            return []

        # Rotation circulaire sur les dorks à chaque cycle pour couvrir tout le web sans saturer
        batch_size = min(cfg.max_dorks_per_cycle, len(all_dorks))
        selected_dorks = [
            all_dorks[(self._dork_cursor + i) % len(all_dorks)]
            for i in range(batch_size)
        ]
        self._dork_cursor = (self._dork_cursor + batch_size) % len(all_dorks)

        all_items: list[ScrapedItem] = []
        seen_urls: set[str] = set()
        engine_name = "SearXNG" if cfg.searxng_url else "DDG"

        for idx, dork in enumerate(selected_dorks, start=1):
            self._emit_status(
                f"{engine_name} dork {idx}/{len(selected_dorks)}", on_status
            )
            if cfg.searxng_url:
                raw_results = await self._search_searxng(
                    cfg.searxng_url, dork, cfg.max_results_per_dork
                )
            else:
                raw_results = await asyncio.to_thread(
                    _ddgs_search_sync, dork, cfg.max_results_per_dork
                )

            dork_items: list[ScrapedItem] = []
            for entry in raw_results:
                url = (entry.get("href") or entry.get("url") or "").strip()
                title = (entry.get("title") or "").strip()
                snippet = (entry.get("body") or entry.get("content") or "").strip()
                if not url or not title or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Alimenter automatiquement le ForumScraper si c'est un thread de forum
                if is_forum_url(url):
                    self.forum_url_sink.add(url)

                host = urlparse(url).netloc or "web"
                candidate_links = extract_candidate_links(
                    f"{title}\n{snippet}\n{url}",
                    base_url=url,
                    target_extensions=self.config.target_extensions,
                    include_repos=False,
                    max_links=5,
                )
                verified_links = await verify_direct_links(
                    candidate_links,
                    self.client,
                    timeout=self.config.general.head_timeout,
                    max_to_check=4,
                )

                dork_items.append(
                    ScrapedItem(
                        id=ScrapedItem.build_id(SourceType.WEB_DORK, url),
                        source=SourceType.WEB_DORK,
                        title=f"[{host}] {title}",
                        url=url,
                        summary=snippet[:600],
                        author=host,
                        published_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                        tags=["web_dork", host],
                        direct_links=verified_links,
                        extra={"dork": dork},
                    )
                )

            if dork_items:
                all_items.extend(dork_items)
                await self._emit_batch(dork_items, on_batch)

            await self._jitter(scale=1.2)

        self._emit_status(
            f"Cycle terminé ({len(all_items)} bruts, {len(self.forum_url_sink)} threads)",
            on_status,
        )
        return all_items

    async def _search_searxng(
        self, base_url: str, query: str, max_results: int
    ) -> list[dict]:
        endpoint = f"{base_url.rstrip('/')}/search"
        resp = await self._request(
            "GET",
            endpoint,
            params={"q": query, "format": "json", "categories": "general,it"},
            headers={"User-Agent": "gta6-port-tracker/3.0"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            payload = resp.json()
            results = payload.get("results") or []
            return results[:max_results]
        except ValueError:
            return []

