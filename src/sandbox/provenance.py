from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/([^/]+)/([^/?#]+)",
    re.IGNORECASE,
)

DEFAULT_TRUSTED_GITHUB_OWNERS: tuple[str, ...] = (
    "4lwayz",
    "vuyp",
    "Belobug",
    "DexNom",
    "avatarmanz",
    "Zettrox",
    "uiliannn",
    "fxceless",
    "Marvisak",
    "xcl-dev",
    "DesignWithTommy",
    "realblastex",
    "lynxnathan",
    "breadbbbb",
    "holdmysocks",
    "nullbrik",
    "pagiii",
    "Ahmet-Dedeler",
    "shadps4-emu",
    "InoriRus",
    "dexyfex",
    "hedge-dev",
    "TheoBIET",
    "nineforge0",
    "rpatty-dev",
    "woreksami",
    "Librett0",
    "Albotterin",
    "Two-Play",
    "meetox80",
    "SixDeck",
    "DurtyFree",
    "ahmed605",
    "liam-mcelhaney122",
    "kalijerry",
    "alexdeliu",
    "booomxd",
    "wifi-revenge",
    "5headsmartbtw",
    "sub-north",
    "HansKristian-Work",
    "emoose",
    "kry-vex-gta",
    "EchoStretch",
    "idlesauce",
    "julienlgy",
    "fishingpvalues",
)


@dataclass
class ProvenanceResult:
    url: str
    trusted: bool
    requires_confirmation: bool
    risk_level: str  # "trusted" | "warning" | "high"
    host: str = ""
    owner: str = ""
    repo: str = ""
    is_fork: bool | None = None
    repo_age_days: int | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "trusted": self.trusted,
            "requires_confirmation": self.requires_confirmation,
            "risk_level": self.risk_level,
            "host": self.host,
            "owner": self.owner,
            "repo": self.repo,
            "is_fork": self.is_fork,
            "repo_age_days": self.repo_age_days,
            "reasons": list(self.reasons),
        }


def extract_github_owner_repo(url: str) -> tuple[str, str]:
    m = GITHUB_URL_RE.match((url or "").strip())
    if not m:
        return "", ""
    owner = m.group(1).strip()
    repo = m.group(2).strip().removesuffix(".git")
    return owner, repo


def evaluate_provenance(
    url: str,
    *,
    trusted_owners: list[str] | tuple[str, ...] = DEFAULT_TRUSTED_GITHUB_OWNERS,
    repo_metadata: dict[str, Any] | None = None,
    min_repo_age_days: int = 14,
) -> ProvenanceResult:
    """Evaluates download provenance against a configurable GitHub owner/org whitelist and repo metadata.

    Never auto-executes anything; returns `requires_confirmation=True` with explicit warning reasons
    whenever a download originates from an unverified author/org, unknown fork, non-GitHub host,
    or recently created repository.
    """
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower().split(":")[0]
    whitelist = {o.strip().lower() for o in trusted_owners if o and o.strip()}
    owner, repo = extract_github_owner_repo(cleaned)

    reasons: list[str] = []
    is_fork: bool | None = None
    repo_age_days: int | None = None

    if not owner:
        reasons.append(f"Source outside verified GitHub repositories (host: {host or 'unknown'}).")
        return ProvenanceResult(
            url=cleaned,
            trusted=False,
            requires_confirmation=True,
            risk_level="high",
            host=host,
            reasons=reasons,
        )

    owner_whitelisted = owner.lower() in whitelist
    if not owner_whitelisted:
        reasons.append(f"GitHub author/organization '{owner}' is not in the verified whitelist.")

    if isinstance(repo_metadata, dict):
        if repo_metadata.get("fork") is True:
            is_fork = True
            if not owner_whitelisted:
                reasons.append(f"Repository '{owner}/{repo}' is an unverified fork.")
        elif repo_metadata.get("fork") is False:
            is_fork = False

        created_raw = str(repo_metadata.get("created_at") or "").strip()
        if created_raw:
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - created_dt).days
                repo_age_days = max(0, age)
                if repo_age_days < min_repo_age_days and not owner_whitelisted:
                    reasons.append(
                        f"Repository was created very recently ({repo_age_days} day(s) ago, threshold: {min_repo_age_days} days)."
                    )
            except ValueError:
                pass

    if not reasons:
        return ProvenanceResult(
            url=cleaned,
            trusted=True,
            requires_confirmation=False,
            risk_level="trusted",
            host=host,
            owner=owner,
            repo=repo,
            is_fork=is_fork,
            repo_age_days=repo_age_days,
            reasons=[],
        )

    risk = "high" if (not owner_whitelisted and (is_fork or (repo_age_days is not None and repo_age_days < min_repo_age_days))) else "warning"
    return ProvenanceResult(
        url=cleaned,
        trusted=False,
        requires_confirmation=True,
        risk_level=risk,
        host=host,
        owner=owner,
        repo=repo,
        is_fork=is_fork,
        repo_age_days=repo_age_days,
        reasons=reasons,
    )


async def check_provenance_async(
    url: str,
    client: httpx.AsyncClient | None = None,
    *,
    trusted_owners: list[str] | tuple[str, ...] = DEFAULT_TRUSTED_GITHUB_OWNERS,
    github_token: str | None = None,
    min_repo_age_days: int = 14,
    fetch_github_meta: bool = True,
) -> ProvenanceResult:
    """Asynchronously checks provenance, optionally querying GitHub API for fork status and repo creation date."""
    owner, repo = extract_github_owner_repo(url)
    whitelist = {o.strip().lower() for o in trusted_owners if o and o.strip()}
    repo_meta: dict[str, Any] | None = None

    # Only query GitHub API if owner is NOT already trusted (saves API quota)
    if fetch_github_meta and owner and repo and owner.lower() not in whitelist:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "SixDeck-Sandbox-Provenance/1.0",
        }
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        try:
            if client is not None:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}",
                    headers=headers,
                    timeout=6.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        repo_meta = data
            else:
                async with httpx.AsyncClient(follow_redirects=True, timeout=6.0) as tmp_client:
                    resp = await tmp_client.get(
                        f"https://api.github.com/repos/{owner}/{repo}",
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict):
                            repo_meta = data
        except Exception:
            repo_meta = None

    return evaluate_provenance(
        url,
        trusted_owners=trusted_owners,
        repo_metadata=repo_meta,
        min_repo_age_days=min_repo_age_days,
    )
