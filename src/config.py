from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class GeneralConfig(BaseModel):
    min_relevance_score: int = 40
    strict_gta6_mode: bool = True
    db_path: str = "data/tracker.db"
    reports_dir: str = "reports"
    request_timeout: float = 20.0
    head_timeout: float = 6.0
    max_concurrency: int = 6
    lookback_days: int = 90
    cycle_delay_seconds: int = 60
    jitter_min_seconds: float = 1.0
    jitter_max_seconds: float = 3.5
    top_display_limit: int = 20


class KeywordGroupConfig(BaseModel):
    weight: int = 20
    terms: list[str] = Field(default_factory=list)


class BlacklistConfig(BaseModel):
    terms: list[str] = Field(default_factory=list)
    suspicious_extensions: list[str] = Field(default_factory=list)
    trusted_archive_domains: list[str] = Field(default_factory=list)


class GitHubLegitimacyConfig(BaseModel):
    min_account_age_days: int = 30
    min_account_repos_if_new: int = 2
    require_valid_code: bool = True
    reject_unchanged_forks: bool = True
    max_readme_to_code_ratio: float = 3.0
    valid_languages: list[str] = Field(default_factory=list)
    min_code_bytes: int = 500
    inspect_top_n_repos: int = 40


class GitHubSourceConfig(BaseModel):
    enabled: bool = True
    per_query_limit: int = 30
    max_pages_per_query: int = 3
    queries: list[str] = Field(default_factory=list)
    code_queries: list[str] = Field(default_factory=list)
    watched_repos: list[str] = Field(default_factory=list)
    watched_filter_terms: list[str] = Field(default_factory=list)
    fetch_releases: bool = True
    fetch_commits: bool = True
    fetch_prs: bool = True
    max_commits_per_repo: int = 10


class WebDorkSourceConfig(BaseModel):
    enabled: bool = True
    searxng_url: str = ""
    max_results_per_dork: int = 8
    max_dorks_per_cycle: int = 10
    forum_sites: list[str] = Field(default_factory=list)
    host_sites: list[str] = Field(default_factory=list)
    custom_dorks: list[str] = Field(default_factory=list)


class ForumSourceConfig(BaseModel):
    enabled: bool = True
    max_threads_per_cycle: int = 12
    seed_urls: list[str] = Field(default_factory=list)


class AltGitSourceConfig(BaseModel):
    enabled: bool = True
    gitlab_enabled: bool = True
    codeberg_enabled: bool = True
    huggingface_enabled: bool = True
    per_query_limit: int = 15
    queries: list[str] = Field(default_factory=list)


class RedditSourceConfig(BaseModel):
    enabled: bool = True
    limit_per_subreddit: int = 25
    max_pages_per_query: int = 2
    sort: str = "new"
    subreddits: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class ArxivConfig(BaseModel):
    enabled: bool = True
    max_results: int = 20
    categories: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)


class RSSFeedConfig(BaseModel):
    name: str
    url: str


class RSSSourceConfig(BaseModel):
    enabled: bool = True
    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)
    feeds: list[RSSFeedConfig] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    github: GitHubSourceConfig = Field(default_factory=GitHubSourceConfig)
    web_dork: WebDorkSourceConfig = Field(default_factory=WebDorkSourceConfig)
    forums: ForumSourceConfig = Field(default_factory=ForumSourceConfig)
    alt_git: AltGitSourceConfig = Field(default_factory=AltGitSourceConfig)
    reddit: RedditSourceConfig = Field(default_factory=RedditSourceConfig)
    rss: RSSSourceConfig = Field(default_factory=RSSSourceConfig)


class DiscordNotifierConfig(BaseModel):
    enabled: bool = False
    min_score: int = 55
    max_items_per_run: int = 15


class MarkdownNotifierConfig(BaseModel):
    enabled: bool = True
    min_score: int = 40


class NotificationsConfig(BaseModel):
    discord: DiscordNotifierConfig = Field(default_factory=DiscordNotifierConfig)
    markdown: MarkdownNotifierConfig = Field(default_factory=MarkdownNotifierConfig)
    windows_toast_on_milestone: bool = True
    milestone_only_alerts: bool = True


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    gta6_identifiers: list[str] = Field(default_factory=list)
    gta6_high_value_terms: list[str] = Field(default_factory=list)
    target_extensions: list[str] = Field(default_factory=list)
    keywords: dict[str, KeywordGroupConfig] = Field(default_factory=dict)
    technical_bonus_terms: list[str] = Field(default_factory=list)
    blacklist: BlacklistConfig = Field(default_factory=BlacklistConfig)
    github_legitimacy: GitHubLegitimacyConfig = Field(default_factory=GitHubLegitimacyConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Secrets chargés depuis l'environnement (.env)
    github_token: str | None = None
    discord_webhook_url: str | None = None
    virustotal_api_key: str | None = None
    reddit_user_agent: str = "python:gta6-port-tracker:v3.4.0 (gta6 recomp tech watch)"


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = None) -> AppConfig:
    """Charge la configuration YAML et les variables d'environnement (.env)."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Fichier de configuration introuvable : {path.resolve()}")

    resolved_env = Path(env_path) if env_path else path.resolve().parent / ".env"
    load_dotenv(dotenv_path=resolved_env, override=False)

    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    config = AppConfig.model_validate(raw)
    config.github_token = os.getenv("GITHUB_TOKEN") or None
    config.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or None
    config.virustotal_api_key = os.getenv("VIRUSTOTAL_API_KEY") or None
    env_ua = os.getenv("REDDIT_USER_AGENT")
    if env_ua:
        config.reddit_user_agent = env_ua

    return config
