from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import AppConfig
from src.models import Category, GitHubMetadata, ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback

logger = logging.getLogger(__name__)

PORTWATCH_TRACKER_URL = "https://raw.githubusercontent.com/4lwayz/portwatch-vi/main/data/tracker.json"
PORTWATCH_PINNED_URL = "https://raw.githubusercontent.com/4lwayz/portwatch-vi/main/data/pinned.json"


def compute_portwatch_score(gta: dict[str, Any] | None) -> float:
    """Calcule un score de progression 0-100 fidèle aux métriques PortWatch VI."""
    if not isinstance(gta, dict):
        return 0.0

    def _num(key: str) -> float:
        v = gta.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        return 0.0

    loads = min(1.0, max(0.0, _num("loads")))
    boots = min(1.0, max(0.0, _num("boots")))
    menu = min(1.0, max(0.0, _num("menu")))
    world = min(1.0, max(0.0, _num("world")))
    playable = min(1.0, max(0.0, _num("playable")))
    story = min(1.0, max(0.0, _num("story")))
    render = min(1.0, max(0.0, _num("render")))
    fps = min(30.0, max(0.0, _num("fps"))) / 30.0
    stab = min(1.0, max(0.0, _num("stability")))

    raw = (
        loads * 6.0
        + boots * 8.0
        + menu * 8.0
        + world * 12.0
        + playable * 14.0
        + story * 14.0
        + render * 18.0
        + fps * 12.0
        + stab * 8.0
    )
    return round(min(100.0, raw), 1)


def format_portwatch_status(gta: dict[str, Any] | None) -> str:
    """Infers a readable English status label from a PortWatch VI entry."""
    if not isinstance(gta, dict):
        return "PortWatch Tracked"
    fps = gta.get("fps")
    fps_val = int(round(fps)) if isinstance(fps, (int, float)) and fps > 0 else 0
    render = gta.get("render")
    render_pct = int(round(render * 100)) if isinstance(render, (int, float)) and render > 0 else 0

    if gta.get("playable"):
        if fps_val >= 55:
            return "Playable 60 FPS"
        if fps_val >= 28:
            return "Playable 30 FPS"
        if fps_val > 0 and render_pct > 0:
            return f"Playable {fps_val} FPS (Render {render_pct}%)"
        if fps_val > 0:
            return f"Playable {fps_val} FPS"
        return "Playable"
    if gta.get("world"):
        return "Load the world"
    if gta.get("menu") or gta.get("boots"):
        return "Boots / Menu"
    if gta.get("loads"):
        return "Loads EXE"
    return "In Development"


def load_portwatch_snapshot(snapshot_path: Path) -> dict[str, Any]:
    if not snapshot_path.exists():
        return {
            "updated": "",
            "pinned": None,
            "leaderboard": [],
            "tools": [],
            "news": [],
            "changes": [],
            "ignored_count": 0,
        }
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "updated": "",
        "pinned": None,
        "leaderboard": [],
        "tools": [],
        "news": [],
        "changes": [],
        "ignored_count": 0,
    }


def parse_portwatch_payload(
    tracker_data: dict[str, Any],
    pinned_data: dict[str, Any] | None = None,
) -> tuple[list[ScrapedItem], dict[str, Any]]:
    """Transforme tracker.json + pinned.json de PortWatch VI en ScrapedItems + snapshot UI."""
    now = datetime.now(timezone.utc)
    updated_raw = str(tracker_data.get("updated") or now.isoformat())
    items: list[ScrapedItem] = []

    leaderboard: list[dict[str, Any]] = []
    for eff in tracker_data.get("efforts") or []:
        if not isinstance(eff, dict):
            continue
        name = str(eff.get("name") or eff.get("id") or "").strip()
        if not name:
            continue
        repo = str(eff.get("repo") or "").strip()
        gta = eff.get("gta") if isinstance(eff.get("gta"), dict) else {}
        pw_score = compute_portwatch_score(gta)
        status_lbl = format_portwatch_status(gta)
        gpus = [str(g) for g in (eff.get("gpu") or []) if g] if isinstance(eff.get("gpu"), list) else []
        approach = str(eff.get("approach") or "Compatibility layer")
        summary = str(eff.get("summary") or "").strip()
        highlights = [str(h) for h in (eff.get("highlights") or []) if h][:3]
        blockers = [str(b) for b in (eff.get("blockers") or []) if b][:2]
        fps_val = gta.get("fps") if isinstance(gta.get("fps"), (int, float)) else 0
        render_pct = int(round(gta["render"] * 100)) if isinstance(gta.get("render"), (int, float)) else 0
        repo_url = f"https://github.com/{repo}" if repo and "/" in repo else "https://portwatch-vi.xyz/#top"

        leaderboard.append(
            {
                "id": str(eff.get("id") or name.lower()),
                "name": name,
                "repo": repo,
                "url": repo_url,
                "tier": str(eff.get("tier") or "tracked"),
                "gpu": gpus,
                "approach": approach,
                "score_pct": pw_score,
                "fps": fps_val,
                "render_pct": render_pct,
                "status": status_lbl,
                "summary": summary[:240],
                "highlights": highlights,
                "blockers": blockers,
            }
        )

        # Ajouter dans tracker.db tout effort qui a un dépôt GitHub public
        if repo and "/" in repo:
            owner = repo.split("/")[0]
            rel_score = max(65, min(100, int(round(58 + pw_score * 0.42))))
            cat = (
                Category.DECOMP
                if "recomp" in approach.lower()
                else Category.EMULATION
            )
            desc_parts = [
                f"[PortWatch VI · {pw_score}%]",
                f"GPU: {'/'.join(gpus) if gpus else 'N/A'}",
                f"Approche: {approach}",
            ]
            if summary:
                desc_parts.append(summary[:260])
            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITHUB_REPO, repo.lower()),
                    source=SourceType.GITHUB_REPO,
                    category=cat,
                    playability_status=status_lbl,
                    title=repo,
                    url=f"https://github.com/{repo}",
                    summary=" — ".join(desc_parts),
                    author=owner,
                    published_at=updated_raw,
                    updated_at=updated_raw,
                    relevance_score=rel_score,
                    tags=["portwatch-vi", approach, *gpus],
                    github_meta=GitHubMetadata(
                        repo_full_name=repo,
                        primary_language="Rust" if "victory" in repo.lower() else "C++",
                        owner_login=owner,
                        inspected=False,
                    ),
                    extra={
                        "portwatch_score": pw_score,
                        "portwatch_gpu": gpus,
                        "portwatch_approach": approach,
                        "portwatch_verified": True,
                    },
                )
            )

    leaderboard.sort(key=lambda x: (x["score_pct"], x["fps"], 1 if x["repo"] else 0), reverse=True)

    tools_list: list[dict[str, Any]] = []
    for tool in tracker_data.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        repo = str(tool.get("repo") or "").strip()
        name = str(tool.get("name") or repo).strip()
        kind = str(tool.get("kind") or "Tool").strip()
        desc = str(tool.get("desc") or "").strip()
        if not repo or "/" not in repo:
            continue
        tools_list.append(
            {
                "name": name,
                "repo": repo,
                "url": f"https://github.com/{repo}",
                "kind": kind,
                "desc": desc[:200],
            }
        )
        owner = repo.split("/")[0]
        items.append(
            ScrapedItem(
                id=ScrapedItem.build_id(SourceType.GITHUB_REPO, repo.lower()),
                source=SourceType.GITHUB_REPO,
                category=Category.DECOMP,
                playability_status=f"Outil ({kind})",
                title=repo,
                url=f"https://github.com/{repo}",
                summary=f"[PortWatch VI · {kind}] {name} — {desc} (GTA 6 / RAGE 9 / RPF9)",
                author=owner,
                published_at=updated_raw,
                updated_at=updated_raw,
                relevance_score=80,
                tags=["portwatch-vi", kind, "RPF9", "RAGE9"],
                github_meta=GitHubMetadata(
                    repo_full_name=repo,
                    primary_language="Rust",
                    owner_login=owner,
                    inspected=False,
                ),
                extra={"portwatch_tool_kind": kind, "portwatch_verified": True},
            )
        )

    news_out = [
        {
            "date": str(n.get("date") or ""),
            "category": str(n.get("category") or "News"),
            "title": str(n.get("title") or ""),
            "dek": str(n.get("dek") or "")[:280],
            "body": [str(p) for p in (n.get("body") or []) if isinstance(p, str)][:6],
            "sources": [
                {
                    "outlet": str(s.get("outlet") or ""),
                    "title": str(s.get("title") or ""),
                    "url": str(s.get("url") or ""),
                }
                for s in (n.get("sources") or [])
                if isinstance(s, dict) and s.get("url")
            ][:4],
        }
        for n in (tracker_data.get("news") or [])[:10]
        if isinstance(n, dict) and n.get("title")
    ]
    changes_out = [
        {
            "date": str(ch.get("date") or ""),
            "kind": str(ch.get("kind") or ""),
            "text": str(ch.get("text") or "")[:200],
        }
        for ch in (tracker_data.get("changes") or [])[:12]
        if isinstance(ch, dict) and ch.get("text")
    ]
    ignored_repos = [
        str(ig.get("repo") or "").lower()
        for ig in (tracker_data.get("ignored") or [])
        if isinstance(ig, dict) and ig.get("repo")
    ]

    snapshot = {
        "updated": updated_raw,
        "pinned": pinned_data if (isinstance(pinned_data, dict) and pinned_data.get("show")) else None,
        "leaderboard": leaderboard,
        "tools": tools_list,
        "news": news_out,
        "changes": changes_out,
        "ignored_count": len(ignored_repos),
    }
    return items, snapshot


class PortWatchScraper(BaseScraper):
    """Scraper direct de PortWatch VI (4lwayz/portwatch-vi tracker.json + pinned.json)."""

    name = "portwatch"

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient,
        *,
        snapshot_path: Path,
    ) -> None:
        super().__init__(config, client)
        self.snapshot_path = snapshot_path

    async def fetch(
        self,
        *,
        on_batch: BatchCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[ScrapedItem]:
        self._emit_status("Synchro PortWatch VI...", on_status)
        headers = {"User-Agent": "gta6-port-tracker/3.0"}
        try:
            r_trk = await self.client.get(
                PORTWATCH_TRACKER_URL,
                headers=headers,
                timeout=self.config.general.request_timeout,
            )
            if r_trk.status_code != 200:
                self._emit_status(f"HTTP {r_trk.status_code}", on_status)
                return []
            tracker_data = r_trk.json()
        except Exception as exc:
            logger.warning("[portwatch] Échec lecture tracker.json : %s", exc)
            self._emit_status("Erreur réseau", on_status)
            return []

        pinned_data: dict[str, Any] | None = None
        try:
            r_pin = await self.client.get(PORTWATCH_PINNED_URL, headers=headers, timeout=10.0)
            if r_pin.status_code == 200:
                pinned_data = r_pin.json()
        except Exception:
            pass

        items, snapshot = parse_portwatch_payload(tracker_data, pinned_data)

        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_path.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("[portwatch] Impossible d'écrire le snapshot : %s", exc)

        if items:
            await self._emit_batch(items, on_batch)
        self._emit_status(
            f"OK ({len(snapshot['leaderboard'])} builds, {len(snapshot['tools'])} outils)",
            on_status,
        )
        return items
