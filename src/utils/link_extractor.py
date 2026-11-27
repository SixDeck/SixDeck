from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.models import DirectLink

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)

DEFAULT_TARGET_EXTENSIONS: tuple[str, ...] = (
    ".tar.gz",
    ".toml",
    ".cpp",
    ".h",
    ".cmake",
    ".elf",
    ".sprx",
    ".rpf",
    ".zip",
    ".7z",
    ".bin",
)

FORUM_DOMAINS: tuple[str, ...] = (
    "gtaforums.com",
    "gbatemp.net",
    "reshax.com",
    "zenhax.com",
    "forums.pcsx2.net",
    "forums.rpcs3.net",
    "xentax.com",
)

RELEASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"github\.com/[^/]+/[^/]+/releases/download/", re.IGNORECASE),
    re.compile(r"github\.com/[^/]+/[^/]+/releases/tag/", re.IGNORECASE),
    re.compile(r"gitlab\.com/.+/-/releases", re.IGNORECASE),
    re.compile(r"codeberg\.org/[^/]+/[^/]+/releases", re.IGNORECASE),
    re.compile(r"archive\.org/(download|details)/", re.IGNORECASE),
    re.compile(r"huggingface\.co/.+/resolve/", re.IGNORECASE),
)

GIT_MIRROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://(github|gitlab|codeberg)\.(com|org)/[^/\s]+/[^/\s]+", re.IGNORECASE),
    re.compile(r"https?://gist\.github\.com/[^/\s]+/[a-f0-9]+", re.IGNORECASE),
    re.compile(r"https?://huggingface\.co/[^/\s]+/[^/\s]+", re.IGNORECASE),
)

IGNORED_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".woff",
    ".woff2",
)


def is_forum_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in FORUM_DOMAINS)


def _clean_url(raw: str) -> str:
    return raw.strip().rstrip(".,;:!?)'\"")


def _classify_link(
    url: str, target_extensions: tuple[str, ...] = DEFAULT_TARGET_EXTENSIONS
) -> tuple[str, str] | None:
    """Retourne (filename, link_type) si l'URL est un lien direct pertinent, sinon None."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    path = unquote(parsed.path or "")
    lower_path = path.lower()

    if any(lower_path.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return None

    basename = path.rstrip("/").split("/")[-1] if path.rstrip("/") else parsed.netloc

    # 1. Fichier correspondant aux extensions cibles de recompilation
    for ext in target_extensions:
        if lower_path.endswith(ext.lower()):
            if ext in {".toml", ".cmake"}:
                link_type = "recomp_config"
            elif ext in {".zip", ".7z", ".tar.gz"}:
                link_type = "archive"
            elif ext in {".cpp", ".h"}:
                link_type = "source"
            elif ext in {".elf", ".sprx", ".rpf", ".bin"}:
                link_type = "binary"
            else:
                link_type = "file"
            return (basename or f"file{ext}", link_type)

    # 2. URL de release ou d'archive directe (GitHub/GitLab/Codeberg/Archive.org/HF)
    for pattern in RELEASE_PATTERNS:
        if pattern.search(url):
            label = basename if basename and basename not in {"releases", "download", "tag"} else parsed.netloc
            return (label[:60], "release")

    # 3. Miroir Git / Gist / HuggingFace mentionné dans un post
    for pattern in GIT_MIRROR_PATTERNS:
        if pattern.match(url):
            parts = [p for p in path.split("/") if p]
            label = "/".join(parts[:2]) if len(parts) >= 2 else parsed.netloc
            return (label[:60], "repo")

    return None


def extract_candidate_links(
    text: str = "",
    *,
    html: str | None = None,
    base_url: str | None = None,
    target_extensions: list[str] | tuple[str, ...] = DEFAULT_TARGET_EXTENSIONS,
    include_repos: bool = True,
    max_links: int = 12,
) -> list[DirectLink]:
    """Extrait tous les liens directs vers fichiers/releases/dépôts depuis du texte ou du HTML."""
    exts = tuple(target_extensions) if target_extensions else DEFAULT_TARGET_EXTENSIONS
    raw_urls: list[str] = []

    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = str(a_tag.get("href", "")).strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                resolved = urljoin(base_url, href) if base_url else href
                raw_urls.append(_clean_url(resolved))
        except Exception as exc:
            logger.debug("[link_extractor] Erreur parsing HTML : %s", exc)

    if text:
        for match in URL_REGEX.findall(text):
            raw_urls.append(_clean_url(match))

    if base_url:
        raw_urls.insert(0, _clean_url(base_url))

    seen: set[str] = set()
    results: list[DirectLink] = []

    # Prioriser fichiers / configs / releases avant les simples liens de dépôts
    priority_order = {
        "recomp_config": 0,
        "release": 1,
        "archive": 2,
        "binary": 3,
        "source": 4,
        "file": 5,
        "repo": 6,
    }

    for url in raw_urls:
        if not url or url in seen:
            continue
        seen.add(url)
        classified = _classify_link(url, exts)
        if classified is None:
            continue
        filename, link_type = classified
        if link_type == "repo" and not include_repos:
            continue
        results.append(DirectLink(url=url, filename=filename, link_type=link_type))

    results.sort(key=lambda dl: priority_order.get(dl.link_type, 9))
    return results[:max_links]


async def verify_direct_links(
    links: list[DirectLink],
    client: httpx.AsyncClient,
    *,
    timeout: float = 6.0,
    max_concurrent: int = 4,
    max_to_check: int = 8,
) -> list[DirectLink]:
    """Vérifie chaque lien direct par une requête HTTP HEAD légère (sans téléchargement)."""
    if not links:
        return []

    sem = asyncio.Semaphore(max_concurrent)

    async def _check_one(link: DirectLink) -> DirectLink:
        async with sem:
            try:
                resp = await client.head(
                    link.url,
                    follow_redirects=True,
                    timeout=timeout,
                    headers={"User-Agent": "gta6-port-tracker/2.1 (HEAD verifier)"},
                )
                link.http_status = resp.status_code
                link.is_alive = 200 <= resp.status_code < 300
                ctype = resp.headers.get("Content-Type")
                if ctype:
                    link.content_type = ctype.split(";")[0].strip()
                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit():
                    link.content_length = int(clen)
            except (httpx.HTTPError, ValueError):
                link.is_alive = False
        return link

    to_check = links[:max_to_check]
    rest = links[max_to_check:]
    checked = await asyncio.gather(*(_check_one(dl) for dl in to_check))
    return list(checked) + rest

