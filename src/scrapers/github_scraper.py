from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import AppConfig
from src.models import DirectLink, GitHubMetadata, ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback
from src.utils.link_extractor import extract_candidate_links, verify_direct_links

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubScraper(BaseScraper):
    """Scraper GitHub REST API v3 avec pagination profonde et gestion intelligente des quotas."""

    name = "github"

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        super().__init__(config, client)
        self._owner_cache: dict[str, dict[str, Any] | None] = {}
        self.core_limit: int = 5000 if config.github_token else 60
        self.core_remaining: int | None = None
        self.core_reset: int = 0
        self.search_limit: int = 30 if config.github_token else 10
        self.search_remaining: int | None = None
        self.search_reset: int = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gta6-port-tracker/2.0",
        }
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        return headers

    async def check_rate_limit(self) -> dict[str, dict[str, int]]:
        """Interroge /rate_limit au démarrage (ne consomme pas de quota)."""
        try:
            resp = await self.client.get(
                f"{GITHUB_API_BASE}/rate_limit",
                headers=self._headers(),
                timeout=self.config.general.request_timeout,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            resources = data.get("resources") or {}
            core = resources.get("core") or {}
            search = resources.get("search") or {}

            self.core_limit = int(core.get("limit", self.core_limit))
            self.core_remaining = int(core.get("remaining", 0))
            self.core_reset = int(core.get("reset", 0))

            self.search_limit = int(search.get("limit", self.search_limit))
            self.search_remaining = int(search.get("remaining", 0))
            self.search_reset = int(search.get("reset", 0))

            return {
                "core": {
                    "limit": self.core_limit,
                    "remaining": self.core_remaining,
                    "reset": self.core_reset,
                },
                "search": {
                    "limit": self.search_limit,
                    "remaining": self.search_remaining,
                    "reset": self.search_reset,
                },
            }
        except Exception as exc:
            logger.warning("[github] Impossible de lire /rate_limit : %s", exc)
            return {}

    def quota_summary(self) -> str:
        core_rem = "?" if self.core_remaining is None else str(self.core_remaining)
        search_rem = "?" if self.search_remaining is None else str(self.search_remaining)
        return f"Core {core_rem}/{self.core_limit} | Search {search_rem}/{self.search_limit}"

    def _update_quota_from_headers(self, headers: httpx.Headers, is_search: bool) -> None:
        rem = headers.get("X-RateLimit-Remaining")
        lim = headers.get("X-RateLimit-Limit")
        rst = headers.get("X-RateLimit-Reset")
        if is_search:
            if rem and rem.isdigit():
                self.search_remaining = int(rem)
            if lim and lim.isdigit():
                self.search_limit = int(lim)
            if rst and rst.isdigit():
                self.search_reset = int(rst)
        else:
            if rem and rem.isdigit():
                self.core_remaining = int(rem)
            if lim and lim.isdigit():
                self.core_limit = int(lim)
            if rst and rst.isdigit():
                self.core_reset = int(rst)

    async def _wait_for_reset(
        self,
        bucket: str,
        reset_ts: int,
        on_status: StatusCallback | None,
    ) -> None:
        """Met en pause uniquement le scraper GitHub avec compte à rebours en direct."""
        now = int(time.time())
        wait_seconds = max(5, (reset_ts - now) + 2) if reset_ts > now else 30
        for remaining_sec in range(wait_seconds, 0, -1):
            self._emit_status(
                f"Pause quota {bucket} (reprise dans {remaining_sec}s)", on_status
            )
            await asyncio.sleep(1.0)
        self._emit_status("Reprise après reset quota", on_status)

    async def _gh_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        on_status: StatusCallback | None = None,
    ) -> Any | None:
        is_search = path.startswith("/search/")
        bucket = "Search" if is_search else "Core"
        url = f"{GITHUB_API_BASE}{path}"

        for _attempt in range(3):
            # Pause préventive si le quota du bucket est déjà épuisé
            if is_search and self.search_remaining is not None and self.search_remaining <= 1:
                await self._wait_for_reset(bucket, self.search_reset, on_status)
            elif not is_search and self.core_remaining is not None and self.core_remaining <= 1:
                await self._wait_for_reset(bucket, self.core_reset, on_status)

            try:
                async with self.semaphore:
                    resp = await self.client.get(
                        url,
                        params=params,
                        headers=self._headers(),
                        timeout=self.config.general.request_timeout,
                    )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
                logger.warning("[github] Erreur réseau sur %s : %s", path, exc)
                await asyncio.sleep(2.0)
                continue

            self._update_quota_from_headers(resp.headers, is_search)

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return None

            if resp.status_code in {403, 429}:
                rem = resp.headers.get("X-RateLimit-Remaining")
                rst = resp.headers.get("X-RateLimit-Reset")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    reset_ts = int(time.time()) + int(retry_after)
                    await self._wait_for_reset(bucket, reset_ts, on_status)
                    continue
                if rem == "0" or rst:
                    reset_ts = int(rst) if (rst and rst.isdigit()) else int(time.time()) + 60
                    await self._wait_for_reset(bucket, reset_ts, on_status)
                    continue
                # Secondary rate limit GitHub (abuse detection)
                await self._wait_for_reset(bucket, int(time.time()) + 20, on_status)
                continue

            if resp.status_code in {500, 502, 503, 504}:
                await asyncio.sleep(2.0)
                continue

            logger.debug("[github] HTTP %s sur %s", resp.status_code, path)
            return None

        return None

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        gh_cfg = self.config.sources.github
        if not gh_cfg.enabled:
            self._emit_status("désactivé", on_status)
            return []

        since_dt = datetime.now(timezone.utc) - timedelta(
            days=self.config.general.lookback_days
        )
        since_date = since_dt.strftime("%Y-%m-%d")

        all_items: list[ScrapedItem] = []
        seen_repos: set[str] = set()
        inspected_count = 0
        max_inspect = self.config.github_legitimacy.inspect_top_n_repos

        # 1. Recherche paginée en profondeur pour chaque requête GTA 6
        total_queries = len(gh_cfg.queries)
        for q_idx, query in enumerate(gh_cfg.queries, start=1):
            for page in range(1, gh_cfg.max_pages_per_query + 1):
                self._emit_status(
                    f"Requête {q_idx}/{total_queries} (page {page}/{gh_cfg.max_pages_per_query})",
                    on_status,
                )
                page_items, has_more = await self._search_repositories_page(
                    query,
                    since_date,
                    gh_cfg.per_query_limit,
                    page,
                    on_status=on_status,
                )

                fresh_batch: list[ScrapedItem] = []
                for item in page_items:
                    key = (
                        item.github_meta.repo_full_name.lower()
                        if item.github_meta
                        else item.url.lower()
                    )
                    if key not in seen_repos:
                        seen_repos.add(key)
                        fresh_batch.append(item)

                # Enrichissement anti-spam (langages, fichiers racine, ancienneté compte)
                if fresh_batch and inspected_count < max_inspect:
                    to_enrich = fresh_batch[: max(0, max_inspect - inspected_count)]
                    await asyncio.gather(
                        *(
                            self._enrich_repository(it, on_status=on_status)
                            for it in to_enrich
                        ),
                        return_exceptions=True,
                    )
                    inspected_count += len(to_enrich)

                if fresh_batch:
                    all_items.extend(fresh_batch)
                    await self._emit_batch(fresh_batch, on_batch)

                if not has_more:
                    break

                # Douceur sur l'API Search (30 req/min authentifié, 10 req/min anonyme)
                await asyncio.sleep(0.5 if self.config.github_token else 1.5)

        # 1.5. GitHub Code Search (/search/code) pour repérer les projets discrets par leurs fichiers réels
        if gh_cfg.code_queries and self.config.github_token:
            for c_idx, code_q in enumerate(gh_cfg.code_queries, start=1):
                self._emit_status(
                    f"Code Search {c_idx}/{len(gh_cfg.code_queries)}", on_status
                )
                code_items = await self._search_code(code_q, on_status=on_status)
                if code_items:
                    all_items.extend(code_items)
                    await self._emit_batch(code_items, on_batch)
                await asyncio.sleep(1.2)

        # 2. Auto-découverte depuis les dépôts curatés + Releases, commits & PRs sur les dépôts suivis
        from pathlib import Path
        from src.utils.curated_discovery import discover_curated_repos

        cache_path = Path(self.config.general.db_path).resolve().parent / "discovered_repos.json"
        discovered_repos: list[str] = []
        try:
            disc = await discover_curated_repos(
                self.client,
                already_watched=gh_cfg.watched_repos,
                cache_path=cache_path,
                timeout=self.config.general.request_timeout,
            )
            discovered_repos = list(disc.get("all_discovered") or [])[:12]
        except Exception:
            discovered_repos = []

        all_watched = list(dict.fromkeys([*gh_cfg.watched_repos, *discovered_repos]))
        for repo in all_watched:
            self._emit_status(f"Suivi dépôt {repo}", on_status)
            watched_batch: list[ScrapedItem] = []
            if gh_cfg.fetch_releases:
                watched_batch.extend(
                    await self._fetch_repo_releases(repo, since_dt, on_status=on_status)
                )
            if gh_cfg.fetch_commits:
                watched_batch.extend(
                    await self._fetch_repo_commits(
                        repo, since_dt, gh_cfg.max_commits_per_repo, on_status=on_status
                    )
                )
            if gh_cfg.fetch_prs:
                watched_batch.extend(
                    await self._fetch_repo_prs(repo, since_dt, on_status=on_status)
                )
            if watched_batch:
                all_items.extend(watched_batch)
                await self._emit_batch(watched_batch, on_batch)

        self._emit_status(f"Cycle terminé ({len(all_items)} bruts)", on_status)
        return all_items

    async def _search_repositories_page(
        self,
        query: str,
        since_date: str,
        per_page: int,
        page: int,
        *,
        on_status: StatusCallback | None = None,
    ) -> tuple[list[ScrapedItem], bool]:
        limit = min(per_page, 100)
        data = await self._gh_get(
            "/search/repositories",
            params={
                "q": f"{query} pushed:>={since_date}",
                "sort": "updated",
                "order": "desc",
                "per_page": limit,
                "page": page,
            },
            on_status=on_status,
        )
        if not isinstance(data, dict):
            return [], False

        raw_repos = data.get("items") or []
        total_count = int(data.get("total_count") or 0)
        has_more = len(raw_repos) == limit and (page * limit) < min(total_count, 1000)

        items: list[ScrapedItem] = []
        for repo in raw_repos:
            full_name = repo.get("full_name", "")
            html_url = repo.get("html_url", "")
            if not full_name or not html_url:
                continue

            owner = repo.get("owner") or {}
            owner_login = owner.get("login", "unknown")
            topics = repo.get("topics") or []
            primary_lang = repo.get("language")

            meta = GitHubMetadata(
                repo_full_name=full_name,
                stars=repo.get("stargazers_count", 0),
                forks=repo.get("forks_count", 0),
                open_issues=repo.get("open_issues_count", 0),
                primary_language=primary_lang,
                owner_login=owner_login,
                is_fork=bool(repo.get("fork", False)),
            )

            tags = list(topics)
            if primary_lang:
                tags.append(primary_lang)

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITHUB_REPO, full_name.lower()),
                    source=SourceType.GITHUB_REPO,
                    title=full_name,
                    url=html_url,
                    summary=(repo.get("description") or "").strip(),
                    author=owner_login,
                    published_at=repo.get("created_at"),
                    updated_at=repo.get("pushed_at") or repo.get("updated_at"),
                    tags=tags,
                    score_engagement=repo.get("stargazers_count", 0),
                    comments_count=repo.get("open_issues_count", 0),
                    github_meta=meta,
                    extra={"search_query": query, "page": page},
                )
            )

        return items, has_more

    async def _enrich_repository(
        self, item: ScrapedItem, *, on_status: StatusCallback | None = None
    ) -> None:
        if item.github_meta is None:
            return
        meta = item.github_meta
        repo = meta.repo_full_name

        langs_task = self._gh_get(f"/repos/{repo}/languages", on_status=on_status)
        contents_task = self._gh_get(f"/repos/{repo}/contents", on_status=on_status)
        owner_task = self._get_owner_profile(meta.owner_login, on_status=on_status)
        readme_task = self._fetch_readme_excerpt(repo)

        langs, contents, owner_profile, readme_text = await asyncio.gather(
            langs_task, contents_task, owner_task, readme_task
        )

        if readme_text:
            item.extra["readme_excerpt"] = readme_text
            meta.readme_bytes = len(readme_text.encode("utf-8", errors="ignore"))

        # Déduplication des forks : vérifier le nombre de commits originaux (ahead_by)
        if meta.is_fork:
            meta.fork_ahead_by = await self._check_fork_ahead_by(repo, on_status=on_status)

        if isinstance(langs, dict):
            meta.languages = {str(k): int(v) for k, v in langs.items() if isinstance(v, int)}
            meta.inspected = True

        if isinstance(contents, list):
            meta.root_files = [
                str(entry.get("name", ""))
                for entry in contents
                if isinstance(entry, dict) and entry.get("name")
            ]
            lower_names = {n.lower() for n in meta.root_files}
            if lower_names and lower_names.issubset(
                {"readme.md", "readme", "readme.txt", "license", "license.md", ".gitignore"}
            ):
                meta.has_readme_only = True

            # Extraire les fichiers clés de recompilation (ex: symbols.toml, CMakeLists.txt, .elf, .rpf)
            target_exts = tuple(e.lower() for e in self.config.target_extensions)
            for entry in contents:
                if not isinstance(entry, dict) or entry.get("type") != "file":
                    continue
                fname = str(entry.get("name", ""))
                dl_url = entry.get("download_url") or entry.get("html_url")
                if not fname or not dl_url:
                    continue
                if fname.lower().endswith(target_exts) or fname.lower() in {
                    "cmakelists.txt",
                    "symbols.toml",
                    "recomp_config.toml",
                }:
                    ltype = "recomp_config" if fname.lower().endswith((".toml", ".cmake")) or fname.lower() == "cmakelists.txt" else "file"
                    item.direct_links.append(
                        DirectLink(
                            url=str(dl_url),
                            filename=fname,
                            link_type=ltype,
                            http_status=200,
                            content_length=entry.get("size") if isinstance(entry.get("size"), int) else None,
                            is_alive=True,
                        )
                    )

        if isinstance(owner_profile, dict):
            created = owner_profile.get("created_at")
            if isinstance(created, str):
                meta.owner_created_at = datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                )
            if isinstance(owner_profile.get("public_repos"), int):
                meta.owner_public_repos = owner_profile["public_repos"]
            if isinstance(owner_profile.get("followers"), int):
                meta.owner_followers = owner_profile["followers"]

    async def _get_owner_profile(
        self, login: str, *, on_status: StatusCallback | None = None
    ) -> dict[str, Any] | None:
        if not login or login == "unknown":
            return None
        if login in self._owner_cache:
            return self._owner_cache[login]
        data = await self._gh_get(f"/users/{login}", on_status=on_status)
        result = data if isinstance(data, dict) else None
        self._owner_cache[login] = result
        return result

    async def _fetch_readme_excerpt(self, repo_full_name: str, max_chars: int = 12000) -> str:
        """Récupère le texte brut du README GitHub pour détecter le statut réel (playable, 30fps, 60 fps...)."""
        headers = self._headers()
        headers["Accept"] = "application/vnd.github.raw+json"
        try:
            async with self.semaphore:
                resp = await self.client.get(
                    f"{GITHUB_API_BASE}/repos/{repo_full_name}/readme",
                    headers=headers,
                    timeout=self.config.general.request_timeout,
                )
            self._update_quota_from_headers(resp.headers, is_search=False)
            if resp.status_code == 200 and resp.text:
                return resp.text[:max_chars]
        except httpx.HTTPError:
            pass
        return ""

    async def _fetch_repo_releases(
        self,
        repo_full_name: str,
        since_dt: datetime,
        *,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        data = await self._gh_get(
            f"/repos/{repo_full_name}/releases",
            params={"per_page": 5},
            on_status=on_status,
        )
        if not isinstance(data, list):
            return []

        items: list[ScrapedItem] = []
        for rel in data:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            published_raw = rel.get("published_at") or rel.get("created_at")
            if published_raw:
                pub_dt = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                if pub_dt < since_dt:
                    continue

            tag_name = rel.get("tag_name") or "release"
            name = rel.get("name") or tag_name
            html_url = rel.get("html_url", "")
            if not html_url:
                continue

            body = (rel.get("body") or "").strip()
            author = (rel.get("author") or {}).get("login", repo_full_name.split("/")[0])

            direct_links: list[DirectLink] = []
            for asset in (rel.get("assets") or [])[:6]:
                if not isinstance(asset, dict):
                    continue
                dl_url = asset.get("browser_download_url")
                fname = asset.get("name")
                if dl_url and fname:
                    direct_links.append(
                        DirectLink(
                            url=str(dl_url),
                            filename=str(fname),
                            link_type="release",
                            http_status=200,
                            content_type=asset.get("content_type"),
                            content_length=asset.get("size") if isinstance(asset.get("size"), int) else None,
                            is_alive=True,
                        )
                    )
            if not direct_links and body:
                extracted = extract_candidate_links(
                    body,
                    target_extensions=self.config.target_extensions,
                    include_repos=False,
                    max_links=4,
                )
                direct_links = await verify_direct_links(
                    extracted, self.client, timeout=self.config.general.head_timeout, max_to_check=3
                )

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(
                        SourceType.GITHUB_RELEASE, f"{repo_full_name}@{tag_name}"
                    ),
                    source=SourceType.GITHUB_RELEASE,
                    title=f"[Release] {repo_full_name} — {name}",
                    url=html_url,
                    summary=body[:600],
                    author=author,
                    published_at=published_raw,
                    updated_at=published_raw,
                    tags=["release", tag_name, repo_full_name.split("/")[-1]],
                    score_engagement=50,
                    direct_links=direct_links,
                    github_meta=GitHubMetadata(
                        repo_full_name=repo_full_name,
                        primary_language="C++",
                        owner_login=author,
                    ),
                )
            )
        return items

    async def _fetch_repo_commits(
        self,
        repo_full_name: str,
        since_dt: datetime,
        limit: int,
        *,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        data = await self._gh_get(
            f"/repos/{repo_full_name}/commits",
            params={"since": since_dt.isoformat(), "per_page": limit},
            on_status=on_status,
        )
        if not isinstance(data, list):
            return []

        items: list[ScrapedItem] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha", "")
            html_url = entry.get("html_url", "")
            commit = entry.get("commit") or {}
            message = (commit.get("message") or "").strip()
            if not sha or not html_url or not message:
                continue

            first_line = message.splitlines()[0][:140]
            author_info = commit.get("author") or {}
            author_login = (entry.get("author") or {}).get(
                "login", author_info.get("name", "unknown")
            )
            commit_date = author_info.get("date")

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(
                        SourceType.GITHUB_COMMIT, f"{repo_full_name}@{sha}"
                    ),
                    source=SourceType.GITHUB_COMMIT,
                    title=f"[Commit] {repo_full_name}: {first_line}",
                    url=html_url,
                    summary=message[:500],
                    author=author_login,
                    published_at=commit_date,
                    updated_at=commit_date,
                    tags=["commit", repo_full_name.split("/")[-1]],
                    score_engagement=15,
                    github_meta=GitHubMetadata(
                        repo_full_name=repo_full_name,
                        primary_language="C++",
                        owner_login=author_login,
                    ),
                )
            )
        return items

    def _matches_watched_filter(self, text: str) -> bool:
        terms = self.config.sources.github.watched_filter_terms
        if not terms:
            return True
        low = text.lower()
        return any(t.lower() in low for t in terms)

    async def _check_fork_ahead_by(
        self, repo_full_name: str, *, on_status: StatusCallback | None = None
    ) -> int | None:
        info = await self._gh_get(f"/repos/{repo_full_name}", on_status=on_status)
        if not isinstance(info, dict):
            return None
        parent = info.get("parent")
        if not isinstance(parent, dict):
            return None
        parent_owner = (parent.get("owner") or {}).get("login")
        parent_branch = parent.get("default_branch") or "main"
        fork_owner = (info.get("owner") or {}).get("login")
        fork_branch = info.get("default_branch") or "main"
        if not parent_owner or not fork_owner:
            return None

        cmp_data = await self._gh_get(
            f"/repos/{repo_full_name}/compare/{parent_owner}:{parent_branch}...{fork_owner}:{fork_branch}",
            on_status=on_status,
        )
        if isinstance(cmp_data, dict) and isinstance(cmp_data.get("ahead_by"), int):
            return cmp_data["ahead_by"]
        return None

    async def _search_code(
        self, code_query: str, *, on_status: StatusCallback | None = None
    ) -> list[ScrapedItem]:
        """GitHub Code Search (/search/code) pour repérer des fichiers réels (symbols.toml, RPF9, elf2cpp...)."""
        data = await self._gh_get(
            "/search/code",
            params={"q": code_query, "per_page": 10},
            on_status=on_status,
        )
        if not isinstance(data, dict):
            return []

        items: list[ScrapedItem] = []
        seen_repos: set[str] = set()
        for entry in data.get("items") or []:
            if not isinstance(entry, dict):
                continue
            repo = entry.get("repository") or {}
            full_name = repo.get("full_name") or ""
            file_url = entry.get("html_url") or ""
            file_name = entry.get("name") or "code_file"
            file_path = entry.get("path") or file_name
            if not full_name or not file_url or full_name.lower() in seen_repos:
                continue
            seen_repos.add(full_name.lower())

            owner_login = (repo.get("owner") or {}).get("login", "unknown")
            desc = (repo.get("description") or "").strip()
            ltype = (
                "recomp_config"
                if file_name.lower().endswith((".toml", ".cmake"))
                else "source"
            )
            dl = DirectLink(
                url=file_url,
                filename=file_name,
                link_type=ltype,
                http_status=200,
                is_alive=True,
            )

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITHUB_CODE, f"{full_name}:{file_path}"),
                    source=SourceType.GITHUB_CODE,
                    title=f"[Code] {full_name} — {file_path}",
                    url=repo.get("html_url") or file_url,
                    summary=f"Fichier trouvé par Code Search ({code_query}) : {file_path}. {desc}",
                    author=owner_login,
                    published_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    tags=["code_search", file_name],
                    score_engagement=25,
                    direct_links=[dl],
                    github_meta=GitHubMetadata(
                        repo_full_name=full_name,
                        primary_language="C++",
                        owner_login=owner_login,
                        is_fork=bool(repo.get("fork", False)),
                    ),
                    extra={"code_query": code_query, "file_path": file_path},
                )
            )
        return items

    async def _fetch_repo_prs(
        self,
        repo_full_name: str,
        since_dt: datetime,
        *,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        """Récupère les PRs récentes sur les gros émulateurs filtrées par mots-clés techniques."""
        data = await self._gh_get(
            f"/repos/{repo_full_name}/pulls",
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 15},
            on_status=on_status,
        )
        if not isinstance(data, list):
            return []

        items: list[ScrapedItem] = []
        for pr in data:
            if not isinstance(pr, dict):
                continue
            updated_raw = pr.get("updated_at") or pr.get("created_at")
            if updated_raw:
                upd_dt = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if upd_dt < since_dt:
                    continue

            title = (pr.get("title") or "").strip()
            body = (pr.get("body") or "").strip()
            if not title or not self._matches_watched_filter(f"{title}\n{body}"):
                continue

            html_url = pr.get("html_url") or ""
            number = pr.get("number")
            state = pr.get("state") or "open"
            author = (pr.get("user") or {}).get("login", "unknown")
            if not html_url:
                continue

            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITHUB_PR, f"{repo_full_name}#{number}"),
                    source=SourceType.GITHUB_PR,
                    title=f"[PR #{number} {state}] {repo_full_name}: {title[:120]}",
                    url=html_url,
                    summary=f"GTA 6 / RAGE / PS5 Emulator PR ({repo_full_name}): {body[:500]}",
                    author=author,
                    published_at=pr.get("created_at"),
                    updated_at=updated_raw,
                    tags=["pr", state, repo_full_name.split("/")[-1]],
                    score_engagement=35,
                    github_meta=GitHubMetadata(
                        repo_full_name=repo_full_name,
                        primary_language="C++",
                        owner_login=author,
                    ),
                )
            )
        return items
