from __future__ import annotations

from typing import Any

import httpx


async def fetch_crates_io_package(
    crate_name: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Queries the official crates.io JSON API for a Rust crate's latest stable release."""
    name = (crate_name or "").strip()
    if not name:
        return None
    url = f"https://crates.io/api/v1/crates/{name}"
    resp = await client.get(
        url,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "SixDeck-Launcher/1.0 (https://github.com/SixDeck/SixDeck)"},
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    crate = data.get("crate") if isinstance(data, dict) else None
    if not isinstance(crate, dict):
        return None
    version = str(crate.get("max_stable_version") or crate.get("max_version") or "").strip()
    if not version:
        return None
    return {
        "registry": "crates.io",
        "package": name,
        "version": version,
        "description": str(crate.get("description") or "").strip()[:240],
        "homepage": str(crate.get("homepage") or crate.get("repository") or f"https://crates.io/crates/{name}"),
        "repository": str(crate.get("repository") or ""),
        "download_url": f"https://crates.io/api/v1/crates/{name}/{version}/download",
        "downloads": int(crate.get("downloads") or 0),
        "updated_at": str(crate.get("updated_at") or ""),
    }


async def fetch_pypi_package(
    package_name: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Queries the official PyPI JSON API for a Python package's latest stable version and download URL."""
    name = (package_name or "").strip()
    if not name:
        return None
    url = f"https://pypi.org/pypi/{name}/json"
    resp = await client.get(
        url,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "SixDeck-Launcher/1.0"},
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    version = str(info.get("version") or "").strip()
    urls = data.get("urls") if isinstance(data.get("urls"), list) else []
    wheel = next((u for u in urls if isinstance(u, dict) and u.get("packagetype") == "bdist_wheel"), None)
    sdist = next((u for u in urls if isinstance(u, dict) and u.get("packagetype") == "sdist"), None)
    chosen = wheel or sdist or (urls[0] if urls and isinstance(urls[0], dict) else {})
    proj_urls = info.get("project_urls") if isinstance(info.get("project_urls"), dict) else {}
    homepage = str(
        info.get("home_page")
        or proj_urls.get("Repository")
        or proj_urls.get("Homepage")
        or f"https://pypi.org/project/{name}/"
    )
    return {
        "registry": "pypi",
        "package": name,
        "version": version,
        "description": str(info.get("summary") or "").strip()[:240],
        "homepage": homepage,
        "repository": str(proj_urls.get("Repository") or proj_urls.get("Source") or ""),
        "download_url": str(chosen.get("url") or f"https://pypi.org/project/{name}/"),
        "sha256": str((chosen.get("digests") or {}).get("sha256") or ""),
        "updated_at": str(chosen.get("upload_time_iso_8601") or ""),
    }


async def fetch_nuget_package(
    package_id: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Queries the official NuGet v3 flat-container JSON API for a .NET/C# package's latest stable version."""
    pkg = (package_id or "").strip()
    if not pkg:
        return None
    pkg_low = pkg.lower()
    idx_url = f"https://api.nuget.org/v3-flatcontainer/{pkg_low}/index.json"
    resp = await client.get(
        idx_url,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "SixDeck-Launcher/1.0"},
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    versions = data.get("versions") if isinstance(data, dict) else None
    if not isinstance(versions, list) or not versions:
        return None
    stable = [str(v) for v in versions if "-" not in str(v)]
    version = stable[-1] if stable else str(versions[-1])
    dl_url = f"https://api.nuget.org/v3-flatcontainer/{pkg_low}/{version.lower()}/{pkg_low}.{version.lower()}.nupkg"
    return {
        "registry": "nuget",
        "package": pkg,
        "version": version,
        "description": f"NuGet package {pkg} ({version})",
        "homepage": f"https://www.nuget.org/packages/{pkg}/{version}",
        "repository": "",
        "download_url": dl_url,
        "updated_at": "",
    }


async def resolve_registry_package(
    registry: str,
    package_name: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Unified dispatcher for Crates.io, PyPI, and NuGet official JSON APIs."""
    reg = (registry or "").strip().lower()
    try:
        if reg in {"crates", "crates.io", "cargo", "rust"}:
            return await fetch_crates_io_package(package_name, client, timeout=timeout)
        if reg in {"pypi", "pip", "python"}:
            return await fetch_pypi_package(package_name, client, timeout=timeout)
        if reg in {"nuget", "dotnet", "csharp"}:
            return await fetch_nuget_package(package_name, client, timeout=timeout)
    except Exception:
        return None
    return None
