from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from src.models import Category, DirectLink, ScrapedItem, SourceType

QUARANTINE_PREFIX_RE = re.compile(r"^\d{9,12}_")


class Storage:
    """Couche de persistance SQLite (items + table liée direct_links)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL,
                    playability_status TEXT DEFAULT '—',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    author TEXT NOT NULL,
                    published_at TEXT,
                    updated_at TEXT,
                    score_engagement INTEGER DEFAULT 0,
                    comments_count INTEGER DEFAULT 0,
                    relevance_score INTEGER DEFAULT 0,
                    matched_keywords TEXT DEFAULT '[]',
                    tags TEXT DEFAULT '[]',
                    score_breakdown TEXT DEFAULT '{}',
                    extra TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    notified INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS direct_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    link_type TEXT NOT NULL DEFAULT 'file',
                    http_status INTEGER,
                    content_type TEXT,
                    content_length INTEGER,
                    is_alive INTEGER DEFAULT 0,
                    checked_at TEXT NOT NULL,
                    UNIQUE(item_id, url),
                    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    saved_path TEXT NOT NULL,
                    sha256 TEXT DEFAULT '',
                    size_bytes INTEGER DEFAULT 0,
                    item_updated_at TEXT,
                    downloaded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_compat (
                    runtime_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    fps INTEGER DEFAULT 0,
                    launches INTEGER DEFAULT 0,
                    crashes INTEGER DEFAULT 0,
                    notes TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads (url)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_item ON downloads (item_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_score ON items (relevance_score DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items (first_seen_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_source ON items (source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_direct_links_item ON direct_links (item_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_direct_links_alive ON direct_links (is_alive DESC)"
            )
            # Migration douce pour les bases existantes sans la colonne playability_status
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()
            }
            if "playability_status" not in existing_cols:
                conn.execute(
                    "ALTER TABLE items ADD COLUMN playability_status TEXT DEFAULT '—'"
                )

    def exists(self, item: ScrapedItem) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM items WHERE id = ? OR url = ? LIMIT 1",
                (item.id, item.url),
            ).fetchone()
            return row is not None

    def _upsert_direct_links(
        self, conn: sqlite3.Connection, item_id: str, links: list[DirectLink], now_iso: str
    ) -> None:
        for dl in links:
            conn.execute(
                """
                INSERT INTO direct_links (
                    item_id, url, filename, link_type, http_status,
                    content_type, content_length, is_alive, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, url) DO UPDATE SET
                    filename = excluded.filename,
                    link_type = excluded.link_type,
                    http_status = COALESCE(excluded.http_status, direct_links.http_status),
                    content_type = COALESCE(excluded.content_type, direct_links.content_type),
                    content_length = COALESCE(excluded.content_length, direct_links.content_length),
                    is_alive = MAX(direct_links.is_alive, excluded.is_alive),
                    checked_at = excluded.checked_at
                """,
                (
                    item_id,
                    dl.url,
                    dl.filename,
                    dl.link_type,
                    dl.http_status,
                    dl.content_type,
                    dl.content_length,
                    1 if dl.is_alive else 0,
                    now_iso,
                ),
            )

    def save_new_items(self, items: list[ScrapedItem]) -> list[ScrapedItem]:
        """Insère les nouveaux items et leurs direct_links ; met à jour les existants."""
        now_iso = datetime.now(timezone.utc).isoformat()
        new_items: list[ScrapedItem] = []

        with self._connect() as conn:
            for item in items:
                existing = conn.execute(
                    "SELECT id FROM items WHERE id = ? OR url = ? LIMIT 1",
                    (item.id, item.url),
                ).fetchone()

                if existing:
                    existing_id = existing["id"]
                    status_clean = (item.playability_status or "").strip()
                    conn.execute(
                        """
                        UPDATE items
                        SET last_seen_at = ?,
                            score_engagement = ?,
                            comments_count = ?,
                            relevance_score = MAX(relevance_score, ?),
                            playability_status = CASE
                                WHEN (playability_status IS NULL OR playability_status = '' OR playability_status = '—')
                                     AND ? != '' THEN ?
                                ELSE playability_status
                            END
                        WHERE id = ?
                        """,
                        (
                            now_iso,
                            item.score_engagement,
                            item.comments_count,
                            item.relevance_score,
                            status_clean,
                            status_clean,
                            existing_id,
                        ),
                    )
                    if item.direct_links:
                        self._upsert_direct_links(conn, existing_id, item.direct_links, now_iso)
                    continue

                conn.execute(
                    """
                    INSERT INTO items (
                        id, url, source, category, playability_status, title, summary, author,
                        published_at, updated_at, score_engagement, comments_count,
                        relevance_score, matched_keywords, tags, score_breakdown,
                        extra, first_seen_at, last_seen_at, notified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        item.id,
                        item.url,
                        item.source.value,
                        item.category.value,
                        (item.playability_status or "").strip(),
                        item.title,
                        item.summary,
                        item.author,
                        item.published_at.isoformat() if item.published_at else None,
                        item.updated_at.isoformat() if item.updated_at else None,
                        item.score_engagement,
                        item.comments_count,
                        item.relevance_score,
                        json.dumps(item.matched_keywords, ensure_ascii=False),
                        json.dumps(item.tags, ensure_ascii=False),
                        json.dumps(item.score_breakdown, ensure_ascii=False),
                        json.dumps(item.extra, ensure_ascii=False),
                        now_iso,
                        now_iso,
                    ),
                )
                if item.direct_links:
                    self._upsert_direct_links(conn, item.id, item.direct_links, now_iso)
                new_items.append(item)

        return new_items

    def mark_notified(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE items SET notified = 1 WHERE id IN ({placeholders})",
                item_ids,
            )

    def get_item_by_id(self, item_id: str) -> ScrapedItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ? LIMIT 1", (item_id,)).fetchone()
            if row is None:
                return None
            items = self._hydrate_items(conn, [row])
            return items[0] if items else None

    def get_items_without_status(self, limit: int = 50) -> list[ScrapedItem]:
        """Retourne les items enregistrés dont le statut n'est pas encore renseigné."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE playability_status IS NULL
                   OR TRIM(playability_status) = ''
                   OR playability_status = '—'
                ORDER BY relevance_score DESC, COALESCE(updated_at, published_at, first_seen_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return self._hydrate_items(conn, rows)

    def clear_preset_statuses(self) -> int:
        """Efface les anciens statuts automatiques issus des presets retirés."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE items
                SET playability_status = ''
                WHERE playability_status IN ('60 fps', '30fps', 'playable', 'Load the world', '—')
                """
            )
            return cur.rowcount

    def update_playability_status(
        self,
        item_id: str,
        status: str,
        *,
        relevance_score: int | None = None,
    ) -> None:
        cleaned = (status or "").strip()[:120]
        with self._connect() as conn:
            if relevance_score is not None:
                conn.execute(
                    """
                    UPDATE items
                    SET playability_status = ?,
                        relevance_score = MAX(relevance_score, ?)
                    WHERE id = ?
                    """,
                    (cleaned, relevance_score, item_id),
                )
            else:
                conn.execute(
                    "UPDATE items SET playability_status = ? WHERE id = ?",
                    (cleaned, item_id),
                )

    def get_recent_items(self, limit: int = 30, min_score: int = 0) -> list[ScrapedItem]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE relevance_score >= ?
                ORDER BY first_seen_at DESC, relevance_score DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
            return self._hydrate_items(conn, rows)

    def get_top_items(self, limit: int = 25, min_score: int = 0) -> list[ScrapedItem]:
        """Retourne les meilleurs items triés par score de pertinence décroissant."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE relevance_score >= ?
                ORDER BY relevance_score DESC, COALESCE(updated_at, published_at, first_seen_at) DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
            return self._hydrate_items(conn, rows)

    def get_direct_links(
        self, limit: int = 50, *, alive_only: bool = False
    ) -> list[tuple[DirectLink, str, str, int]]:
        """Retourne (DirectLink, item_title, item_url, item_score) triés par pertinence."""
        where_clause = "WHERE dl.is_alive = 1" if alive_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT dl.*, i.title AS item_title, i.url AS item_url, i.relevance_score AS item_score
                FROM direct_links dl
                JOIN items i ON i.id = dl.item_id
                {where_clause}
                ORDER BY dl.is_alive DESC, i.relevance_score DESC, dl.checked_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        results: list[tuple[DirectLink, str, str, int]] = []
        for row in rows:
            dl = DirectLink(
                url=row["url"],
                filename=row["filename"],
                link_type=row["link_type"],
                http_status=row["http_status"],
                content_type=row["content_type"],
                content_length=row["content_length"],
                is_alive=bool(row["is_alive"]),
            )
            results.append((dl, row["item_title"], row["item_url"], row["item_score"]))
        return results

    def _hydrate_items(
        self, conn: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> list[ScrapedItem]:
        if not rows:
            return []
        item_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in item_ids)
        link_rows = conn.execute(
            f"""
            SELECT * FROM direct_links
            WHERE item_id IN ({placeholders})
            ORDER BY is_alive DESC, id ASC
            """,
            item_ids,
        ).fetchall()

        links_by_item: dict[str, list[DirectLink]] = {}
        for lr in link_rows:
            links_by_item.setdefault(lr["item_id"], []).append(
                DirectLink(
                    url=lr["url"],
                    filename=lr["filename"],
                    link_type=lr["link_type"],
                    http_status=lr["http_status"],
                    content_type=lr["content_type"],
                    content_length=lr["content_length"],
                    is_alive=bool(lr["is_alive"]),
                )
            )

        results: list[ScrapedItem] = []
        for row in rows:
            raw_status = row["playability_status"] if "playability_status" in row.keys() else ""
            status_text = "" if (not raw_status or raw_status == "—") else str(raw_status).strip()
            results.append(
                ScrapedItem(
                    id=row["id"],
                    url=row["url"],
                    source=SourceType(row["source"]),
                    category=Category(row["category"]),
                    playability_status=status_text,
                    title=row["title"],
                    summary=row["summary"],
                    author=row["author"],
                    published_at=row["published_at"],
                    updated_at=row["updated_at"],
                    score_engagement=row["score_engagement"],
                    comments_count=row["comments_count"],
                    relevance_score=row["relevance_score"],
                    matched_keywords=json.loads(row["matched_keywords"] or "[]"),
                    tags=json.loads(row["tags"] or "[]"),
                    score_breakdown=json.loads(row["score_breakdown"] or "{}"),
                    extra=json.loads(row["extra"] or "{}"),
                    direct_links=links_by_item.get(row["id"], []),
                )
            )
        return results

    def reclassify_all_items(self, config: Any | None = None) -> dict[str, int]:
        """Réapplique classify_role_and_deps() et recalibre les scores sur tous les items en base."""
        from src.filters.relevance import RelevanceFilter
        from src.utils.role_classifier import classify_role_and_deps

        rf = RelevanceFilter(config) if config is not None else None
        updated = 0
        xbox_count = 0
        recomp_count = 0
        common_count = 0

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM items").fetchall()
            items = self._hydrate_items(conn, rows)
            for it in items:
                track, role, deps = classify_role_and_deps(it)
                extra = dict(it.extra or {})
                extra["track"] = track
                extra["role"] = role
                extra["dependencies"] = deps

                new_score = it.relevance_score
                if rf is not None and not (extra.get("portwatch_verified") or extra.get("pc_race_verified")):
                    evaluated = rf.evaluate(it.model_copy(deep=True))
                    if not evaluated.rejected:
                        new_score = evaluated.relevance_score
                    else:
                        new_score = min(it.relevance_score, 45)

                new_cat = it.category.value
                if role in {"2. Émulateur / Runtime", "6. Shaders / GPU"}:
                    new_cat = Category.EMULATION.value
                elif role in {"1. Portage / Exe PC", "3. Recompilateur Statique", "4. Symboles / Config"}:
                    new_cat = Category.DECOMP.value
                elif role == "7. Dumper / ELF-SPRX":
                    new_cat = Category.DUMPS.value
                elif role == "8. Décompilateur / IA RE":
                    new_cat = Category.AI_RE.value

                conn.execute(
                    "UPDATE items SET category = ?, relevance_score = ?, extra = ? WHERE id = ?",
                    (new_cat, new_score, json.dumps(extra, ensure_ascii=False), it.id),
                )
                updated += 1
                if track == "Exécution Dump / Ému":
                    xbox_count += 1
                elif track == "PC Natif / Recomp":
                    recomp_count += 1
                else:
                    common_count += 1

        return {
            "updated": updated,
            "xbox_count": xbox_count,
            "recomp_count": recomp_count,
            "common_count": common_count,
        }

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            notified = conn.execute("SELECT COUNT(*) FROM items WHERE notified = 1").fetchone()[0]
            high_score = conn.execute(
                "SELECT COUNT(*) FROM items WHERE relevance_score >= 60"
            ).fetchone()[0]
            total_links = conn.execute("SELECT COUNT(*) FROM direct_links").fetchone()[0]
            alive_links = conn.execute(
                "SELECT COUNT(*) FROM direct_links WHERE is_alive = 1"
            ).fetchone()[0]
        return {
            "total": total,
            "notified": notified,
            "high_score": high_score,
            "total_links": total_links,
            "alive_links": alive_links,
        }

    def record_download(
        self,
        *,
        url: str,
        filename: str,
        saved_path: str,
        sha256: str = "",
        size_bytes: int = 0,
        item_id: str | None = None,
        item_updated_at: str | None = None,
    ) -> None:
        """Enregistre un fichier téléchargé pour suivre les versions et les mises à jour."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            resolved_item_id = item_id
            resolved_updated = item_updated_at
            if not resolved_item_id:
                row = conn.execute(
                    """
                    SELECT i.id, COALESCE(i.updated_at, i.published_at) AS eff_dt
                    FROM items i
                    LEFT JOIN direct_links dl ON dl.item_id = i.id
                    WHERE i.url = ? OR dl.url = ?
                    LIMIT 1
                    """,
                    (url, url),
                ).fetchone()
                if row:
                    resolved_item_id = row["id"]
                    if not resolved_updated:
                        resolved_updated = row["eff_dt"]

            conn.execute(
                """
                INSERT INTO downloads (
                    item_id, url, filename, saved_path, sha256,
                    size_bytes, item_updated_at, downloaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_item_id,
                    url,
                    filename,
                    saved_path,
                    sha256,
                    size_bytes,
                    resolved_updated,
                    now_iso,
                ),
            )

    def get_download_history_map(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Retourne (by_url, by_item_id) contenant le dernier téléchargement connu."""
        by_url: dict[str, dict] = {}
        by_item: dict[str, dict] = {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM downloads
                ORDER BY downloaded_at DESC, id DESC
                """
            ).fetchall()

        for row in rows:
            info = {
                "url": row["url"],
                "item_id": row["item_id"],
                "filename": row["filename"],
                "saved_path": row["saved_path"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "item_updated_at": row["item_updated_at"],
                "downloaded_at": row["downloaded_at"],
            }
            if row["url"] and row["url"] not in by_url:
                by_url[row["url"]] = info
            if row["item_id"] and row["item_id"] not in by_item:
                by_item[row["item_id"]] = info
        return by_url, by_item

    def sync_quarantine_folders(self, sandbox_dirs: list[Path]) -> int:
        """Scanne les dossiers quarantine/vm_incoming existants et synchronise la table downloads."""
        synced = 0
        with self._connect() as conn:
            known_paths = {
                row["saved_path"]
                for row in conn.execute("SELECT saved_path FROM downloads").fetchall()
            }
            all_items = conn.execute(
                "SELECT id, url, title, COALESCE(updated_at, published_at) AS eff_dt FROM items"
            ).fetchall()
            all_links = conn.execute(
                "SELECT item_id, url, filename FROM direct_links"
            ).fetchall()

            for root_dir in sandbox_dirs:
                candidate_folders: list[Path] = [
                    root_dir / "quarantine",
                    root_dir / "vm_incoming",
                ]
                by_role_dir = root_dir / "by_role"
                if by_role_dir.is_dir():
                    for rdir in by_role_dir.iterdir():
                        if rdir.is_dir():
                            candidate_folders.append(rdir / "quarantine")
                            candidate_folders.append(rdir / "vm_incoming")
                for folder in candidate_folders:
                    if not folder.exists():
                        continue
                    for fp in folder.iterdir():
                        if not fp.is_file() or str(fp) in known_paths:
                            continue
                        raw_name = fp.name
                        clean = raw_name[:-11] if raw_name.lower().endswith(".quarantine") else raw_name
                        clean = QUARANTINE_PREFIX_RE.sub("", clean).strip()
                        if not clean:
                            continue
                        clean_low = clean.lower()

                        matched_item_id: str | None = None
                        matched_url: str = ""

                        # 1. Correspondance sur direct_links.filename
                        for lr in all_links:
                            if (lr["filename"] or "").lower() == clean_low:
                                matched_item_id = lr["item_id"]
                                matched_url = lr["url"]
                                break

                        # 2. Correspondance sur fin d'URL ou nom de dépôt dans items
                        if not matched_item_id:
                            for ir in all_items:
                                url_tail = (ir["url"] or "").rstrip("/").split("/")[-1].lower()
                                title_tail = (ir["title"] or "").rstrip("/").split("/")[-1].lower()
                                if url_tail == clean_low or title_tail == clean_low:
                                    matched_item_id = ir["id"]
                                    matched_url = ir["url"]
                                    break

                        if not matched_item_id:
                            continue

                        stat = fp.stat()
                        dl_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                        conn.execute(
                            """
                            INSERT INTO downloads (
                                item_id, url, filename, saved_path, sha256,
                                size_bytes, item_updated_at, downloaded_at
                            ) VALUES (?, ?, ?, ?, '', ?, NULL, ?)
                            """,
                            (
                                matched_item_id,
                                matched_url,
                                raw_name,
                                str(fp),
                                stat.st_size,
                                dl_iso,
                            ),
                        )
                        known_paths.add(str(fp))
                        synced += 1
        return synced

    def record_runtime_compat(
        self,
        runtime_key: str,
        status: str,
        *,
        fps: int = 0,
        crashed: bool = False,
        notes: str = "",
    ) -> None:
        """Records local runtime compatibility test results (launches, crashes, best FPS, badge status)."""
        key = (runtime_key or "").strip().lower()
        if not key:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT launches, crashes, fps FROM runtime_compat WHERE runtime_key = ?",
                (key,),
            ).fetchone()
            launches = (int(row["launches"]) if row else 0) + 1
            crashes = (int(row["crashes"]) if row else 0) + (1 if crashed else 0)
            best_fps = max(int(row["fps"]) if row else 0, int(fps or 0))
            conn.execute(
                """
                INSERT INTO runtime_compat (
                    runtime_key, status, fps, launches, crashes, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runtime_key) DO UPDATE SET
                    status = excluded.status,
                    fps = excluded.fps,
                    launches = excluded.launches,
                    crashes = excluded.crashes,
                    notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE runtime_compat.notes END,
                    updated_at = excluded.updated_at
                """,
                (key, status, best_fps, launches, crashes, notes[:240], now_iso),
            )

    def get_runtime_compat_map(self) -> dict[str, dict]:
        """Returns all locally recorded runtime compatibility results keyed by normalized runtime key."""
        out: dict[str, dict] = {}
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runtime_compat").fetchall()
            for r in rows:
                out[r["runtime_key"]] = {
                    "runtime_key": r["runtime_key"],
                    "status": r["status"],
                    "fps": int(r["fps"] or 0),
                    "launches": int(r["launches"] or 0),
                    "crashes": int(r["crashes"] or 0),
                    "notes": r["notes"] or "",
                    "updated_at": r["updated_at"],
                }
        return out


def classify_compat_badge(
    status_text: str = "",
    *,
    score_pct: float = 0.0,
    fps: float = 0.0,
    local_record: dict | None = None,
) -> dict[str, str]:
    """Maps PortWatch VI status/metrics and local results into standardized English compatibility badges."""
    low = (status_text or "").strip().lower()
    if local_record:
        loc_st = str(local_record.get("status") or "").strip().lower()
        loc_fps = float(local_record.get("fps") or 0)
        if loc_fps > fps:
            fps = loc_fps
        if loc_st in {"parfait", "perfect"}:
            return {"label": "Perfect", "tier": "perfect"}
        if loc_st in {"jouable", "playable"}:
            return {"label": "Playable", "tier": "playable"}
        if loc_st in {"en jeu", "ingame", "in-game"}:
            return {"label": "In-Game", "tier": "ingame"}
        if loc_st in {"menus", "menu", "boots"}:
            return {"label": "Menus", "tier": "menus"}
        if loc_st == "crash" and not low:
            return {"label": "Crash", "tier": "crash"}

    if "crash" in low or "broken" in low or "panicked" in low:
        return {"label": "Crash", "tier": "crash"}
    if ("60 fps" in low and "playable" in low) or (score_pct >= 90.0 and fps >= 55):
        return {"label": "Perfect", "tier": "perfect"}
    if "playable" in low or "jouable" in low or score_pct >= 55.0:
        return {"label": "Playable", "tier": "playable"}
    if "world" in low or "in-game" in low or "en jeu" in low or "render" in low or score_pct >= 32.0:
        return {"label": "In-Game", "tier": "ingame"}
    if "menu" in low or "boot" in low or "loads exe" in low or score_pct >= 12.0:
        return {"label": "Menus", "tier": "menus"}
    if local_record and int(local_record.get("crashes") or 0) > 0 and int(local_record.get("fps") or 0) == 0:
        return {"label": "Crash", "tier": "crash"}
    return {"label": "In Dev", "tier": "dev"}
