from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx

from src.config import load_config
from src.filters.relevance import RelevanceFilter
from src.models import ScrapedItem
from src.notifiers.alerts import is_milestone_item, send_windows_toast
from src.notifiers.discord_webhook import send_discord_alert
from src.notifiers.markdown_export import update_daily_markdown_report
from src.scrapers.alt_git_scraper import AltGitScraper
from src.scrapers.github_scraper import GitHubScraper
from src.scrapers.portwatch_scraper import PortWatchScraper
from src.scrapers.reddit_scraper import RedditScraper
from src.scrapers.rss_scraper import RSSScraper
from src.storage import Storage
from src.utils.status_analyzer import infer_simple_status

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "data" / "silent_watch.log"


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def run_silent_cycle() -> int:
    """Exécute un cycle silencieux et ne notifie Windows/Discord que sur un vrai jalon."""
    config = load_config(CONFIG_PATH)
    storage = Storage(BASE_DIR / config.general.db_path)
    rf = RelevanceFilter(config)

    all_new: list[ScrapedItem] = []
    total_scraped = 0
    total_rejected = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        scrapers = [
            PortWatchScraper(config, client, snapshot_path=BASE_DIR / "data" / "portwatch_snapshot.json"),
            GitHubScraper(config, client),
            AltGitScraper(config, client),
            RedditScraper(config, client),
            RSSScraper(config, client),
        ]
        results = await asyncio.gather(
            *(s.fetch() for s in scrapers),
            return_exceptions=True,
        )

        for res in results:
            if isinstance(res, Exception) or not isinstance(res, list):
                continue
            total_scraped += len(res)
            accepted, rejected = rf.filter_items(res)
            total_rejected += len(rejected)
            for it in accepted:
                if not it.playability_status:
                    it.playability_status = infer_simple_status(
                        it, str(it.extra.get("readme_excerpt") or "")
                    )
            new_items = storage.save_new_items(accepted)
            all_new.extend(new_items)

        milestones = [it for it in all_new if is_milestone_item(it)]
        if milestones:
            for m_it in milestones[:3]:
                send_windows_toast(
                    f"GTA 6 Jalon : {m_it.playability_status or 'Release / TOML'}",
                    f"{m_it.title} ({m_it.relevance_score}/100)",
                )
            if config.notifications.discord.enabled and config.discord_webhook_url:
                try:
                    await send_discord_alert(
                        config.discord_webhook_url,
                        milestones[:5],
                        client,
                    )
                except Exception:
                    pass
            storage.mark_notified([m.id for m in milestones])

        if all_new:
            md_items = storage.get_top_items(limit=100, min_score=40)
            dl_table = storage.get_direct_links(limit=50)
            update_daily_markdown_report(
                md_items,
                BASE_DIR / config.general.reports_dir,
                direct_links_table=dl_table,
                total_scraped=total_scraped,
                total_rejected=total_rejected,
                total_stored=storage.stats()["total"],
            )

    _log(
        f"Cycle silencieux terminé : {total_scraped} scannés, {len(all_new)} nouveaux, {len(milestones)} jalon(s)."
    )
    return len(milestones)


async def run_loop(interval_minutes: int) -> None:
    _log(f"Démarrage de la veille silencieuse (intervalle : {interval_minutes} min).")
    while True:
        try:
            await run_silent_cycle()
        except Exception as exc:
            _log(f"Erreur cycle silencieux : {exc}")
        await asyncio.sleep(max(10, interval_minutes) * 60)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Veille silencieuse GTA 6 (mode zéro bruit)")
    parser.add_argument("--once", action="store_true", help="Exécute un seul cycle puis quitte")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=60,
        help="Intervalle en minutes entre deux scans silencieux (défaut: 60)",
    )
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_silent_cycle())
    else:
        asyncio.run(run_loop(args.interval_minutes))


if __name__ == "__main__":
    main()
