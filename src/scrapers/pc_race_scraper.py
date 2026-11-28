from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import AppConfig
from src.models import Category, GitHubMetadata, ScrapedItem, SourceType
from src.scrapers.base import BaseScraper, BatchCallback, StatusCallback

logger = logging.getLogger(__name__)

PC_RACE_DATA_URL = "https://raw.githubusercontent.com/vuyp/gta6-pc-race/main/src/data.json"
PC_RACE_SITE_URL = "https://vuyp.github.io/gta6-pc-race/"
PC_RACE_REPO_URL = "https://github.com/vuyp/gta6-pc-race"

DEFAULT_STAGES: tuple[str, ...] = (
    "Not started",
    "Loads",
    "Starts up",
    "Graphics device",
    "First frames",
    "Recognisable picture",
    "In-game",
    "Playable",
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            try:
                return int(float(m.group(0)))
            except ValueError:
                return default
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return default
    return default


def _extract_fps(project: dict[str, Any]) -> float:
    lanes = project.get("lanes") if isinstance(project.get("lanes"), dict) else {}
    speed = lanes.get("speed") if isinstance(lanes.get("speed"), dict) else {}
    for cand in (speed.get("high"), speed.get("low")):
        val = _safe_float(cand, 0.0)
        if val > 0:
            return round(val, 1)
    return 0.0


def compute_pc_race_score(project: dict[str, Any] | None) -> float:
    """Computes a 0–100 progress score from GTA VI PC Race stage, FPS, picture quality, and stability."""
    if not isinstance(project, dict):
        return 0.0

    stage = max(0, min(7, _safe_int(project.get("stage"), 0)))
    # Base progression mapped from the 8 official stages (0..7 -> 0..68)
    stage_weights = (0.0, 8.0, 16.0, 26.0, 36.0, 48.0, 58.0, 68.0)
    base = stage_weights[stage]

    lanes = project.get("lanes") if isinstance(project.get("lanes"), dict) else {}
    pic = lanes.get("picture") if isinstance(lanes.get("picture"), dict) else {}
    pic_step = max(0.0, min(4.0, _safe_float(pic.get("step"), 0.0)))

    fps = _extract_fps(project)
    fps_bonus = (min(30.0, max(0.0, fps)) / 30.0) * 14.0

    stab = lanes.get("stability") if isinstance(lanes.get("stability"), dict) else {}
    mins = _safe_float(stab.get("minutes"), 0.0)
    stab_bonus = (min(30.0, max(0.0, mins)) / 30.0) * 10.0

    pic_bonus = (pic_step / 4.0) * 8.0
    return round(min(100.0, base + fps_bonus + stab_bonus + pic_bonus), 1)


def format_pc_race_status(project: dict[str, Any] | None, stages: list[str] | None = None) -> str:
    """Formats a readable English status label from a GTA VI PC Race entry."""
    if not isinstance(project, dict):
        return "PC Race Tracked"

    stage_list = stages if (isinstance(stages, list) and len(stages) >= 8) else list(DEFAULT_STAGES)
    stage = max(0, min(len(stage_list) - 1, _safe_int(project.get("stage"), 0)))
    stage_name = str(stage_list[stage])
    fps = _extract_fps(project)
    fps_int = int(round(fps)) if fps > 0 else 0

    if stage >= 7:
        if fps_int >= 55:
            return "Playable 60 FPS"
        if fps_int >= 28:
            return "Playable 30 FPS"
        if fps_int > 0:
            return f"Playable {fps_int} FPS"
        return "Playable"
    if stage == 6:
        return f"In-Game ({fps_int} FPS)" if fps_int > 0 else "In-Game"
    if stage == 5:
        return f"Recognisable Picture ({fps_int} FPS)" if fps_int > 0 else "Recognisable Picture"
    return stage_name


def load_pc_race_snapshot(snapshot_path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "source": "GTA VI PC Race",
        "site_url": PC_RACE_SITE_URL,
        "repo_url": PC_RACE_REPO_URL,
        "checked": "",
        "playable": "",
        "leader": "",
        "headline": "",
        "summary": "",
        "stages": list(DEFAULT_STAGES),
        "leaderboard": [],
        "watching": [],
        "updates": [],
    }
    if not snapshot_path.exists():
        return empty
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**empty, **data}
    except Exception:
        pass
    return empty


def parse_pc_race_payload(data: dict[str, Any]) -> tuple[list[ScrapedItem], dict[str, Any]]:
    """Transforms vuyp/gta6-pc-race src/data.json into ScrapedItems and a UI snapshot."""
    now = datetime.now(timezone.utc)
    checked_raw = str(data.get("checked") or now.isoformat())
    stages = [str(s) for s in (data.get("stages") or DEFAULT_STAGES) if s]
    if len(stages) < 8:
        stages = list(DEFAULT_STAGES)

    items: list[ScrapedItem] = []
    leaderboard: list[dict[str, Any]] = []

    for proj in data.get("projects") or []:
        if not isinstance(proj, dict):
            continue
        name = str(proj.get("name") or "").strip()
        if not name:
            continue
        repo = str(proj.get("repo") or "").strip()
        is_private = bool(proj.get("private") or proj.get("nolink"))
        public_repo = repo if (repo and "/" in repo and not is_private) else ""

        stage_idx = max(0, min(len(stages) - 1, _safe_int(proj.get("stage"), 0)))
        stage_name = stages[stage_idx]
        score_pct = compute_pc_race_score(proj)
        status_lbl = format_pc_race_status(proj, stages)
        fps_val = _extract_fps(proj)

        tested = proj.get("tested") if isinstance(proj.get("tested"), dict) else {}
        gpus = [str(g).upper() for g in (tested.get("gpu") or []) if g] if isinstance(tested.get("gpu"), list) else []
        oses = [str(o) for o in (tested.get("os") or []) if o] if isinstance(tested.get("os"), list) else []

        lanes = proj.get("lanes") if isinstance(proj.get("lanes"), dict) else {}
        stab = lanes.get("stability") if isinstance(lanes.get("stability"), dict) else {}
        stab_mins = _safe_float(stab.get("minutes"), 0.0)
        speed_text = str((lanes.get("speed") or {}).get("text") or "") if isinstance(lanes.get("speed"), dict) else ""

        kind = str(proj.get("kind") or "Compatibility layer").strip()
        status_text = str(proj.get("status") or "").strip()
        changed_raw = str(proj.get("changed") or checked_raw)
        repo_url = f"https://github.com/{public_repo}" if public_repo else PC_RACE_SITE_URL
        pos_label = str(proj.get("pos") or "").strip()

        leaderboard.append(
            {
                "id": (public_repo or name).lower().replace("/", "-"),
                "name": name,
                "repo": public_repo,
                "url": repo_url,
                "stage": stage_idx,
                "stage_name": stage_name,
                "pos": _safe_int(proj.get("pos"), 99),
                "pos_label": pos_label,
                "gpu": gpus,
                "os": oses,
                "approach": kind[:140],
                "score_pct": score_pct,
                "fps": fps_val,
                "stability_minutes": stab_mins,
                "speed_text": speed_text[:120],
                "status": status_lbl,
                "summary": status_text[:260],
                "changed": changed_raw,
                "private": is_private,
                "tracker_source": "GTA VI PC Race",
            }
        )

        if public_repo:
            owner = public_repo.split("/")[0]
            rel_score = max(62, min(100, int(round(56 + score_pct * 0.44))))
            cat = Category.DECOMP if "recomp" in kind.lower() else Category.EMULATION
            desc_parts = [
                f"[GTA VI PC Race · Stage {stage_idx}/7 {stage_name} · {score_pct}%]",
                f"GPU: {'/'.join(gpus) if gpus else 'N/A'}",
            ]
            if fps_val > 0:
                desc_parts.append(f"FPS: {fps_val}")
            if status_text:
                desc_parts.append(status_text[:240])
            items.append(
                ScrapedItem(
                    id=ScrapedItem.build_id(SourceType.GITHUB_REPO, public_repo.lower()),
                    source=SourceType.GITHUB_REPO,
                    category=cat,
                    playability_status=status_lbl,
                    title=public_repo,
                    url=f"https://github.com/{public_repo}",
                    summary=" — ".join(desc_parts),
                    author=owner,
                    published_at=changed_raw,
                    updated_at=changed_raw,
                    relevance_score=rel_score,
                    tags=["gta6-pc-race", stage_name, *gpus],
                    github_meta=GitHubMetadata(
                        repo_full_name=public_repo,
                        primary_language="Rust" if "victory" in public_repo.lower() else "C++",
                        owner_login=owner,
                        inspected=False,
                    ),
                    extra={
                        "pc_race_score": score_pct,
                        "pc_race_stage": stage_idx,
                        "pc_race_stage_name": stage_name,
                        "pc_race_gpu": gpus,
                        "pc_race_verified": True,
                        "portwatch_score": score_pct,
                        "portwatch_gpu": gpus,
                    },
                )
            )

    leaderboard.sort(
        key=lambda x: (x["stage"], x["score_pct"], x["fps"], 1 if x["repo"] else 0),
        reverse=True,
    )

    watching_out = [
        {
            "name": str(w.get("name") or ""),
            "repo": str(w.get("repo") or ""),
            "url": (
                f"https://github.com/{w['repo']}"
                if w.get("repo") and "/" in str(w["repo"]) and not w.get("nolink")
                else PC_RACE_SITE_URL
            ),
            "note": str(w.get("note") or "")[:240],
        }
        for w in (data.get("watching") or [])[:40]
        if isinstance(w, dict) and w.get("name")
    ]

    updates_out = [
        {
            "date": str(u.get("t") or "")[:10],
            "timestamp": str(u.get("t") or ""),
            "category": "PC RACE",
            "title": str(u.get("title") or ""),
            "dek": str((u.get("points") or [""])[0])[:280] if isinstance(u.get("points"), list) and u.get("points") else "",
            "body": [str(p) for p in (u.get("points") or []) if isinstance(p, str)][:6],
            "url": PC_RACE_SITE_URL,
        }
        for u in (data.get("updates") or [])[:12]
        if isinstance(u, dict) and u.get("title")
    ]

    snapshot = {
        "source": "GTA VI PC Race",
        "site_url": PC_RACE_SITE_URL,
        "repo_url": PC_RACE_REPO_URL,
        "checked": checked_raw,
        "playable": str(data.get("playable") or ""),
        "leader": str(data.get("leader") or ""),
        "headline": str(data.get("headline") or ""),
        "summary": str(data.get("summary") or "")[:500],
        "stages": stages,
        "leaderboard": leaderboard,
        "watching": watching_out,
        "updates": updates_out,
    }
    return items, snapshot


class PcRaceScraper(BaseScraper):
    """Second official tracker source: GTA VI PC Race (vuyp/gta6-pc-race src/data.json)."""

    name = "pc_race"

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
        self._emit_status("Syncing GTA VI PC Race...", on_status)
        headers = {"User-Agent": "SixDeck-Tracker/3.4"}
        try:
            resp = await self.client.get(
                PC_RACE_DATA_URL,
                headers=headers,
                timeout=self.config.general.request_timeout,
            )
            if resp.status_code != 200:
                self._emit_status(f"HTTP {resp.status_code}", on_status)
                return []
            payload = resp.json()
        except Exception as exc:
            logger.warning("[pc_race] Failed to read src/data.json: %s", exc)
            self._emit_status("Network error", on_status)
            return []

        items, snapshot = parse_pc_race_payload(payload)
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_path.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("[pc_race] Failed to write snapshot: %s", exc)

        if items:
            await self._emit_batch(items, on_batch)
        self._emit_status(
            f"OK ({len(snapshot['leaderboard'])} projects, {len(snapshot['updates'])} updates)",
            on_status,
        )
        return items
