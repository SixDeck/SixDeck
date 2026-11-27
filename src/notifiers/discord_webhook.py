from __future__ import annotations

import asyncio
import logging

import httpx

from src.models import Category, ScrapedItem

logger = logging.getLogger(__name__)

CATEGORY_COLORS: dict[Category, int] = {
    Category.EMULATION: 0x3498DB,  # Bleu
    Category.DECOMP: 0xE67E22,     # Orange
    Category.AI_RE: 0x9B59B6,      # Violet
    Category.DUMPS: 0x1ABC9C,      # Turquoise
    Category.GENERAL: 0x95A5A6,    # Gris
}


def _build_embed(item: ScrapedItem) -> dict:
    summary = item.summary.strip()
    if len(summary) > 300:
        summary = summary[:297] + "..."

    fields = [
        {"name": "Score", "value": f"**{item.relevance_score}/100**", "inline": True},
        {"name": "Source", "value": f"`{item.source.value}`", "inline": True},
        {"name": "Catégorie", "value": f"`{item.category.value}`", "inline": True},
    ]
    if item.playability_status:
        fields.append(
            {
                "name": "Statut",
                "value": f"**{item.playability_status}**",
                "inline": True,
            }
        )
    if item.matched_keywords:
        fields.append(
            {
                "name": "Mots-clés",
                "value": ", ".join(f"`{k}`" for k in item.matched_keywords[:6]),
                "inline": False,
            }
        )

    embed: dict = {
        "title": item.title[:250],
        "url": item.url,
        "description": summary or "_Aucun résumé disponible._",
        "color": CATEGORY_COLORS.get(item.category, 0x95A5A6),
        "fields": fields,
        "footer": {"text": f"Auteur: {item.author}"},
    }
    if item.effective_date:
        embed["timestamp"] = item.effective_date.isoformat()
    return embed


async def send_discord_digest(
    webhook_url: str,
    items: list[ScrapedItem],
    client: httpx.AsyncClient,
    *,
    max_items: int = 15,
) -> list[str]:
    """Envoie les items sous forme d'embeds Discord (lots de 10 max).

    Retourne les IDs des items envoyés avec succès.
    """
    if not webhook_url or not items:
        return []

    selected = items[:max_items]
    sent_ids: list[str] = []

    # Discord limite à 10 embeds par requête webhook
    for i in range(0, len(selected), 10):
        batch = selected[i : i + 10]
        payload = {
            "username": "GTA6 Port & RE Tracker",
            "embeds": [_build_embed(item) for item in batch],
        }
        try:
            resp = await client.post(webhook_url, json=payload, timeout=15.0)
            if resp.status_code in {200, 204}:
                sent_ids.extend(item.id for item in batch)
            elif resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 2.0))
                await asyncio.sleep(min(retry_after, 10.0))
                retry_resp = await client.post(webhook_url, json=payload, timeout=15.0)
                if retry_resp.status_code in {200, 204}:
                    sent_ids.extend(item.id for item in batch)
            else:
                logger.warning(
                    "[discord] Échec webhook HTTP %s : %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning("[discord] Erreur réseau webhook : %s", exc)

        if i + 10 < len(selected):
            await asyncio.sleep(1.0)

    return sent_ids

