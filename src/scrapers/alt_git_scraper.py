from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from src.config import AppConfig
from src.models import ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback
from src.utils.link_extractor import extract_candidate_links, verify_direct_links

logger = logging.getLogger(__name__)


class AltGitScraper(BaseScraper):
    """Scraper pour les plateformes alternatives : GitLab, Codeberg et HuggingFace."""

    name = "alt_git"

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        super().__init__(config, client)

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        cfg = self.config.sources.alt_git
        if not cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        all_items: list[ScrapedItem] = []
        seen_urls: set[str] = set()

        for q_idx, query in enumerate(cfg.queries, start=1):
            batch: list[ScrapedItem] = []

            if cfg.gitlab_enabled:
                self._emit_status(f"GitLab ({q_idx}/{len(cfg.queries)}: {query})", on_status)
                batch.extend(await self._search_gitlab(query, cfg.per_query_limit))
                await self._jitter(scale=0.5)

            if cfg.codeberg_enabled:
                self._emit_status(
                    f"Codeberg ({q_idx}/{len(cfg.queries)}: {query})", on_status
                )
                batch.extend(await self._search_codeberg(query, cfg.per_query_limit))
                await self._jitter(scale=0.5)

            if cfg.huggingface_enabled:
                self._emit_status(
                    f"HuggingFace ({q_idx}/{len(cfg.queries)}: {query})", on_status
                )
                batch.extend(await self._search_huggingface(query, cfg.per_query_limit))
                await self._jitter(scale=0.5)

            fresh = []
            for item in batch:
                if item.url not in seen_urls:
                    seen_urls.add(item.url)
                    fresh.append(item)

            if fresh:
                all_items.extend(fresh)
                await self._emit_batch(fresh, on_batch)

        self._emit_status(f"Cycle terminé ({len(all_items)} bruts)", on_status)
        return all_items

    async def _search_gitlab(self, query: str, limit: int) -> list[ScrapedItem]:
        resp = await self._request(
            "GET",
            "https://gitlab.com/api/v4/projects",
            params={
                "search": query,
                "order_by": "last_activity_at",
                "sort": "desc",
                "per_page": min(limit, 30),
                "visibility": "public",
            },
            headers={"User-Agent": "gta6-port-tracker/3.0"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            projects = resp.json()
        except ValueError:
            return []
        if not isinstance(projects, list):
            return []

        items: list[ScrapedItem] = []
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            url = proj.get("web_url") or ""
            full_name = proj.get("path_with_namespace") or proj.get("name") or ""
            if not url or not full_name:
                continue

            desc = (proj.get("description") or "").strip()
            topics = proj.get("topics") or proj.get("tag_list") or []
            links = extract_candidate_links(
                desc,
                target_extensions=self.config.target_extensions,
                include_repos=False,
                max_links=4,
            )
            verified = await verify_direct_links(
                links, self.client, timeout=self.config.general.head_timeout, max_to_check=3
            )

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITLAB, full_name.lower()),
                    source=SourceType.GITLAB,
                    title=f"[GitLab] {full_name}",
                    url=url,
                    summary=desc[:600],
                    author=(proj.get("namespace") or {}).get("name", "gitlab"),
                    published_at=proj.get("created_at"),
                    updated_at=proj.get("last_activity_at"),
                    tags=["gitlab"] + [str(t) for t in topics[:6]],
                    score_engagement=int(proj.get("star_count") or 0),
                    direct_links=verified,
                    extra={"platform": "gitlab", "query": query},
                )
            )
        return items

    async def _search_codeberg(self, query: str, limit: int) -> list[ScrapedItem]:
        resp = await self._request(
            "GET",
            "https://codeberg.org/api/v1/repos/search",
            params={"q": query, "sort": "updated", "order": "desc", "limit": min(limit, 30)},
            headers={"User-Agent": "gta6-port-tracker/3.0"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        repos = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(repos, list):
            return []

        items: list[ScrapedItem] = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            url = repo.get("html_url") or ""
            full_name = repo.get("full_name") or ""
            if not url or not full_name:
                continue

            desc = (repo.get("description") or "").strip()
            lang = repo.get("language")
            tags = ["codeberg"] + ([lang] if lang else [])

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.CODEBERG, full_name.lower()),
                    source=SourceType.CODEBERG,
                    title=f"[Codeberg] {full_name}",
                    url=url,
                    summary=desc[:600],
                    author=(repo.get("owner") or {}).get("login", "codeberg"),
                    published_at=repo.get("created_at"),
                    updated_at=repo.get("updated_at"),
                    tags=tags,
                    score_engagement=int(repo.get("stars_count") or 0),
                    extra={"platform": "codeberg", "query": query},
                )
            )
        return items

    async def _search_huggingface(self, query: str, limit: int) -> list[ScrapedItem]:
        items: list[ScrapedItem] = []
        for kind, endpoint, url_prefix in (
            ("model", "https://huggingface.co/api/models", "https://huggingface.co/"),
            ("dataset", "https://huggingface.co/api/datasets", "https://huggingface.co/datasets/"),
        ):
            resp = await self._request(
                "GET",
                endpoint,
                params={"search": query, "sort": "lastModified", "direction": -1, "limit": min(limit, 20)},
                headers={"User-Agent": "gta6-port-tracker/3.0"},
            )
            if resp is None or resp.status_code != 200:
                continue
            try:
                entries = resp.json()
            except ValueError:
                continue
            if not isinstance(entries, list):
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                hf_id = entry.get("id") or entry.get("modelId") or ""
                if not hf_id:
                    continue
                url = f"{url_prefix}{hf_id}"
                tags = ["huggingface", kind] + [str(t) for t in (entry.get("tags") or [])[:6]]
                last_mod = entry.get("lastModified") or datetime.now(timezone.utc).isoformat()
                items.append(
                    ScrapedItem(
                        id=ScrapedItem.build_id(SourceType.HUGGINGFACE, f"{kind}:{hf_id.lower()}"),
                        source=SourceType.HUGGINGFACE,
                        title=f"[HF {kind}] {hf_id}",
                        url=url,
                        summary=f"HuggingFace {kind}: {hf_id} | tags: {', '.join(tags[2:])}",
                        author=hf_id.split("/")[0] if "/" in hf_id else "huggingface",
                        published_at=last_mod,
                        updated_at=last_mod,
                        tags=tags,
                        score_engagement=int(entry.get("likes") or 0),
                        extra={"platform": "huggingface", "kind": kind, "query": query},
                    )
                )
        return items

