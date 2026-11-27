from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SourceType(StrEnum):
    GITHUB_REPO = "github_repo"
    GITHUB_CODE = "github_code"
    GITHUB_RELEASE = "github_release"
    GITHUB_COMMIT = "github_commit"
    GITHUB_PR = "github_pr"
    GITLAB = "gitlab"
    CODEBERG = "codeberg"
    HUGGINGFACE = "huggingface"
    WEB_DORK = "web_dork"
    FORUM = "forum"
    REDDIT = "reddit"
    ARXIV = "arxiv"
    RSS = "rss"


class Category(StrEnum):
    EMULATION = "emulation"
    DECOMP = "decomp"
    AI_RE = "ai_re"
    DUMPS = "dumps"
    GENERAL = "general"


class DirectLink(BaseModel):
    """Lien direct extrait vers un fichier, une release ou un miroir de recompilation."""

    url: str
    filename: str
    link_type: str = "file"  # release, recomp_config, archive, source, binary, repo, file
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    is_alive: bool = False

    @property
    def human_size(self) -> str:
        if self.content_length is None or self.content_length <= 0:
            return ""
        size = float(self.content_length)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024.0
        return ""

    @property
    def display_label(self) -> str:
        size_str = self.human_size
        if size_str:
            return f"{self.filename} ({size_str})"
        return self.filename


class GitHubMetadata(BaseModel):
    """Métadonnées techniques GitHub utilisées pour la vérification de légitimité."""

    repo_full_name: str
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    primary_language: str | None = None
    languages: dict[str, int] = Field(default_factory=dict)
    root_files: list[str] = Field(default_factory=list)
    owner_login: str = ""
    owner_created_at: datetime | None = None
    owner_public_repos: int | None = None
    owner_followers: int | None = None
    is_fork: bool = False
    fork_ahead_by: int | None = None
    readme_bytes: int = 0
    has_readme_only: bool = False
    inspected: bool = False

    @property
    def total_code_bytes(self) -> int:
        return sum(self.languages.values())


class ScrapedItem(BaseModel):
    """Modèle unifié d'un élément collecté et évalué par le tracker."""

    id: str
    source: SourceType
    title: str
    url: str
    summary: str = ""
    author: str = "unknown"
    published_at: datetime | None = None
    updated_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    score_engagement: int = 0
    comments_count: int = 0

    # Liens directs extraits et vérifiés par HTTP HEAD
    direct_links: list[DirectLink] = Field(default_factory=list)

    # Champs remplis par le moteur de filtrage, la lecture de page ou manuellement
    category: Category = Category.GENERAL
    playability_status: str = ""
    relevance_score: int = Field(default=0, ge=0, le=100)
    matched_keywords: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, int] = Field(default_factory=dict)
    rejected: bool = False
    rejection_reason: str | None = None

    # Métadonnées optionnelles spécifiques aux sources
    github_meta: GitHubMetadata | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("published_at", "updated_at", mode="before")
    @classmethod
    def ensure_utc(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def build_id(source: SourceType, unique_key: str) -> str:
        digest = hashlib.sha256(f"{source.value}:{unique_key}".encode("utf-8")).hexdigest()[:16]
        return f"{source.value}:{digest}"

    @property
    def effective_date(self) -> datetime | None:
        return self.updated_at or self.published_at

    def searchable_text(self) -> str:
        parts = [self.title, self.summary, " ".join(self.tags), self.url]
        if self.github_meta:
            parts.append(self.github_meta.repo_full_name)
            if self.github_meta.primary_language:
                parts.append(self.github_meta.primary_language)
        for dl in self.direct_links:
            parts.append(dl.filename)
            parts.append(dl.url)
        for key in ("readme_excerpt", "page_text"):
            val = self.extra.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
        return "\n".join(p for p in parts if p)
