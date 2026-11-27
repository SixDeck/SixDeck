from __future__ import annotations

import logging
import os
import subprocess

from src.models import ScrapedItem

logger = logging.getLogger(__name__)

MILESTONE_STATUSES: set[str] = {
    "Playable 60 FPS",
    "Playable 30 FPS",
    "Playable",
    "In-game",
    "Load the world",
    "Boots / Menu",
    "Boots",
}

XBOX_RUNTIME_HINTS: tuple[str, ...] = (
    "neonbridge",
    "flamingo",
    "victory-fork",
    "vitoriabr",
    "d3d12_xs",
    "xgameruntime",
    "scarlettport",
)


def is_milestone_item(item: ScrapedItem) -> bool:
    """Retourne True uniquement si l'item franchit un vrai cap technique."""
    if item.playability_status in MILESTONE_STATUSES:
        return True
    for dl in item.direct_links:
        if dl.is_alive and dl.link_type in {"recomp_config", "release"}:
            return True
    # Nouvelle release d'un runtime Xbox Series connu
    if item.source.value == "github_release":
        low = f"{item.title} {item.url}".lower()
        if any(h in low for h in XBOX_RUNTIME_HINTS):
            return True
    return False


def send_windows_toast(title: str, message: str) -> bool:
    """Envoie une notification bureau Windows native (Toast) sans dépendance externe."""
    if os.name != "nt":
        return False

    safe_title = title.replace("'", "''").replace("\n", " ")[:120]
    safe_msg = message.replace("'", "''").replace("\n", " ")[:220]
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        f"$xml = '<toast><visual><binding template=\"ToastText02\">"
        f"<text id=\"1\">{safe_title}</text>"
        f"<text id=\"2\">{safe_msg}</text>"
        f"</binding></visual></toast>';"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$doc.LoadXml($xml);"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $doc;"
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('VI Community Launcher');"
        "$notifier.Show($toast);"
    )
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception as exc:
        logger.debug("[alerts] Échec Toast Windows : %s", exc)
        return False
