from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.config import AppConfig
from src.models import ScrapedItem

logger = logging.getLogger(__name__)

BatchCallback = Callable[[str, list[ScrapedItem]], Awaitable[None]]
StatusCallback = Callable[[str, str], None]


class BaseScraper(ABC):
    """Classe abstraite de base pour tous les scrapers asynchrones."""

    name: str = "base"

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client
        self.semaphore = asyncio.Semaphore(config.general.max_concurrency)
        self.status: str = "inactif"

    @abstractmethod
    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        """Collecte les items bruts depuis la source cible."""

    def _emit_status(self, message: str, on_status: StatusCallback | None) -> None:
        self.status = message
        if on_status is not None:
            on_status(self.name, message)

    async def _emit_batch(
        self, items: list[ScrapedItem], on_batch: BatchCallback | None
    ) -> None:
        if items and on_batch is not None:
            await on_batch(self.name, items)

    async def _jitter(self, scale: float = 1.0) -> None:
        """Pause aléatoire (jitter) entre requêtes web pour éviter les blocages anti-bot."""
        lo = max(0.2, self.config.general.jitter_min_seconds * scale)
        hi = max(lo, self.config.general.jitter_max_seconds * scale)
        await asyncio.sleep(random.uniform(lo, hi))

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> httpx.Response | None:
        """Exécute une requête HTTP asynchrone avec contrôle de concurrence et retries."""
        async with self.semaphore:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await self.client.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        timeout=self.config.general.request_timeout,
                    )

                    if response.status_code == 200:
                        return response

                    if response.status_code in {429, 500, 502, 503, 504}:
                        retry_after = response.headers.get("Retry-After")
                        delay = (
                            min(float(retry_after), 15.0)
                            if retry_after and retry_after.isdigit()
                            else min(2.0**attempt, 10.0)
                        )
                        logger.warning(
                            "[%s] HTTP %s sur %s (tentative %d/%d) — pause %.1fs",
                            self.name,
                            response.status_code,
                            url,
                            attempt,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    if response.status_code == 403:
                        remaining = response.headers.get("X-RateLimit-Remaining")
                        if remaining == "0":
                            logger.warning(
                                "[%s] Rate limit atteint sur %s (403).", self.name, url
                            )
                            return response

                    logger.debug(
                        "[%s] HTTP %s sur %s", self.name, response.status_code, url
                    )
                    return None

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
                    if attempt == max_retries:
                        logger.warning(
                            "[%s] Échec définitif sur %s : %s", self.name, url, exc
                        )
                        return None
                    delay = min(1.5**attempt, 8.0)
                    await asyncio.sleep(delay)

        return None
