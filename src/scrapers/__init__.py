"""Modules de scraping asynchrone par source."""

from src.scrapers.alt_git_scraper import AltGitScraper
from src.scrapers.base import BaseScraper
from src.scrapers.forum_scraper import ForumScraper
from src.scrapers.github_scraper import GitHubScraper
from src.scrapers.reddit_scraper import RedditScraper
from src.scrapers.rss_scraper import RSSScraper
from src.scrapers.web_dork_scraper import WebDorkScraper

__all__ = [
    "AltGitScraper",
    "BaseScraper",
    "ForumScraper",
    "GitHubScraper",
    "RedditScraper",
    "RSSScraper",
    "WebDorkScraper",
]
