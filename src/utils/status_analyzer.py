from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import AppConfig
from src.models import ScrapedItem, SourceType
from src.storage import Storage

logger = logging.getLogger(__name__)

GITHUB_REPO_RE = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?/?$")
WHITESPACE_RE = re.compile(r"\s+")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^\)]+\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")

STATUS_SECTION_RE = re.compile(
    r"(?:^|\n)#{1,4}\s*(?:current\s+status|project\s+status|status|progress|compatibility|current\s+state|statut)[^\n]*\n(.*?)(?=\n#{1,4}\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Motifs précis pour déduire un label simple et fiable depuis le contenu réel de la page
ARCHIVED_RE = re.compile(
    r"\b(archived|discontinued|deprecated|abandoned|no longer maintained|dmca|takedown)\b",
    re.IGNORECASE,
)
NOT_PLAYABLE_RE = re.compile(
    r"\b(not playable|unplayable|doesn'?t boot|does not boot|cannot boot|no playable|not yet playable|stub only)\b",
    re.IGNORECASE,
)
PLAYABLE_60_RE = re.compile(
    r"\b(?:runs?|running|playable|in-?game|gameplay|locked|stable|achieves?|reaches?)\b.{0,40}\b(?:60\s*\+?\s*fps|120\s*fps)\b"
    r"|\b(?:60\s*\+?\s*fps|120\s*fps)\b.{0,40}\b(?:playable|in-?game|gameplay|stable|locked)\b",
    re.IGNORECASE,
)
PLAYABLE_30_RE = re.compile(
    r"\b(?:runs?|running|playable|in-?game|gameplay|locked|stable|achieves?|reaches?)\b.{0,40}\b30\s*fps\b"
    r"|\b30\s*fps\b.{0,40}\b(?:playable|in-?game|gameplay|stable|locked)\b",
    re.IGNORECASE,
)
PLAYABLE_RE = re.compile(
    r"\b(fully playable|playable build|playable in-?game|boots to gameplay|status\s*[:\-]\s*playable|complete playthrough)\b",
    re.IGNORECASE,
)
LOAD_WORLD_RE = re.compile(
    r"\b(loads?\s+the\s+world|world\s+loads?|renders?\s+(?:the\s+)?world|map\s+loads?|in-?world|leonida\s+loads?|goes\s+in-?game|boots\s+in-?game)\b",
    re.IGNORECASE,
)
BOOTS_MENU_RE = re.compile(
    r"\b(boots?\s+to\s+(?:menu|title|intro)|title\s+screen|shows?\s+intro|boots?\s+into|menus?\s+working)\b",
    re.IGNORECASE,
)
RPF_TOOL_RE = re.compile(
    r"\b(rpf\d?|openiv|codewalker|archive\s+(?:extractor|viewer|explorer|parser|tool)|asset\s+(?:extractor|viewer|converter)|modding\s+tool)\b",
    re.IGNORECASE,
)
RECOMP_TOOL_RE = re.compile(
    r"\b(static\s+recompil\w*|recompiler|decompiler|binary\s+(?:lifting|translation)|elf2cpp|symbols\.toml|llvm\s+lifting|disassembler)\b",
    re.IGNORECASE,
)
EMULATOR_RE = re.compile(
    r"\b(emulator|shadps4|kyty|fpps4|hle|translation\s+layer|orbis|prospero|ps5\s+emu|ps4\s+emu)\b",
    re.IGNORECASE,
)
WIP_RE = re.compile(
    r"\b(work\s+in\s+progress|wip|early\s+development|experimental|proof\s+of\s+concept|poc|pre-?alpha|early\s+alpha|under\s+development)\b",
    re.IGNORECASE,
)
RESEARCH_RE = re.compile(
    r"\b(paper|arxiv|research|documentation|awesome\s+list|analysis|reverse\s+engineering\s+notes|specification|wiki|guide)\b",
    re.IGNORECASE,
)


def _strip_noise(text: str) -> str:
    txt = CODE_BLOCK_RE.sub(" ", text or "")
    txt = MD_IMG_RE.sub(" ", txt)
    txt = MD_LINK_RE.sub(r"\1", txt)
    txt = HTML_TAG_RE.sub(" ", txt)
    return WHITESPACE_RE.sub(" ", txt).strip()


def infer_simple_status(
    item: ScrapedItem,
    page_text: str = "",
) -> str:
    """Lit la page/README d'un projet et en déduit UN label de progression simple et fiable."""
    # Prioriser les sections Status/Progress explicites si elles existent sur la page
    status_sections = STATUS_SECTION_RE.findall(page_text or "")
    focused_raw = "\n".join(
        [item.title, item.summary] + status_sections + [(page_text or "")[:3500]]
    )
    focused = _strip_noise(focused_raw)
    status_focus = _strip_noise("\n".join([item.title, item.summary] + status_sections))

    if not focused:
        return "Info / Lien"

    # 1. Projet archivé / abandonné
    if ARCHIVED_RE.search(status_focus or focused[:1200]):
        return "Archivé"

    # 2. Vérifier d'abord si le projet dit explicitement qu'il n'est PAS jouable
    explicitly_not_playable = bool(NOT_PLAYABLE_RE.search(focused[:3000]))

    # 3. Progression d'exécution du jeu (uniquement si pas marqué 'not playable')
    if not explicitly_not_playable:
        if PLAYABLE_60_RE.search(focused):
            return "Playable 60 FPS"
        if PLAYABLE_30_RE.search(focused):
            return "Playable 30 FPS"
        if PLAYABLE_RE.search(focused):
            return "Playable"
        if LOAD_WORLD_RE.search(focused):
            return "Load the world"
        if BOOTS_MENU_RE.search(focused):
            return "Boots / Menu"

    # 4. Nature réelle et stade des projets techniques / outils / discussions
    if item.source == SourceType.ARXIV or RESEARCH_RE.search(status_focus):
        return "Recherche / Doc"

    if RPF_TOOL_RE.search(status_focus or focused[:1000]):
        return "Outil RPF (WIP)" if WIP_RE.search(focused[:2000]) else "Outil RPF / Assets"

    if RECOMP_TOOL_RE.search(status_focus or focused[:1000]):
        return "Recomp (WIP)" if WIP_RE.search(focused[:2000]) else "Outil Recomp / Decomp"

    if EMULATOR_RE.search(status_focus or focused[:1000]):
        return "Émulateur (WIP)"

    if WIP_RE.search(focused[:2500]) or explicitly_not_playable:
        return "En dev (WIP)"

    if item.source in {SourceType.REDDIT, SourceType.FORUM, SourceType.WEB_DORK, SourceType.RSS}:
        return "Discussion / Veille"

    return "Code / Projet"


async def fetch_item_page_text(
    item: ScrapedItem,
    client: httpx.AsyncClient,
    *,
    github_token: str | None = None,
    timeout: float = 15.0,
    max_chars: int = 16000,
) -> str:
    """Récupère le README GitHub (brut) ou le texte HTML de la page liée à un item."""
    parsed = urlparse(item.url)
    host = (parsed.hostname or "").lower()

    if host == "github.com":
        m = GITHUB_REPO_RE.match(parsed.path or "")
        if m:
            owner, repo = m.group(1), m.group(2)
            headers = {
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gta6-port-tracker/3.3",
            }
            if github_token:
                headers["Authorization"] = f"Bearer {github_token}"

            chunks: list[str] = []
            try:
                readme_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers=headers,
                    timeout=timeout,
                )
                if readme_resp.status_code == 200 and readme_resp.text:
                    chunks.append(readme_resp.text[:max_chars])
            except httpx.HTTPError:
                pass

            try:
                rel_headers = dict(headers)
                rel_headers["Accept"] = "application/vnd.github+json"
                rel_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
                    headers=rel_headers,
                    timeout=timeout,
                )
                if rel_resp.status_code == 200:
                    rel_data = rel_resp.json()
                    body = (rel_data.get("body") or "").strip()
                    if body:
                        chunks.append(body[:4000])
            except (httpx.HTTPError, ValueError):
                pass

            if chunks:
                return "\n".join(chunks)[:max_chars]

    try:
        resp = await client.get(
            item.url,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,text/plain",
            },
        )
        if resp.status_code == 200 and resp.text:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" in ctype:
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                lines = [
                    WHITESPACE_RE.sub(" ", ln).strip()
                    for ln in soup.get_text("\n").splitlines()
                    if ln.strip()
                ]
                return "\n".join(lines)[:max_chars]
            return resp.text[:max_chars]
    except httpx.HTTPError:
        pass

    return ""


async def analyze_and_backfill_statuses(
    storage: Storage,
    config: AppConfig,
    client: httpx.AsyncClient,
    *,
    limit: int = 60,
    concurrency: int = 6,
) -> list[tuple[ScrapedItem, str]]:
    """Lit la page/README de chaque projet sans statut et en déduit un label simple."""
    candidates = storage.get_items_without_status(limit=limit)
    if not candidates:
        return []

    sem = asyncio.Semaphore(concurrency)
    updated: list[tuple[ScrapedItem, str]] = []

    async def _analyze_one(item: ScrapedItem) -> tuple[ScrapedItem, str] | None:
        async with sem:
            page_text = await fetch_item_page_text(
                item,
                client,
                github_token=config.github_token,
                timeout=config.general.request_timeout,
            )
        label = infer_simple_status(item, page_text)
        if label:
            storage.update_playability_status(item.id, label)
            item.playability_status = label
            return (item, label)
        return None

    results = await asyncio.gather(
        *(_analyze_one(it) for it in candidates), return_exceptions=True
    )
    for res in results:
        if isinstance(res, tuple):
            updated.append(res)

    return updated
